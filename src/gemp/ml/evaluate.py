"""Tuning the anomaly threshold against injected ground truth.

The proposal (Sections 4.2 and 4.5) requires `k` to be chosen so that the flag rate
is "neither negligible nor overwhelming", and states that precision and recall are
measurable because the simulator records the anomalies it injects. This module is
that measurement. Without it `k = 3` is a guess, and a guess is not something to put
in front of judges next to the word "tuned".

Matching is at EVENT level, not sample level, and the asymmetry is deliberate:

* **Recall** counts a ground-truth event as found if at least one hour inside it was
  flagged. An operator who is told "this building has a fault, starting Tuesday" does
  not need every hour of that fault flagged separately.
* **Precision** counts a flagged hour as correct if it falls inside any event for
  that building. Flagging the same real fault for ten consecutive hours is one
  correct detection repeated, not ten separate false alarms.

    python -m gemp.ml.evaluate --sweep
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from gemp.db import get_engine
from gemp.domain.catalog import load_params
from gemp.ml.anomaly import detect
from gemp.ml.dataset import load_hourly_all
from gemp.ml.forecast import fit_building
from gemp.paths import data_dir

log = logging.getLogger("gemp.ml.evaluate")


@dataclass
class Scores:
    k: float
    flagged: int
    events: int
    events_found: int
    true_positives: int
    flag_rate: float

    @property
    def recall(self) -> float:
        return self.events_found / self.events if self.events else 0.0

    @property
    def precision(self) -> float:
        return self.true_positives / self.flagged if self.flagged else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def row(self) -> str:
        return (
            f"  {self.k:>4.1f}  {self.flagged:>8,}  {self.flag_rate:>7.2%}  "
            f"{self.precision:>9.3f}  {self.recall:>7.3f}  {self.f1:>6.3f}  "
            f"{self.events_found:>4}/{self.events}"
        )


def load_ground_truth(path=None) -> pd.DataFrame:
    """Anomalies the simulator injected, written during seeding."""
    path = path or (data_dir() / "ground_truth.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. It is written by `python -m gemp.seed`; without it "
            f"precision and recall cannot be computed and k cannot be tuned."
        )

    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    return pd.DataFrame([
        {
            "building_id": r["building_id"],
            "kind": r["kind"],
            "start": datetime.fromisoformat(r["start"]),
            "end": datetime.fromisoformat(r["end"]),
        }
        for r in rows
    ])


def score(detections: dict[str, list], truth: pd.DataFrame) -> Scores:
    """Precision, recall and F1 at event level."""
    by_building: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in truth.itertuples():
        by_building.setdefault(row.building_id, []).append((row.start, row.end))

    flagged = sum(len(v) for v in detections.values())
    true_positives = 0
    found: set[tuple[str, datetime]] = set()

    for building_id, anomalies in detections.items():
        windows = by_building.get(building_id, [])
        for anomaly in anomalies:
            ts = anomaly.ts.to_pydatetime()
            for start, end in windows:
                if start <= ts <= end:
                    true_positives += 1
                    found.add((building_id, start))
                    break

    return Scores(
        k=0.0,
        flagged=flagged,
        events=len(truth),
        events_found=len(found),
        true_positives=true_positives,
        flag_rate=0.0,
    )


def sweep(k_values: list[float], test_hours: int = 24 * 14) -> list[Scores]:
    """Fit once, then score the detector at each threshold.

    Fitting is the expensive part and does not depend on `k`, so it happens once and
    the residuals are reused across the sweep.
    """
    params = load_params()
    truth = load_ground_truth()
    series = load_hourly_all(get_engine())

    fitted = {}
    total_points = 0
    for building_id, frame in series.items():
        try:
            result = fit_building(frame, building_id, test_hours=test_hours)
        except ValueError:
            continue
        actual = frame.set_index("ts")["kw"]
        expected = result.full_predictions.reindex(pd.DatetimeIndex(frame["ts"]))
        fitted[building_id] = (actual, expected)
        total_points += len(actual)

    log.info("fitted %d buildings, %d hourly points", len(fitted), total_points)

    out: list[Scores] = []
    for k in k_values:
        detections = {
            building_id: detect(
                actual, expected, building_id,
                k=k, window_days=params.anomaly_window_days,
            )
            for building_id, (actual, expected) in fitted.items()
        }
        scores = score(detections, truth)
        scores.k = k
        scores.flag_rate = scores.flagged / total_points if total_points else 0.0
        out.append(scores)
        log.info("k=%.1f -> precision %.3f recall %.3f", k, scores.precision, scores.recall)

    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--k", type=float, nargs="+",
                        default=[3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0])
    parser.add_argument("--test-hours", type=int, default=24 * 14)
    args = parser.parse_args(argv)

    try:
        results = sweep(args.k, args.test_hours)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    print("\n     k   flagged  flag-rate  precision   recall      F1  events found")
    print(f"  {'-' * 64}")
    for scores in results:
        print(scores.row())

    best = max(results, key=lambda s: s.f1)
    print(f"\n  best F1 at k={best.k:g}: precision {best.precision:.3f}, "
          f"recall {best.recall:.3f}, flag rate {best.flag_rate:.2%}")
    print("  set anomaly_k in data/params.yaml accordingly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
