"""The nightly job: refit forecasts, detect anomalies, refresh the annual estimate.

Batch, not per-request. Fitting fifty models takes about twenty seconds, which is
fine overnight and unacceptable inside a budget-slider round trip. The proposal makes
the same split (Section 3.1) and it is the right one.

Order matters, and the last step is the one that connects this module to the rest of
the system:

1. Load every building's hourly series in one read.
2. Fit a model per building; keep it only if it beats the seasonal-naive baseline.
3. Store the forecast and flag residual anomalies.
4. **Write the annualized estimate back onto the building.** This is the single point
   at which the metered time series reaches the allocation decision (F3). Until this
   runs, the optimizer is costing interventions against the figure that came with the
   portfolio fixture rather than against measured consumption.

    python -m gemp.ml.jobs
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import delete, update

from gemp.db import AnomalyRow, BuildingRow, ForecastRow, get_engine, insert_ignore, session_scope
from gemp.domain.catalog import load_params
from gemp.ml.anomaly import detect_all
from gemp.ml.dataset import load_hourly_all
from gemp.ml.forecast import ForecastResult, fit_building

log = logging.getLogger("gemp.ml.jobs")


def run_nightly(
    test_hours: int = 24 * 14,
    store_forecasts: bool = True,
    update_annual: bool = True,
) -> dict:
    """Refit the portfolio and write the results back. Returns a summary."""
    params = load_params()
    started = time.perf_counter()

    series = load_hourly_all(get_engine())
    if not series:
        log.warning("no readings available; nothing to fit")
        return {"buildings": 0}

    results: list[ForecastResult] = []
    skipped: list[str] = []
    anomaly_count = 0

    for building_id, frame in series.items():
        try:
            result = fit_building(frame, building_id, test_hours=test_hours)
        except ValueError as exc:
            skipped.append(building_id)
            log.info("skipping %s: %s", building_id, exc)
            continue

        results.append(result)

        if store_forecasts:
            _store_forecast(result)

        anomalies = detect_all(
            actual=frame.set_index("ts")["kw"],
            expected=_expected_series(frame, result),
            building_id=building_id,
            k=params.anomaly_k,
            window_days=params.anomaly_window_days,
        )
        anomaly_count += _store_anomalies(anomalies, building_id)

    if update_annual:
        _update_annual_kwh(results)

    elapsed = time.perf_counter() - started
    beat = sum(1 for r in results if r.beat_baseline)

    return {
        "buildings": len(results),
        "skipped": len(skipped),
        "model_beat_baseline": beat,
        "baseline_used": len(results) - beat,
        "anomalies": anomaly_count,
        "seconds": round(elapsed, 1),
        "median_model_mape": _median(r.model_metrics.mape for r in results),
        "median_baseline_mape": _median(r.baseline_metrics.mape for r in results),
    }


def _median(values) -> float | None:
    ordered = sorted(v for v in values if v == v and v != float("inf"))
    if not ordered:
        return None
    mid = len(ordered) // 2
    return round(
        ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2, 3
    )


def _expected_series(frame: pd.DataFrame, result: ForecastResult) -> pd.Series:
    """Expected load aligned to the actual series, across the whole history.

    Uses `full_predictions`, not the held-out window. Scoring only the test window
    would leave most of the seeded history without an expectation, and a detector
    with nothing to compare against reports no anomalies at all - which is exactly
    what happened before this was fixed.
    """
    return result.full_predictions.reindex(pd.DatetimeIndex(frame["ts"]))


def _store_forecast(result: ForecastResult) -> None:
    made_at = datetime.now(UTC)
    records = [
        {
            "building_id": result.building_id,
            "ts": ts.to_pydatetime(),
            "yhat": float(value),
            "model_version": result.model_version,
            "made_at": made_at,
        }
        for ts, value in result.predictions.dropna().items()
    ]
    if not records:
        return

    with session_scope() as session:
        # One forecast per building per timestamp: replace rather than accumulate,
        # or the series would fan out into a history of revisions nobody reads.
        session.execute(
            delete(ForecastRow).where(ForecastRow.building_id == result.building_id)
        )
        session.execute(insert_ignore(ForecastRow, session.bind.dialect.name), records)


def _store_anomalies(anomalies, building_id: str) -> int:
    """Replace this building's anomalies rather than adding to them.

    Accumulating across runs mixes detections made at different thresholds: after
    tuning k from 4 to 8, the table still held every k=4 flag, so the dashboard
    showed a sensitivity nobody had chosen.
    """
    with session_scope() as session:
        session.execute(delete(AnomalyRow).where(AnomalyRow.building_id == building_id))

    if not anomalies:
        return 0

    records = [
        {
            "building_id": a.building_id,
            "ts": a.ts.to_pydatetime(),
            "observed_kw": a.observed_kw,
            "expected_kw": a.expected_kw,
            "residual": a.residual,
            "robust_z": a.robust_z,
            "severity": a.severity,
            "acknowledged": False,
        }
        for a in anomalies
    ]
    with session_scope() as session:
        session.execute(insert_ignore(AnomalyRow, session.bind.dialect.name), records)
    return len(records)


def _update_annual_kwh(results: list[ForecastResult]) -> None:
    """F3 + A5: replace the fixture's annual figure with the measured one.

    Recorded as `annual_kwh_source='forecast'` so it is visible which buildings are
    costed against measurement and which are still on their seed value - a
    distinction that matters when explaining a recommendation.

    The A5 load shape travels the same road: derived from the identical window,
    persisted beside the annual figure, and consumed by the TOU carbon weighting.
    A building whose shape is null is costed as flat, so a deployment upgrades its
    carbon accounting building-by-building as refits land rather than all at once.
    """
    with session_scope() as session:
        for result in results:
            if result.annual_kwh <= 0:
                continue
            session.execute(
                update(BuildingRow)
                .where(BuildingRow.id == result.building_id)
                .values(
                    annual_kwh=result.annual_kwh,
                    annual_kwh_source="forecast",
                    load_shape=list(result.load_shape),
                    load_shape_source="forecast",
                    updated_at=datetime.now(UTC),
                )
            )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--test-hours", type=int, default=24 * 14)
    parser.add_argument("--no-store", action="store_true",
                        help="fit and report without writing anything")
    args = parser.parse_args(argv)

    try:
        summary = run_nightly(
            test_hours=args.test_hours,
            store_forecasts=not args.no_store,
            update_annual=not args.no_store,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for key, value in summary.items():
        print(f"  {key:22} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
