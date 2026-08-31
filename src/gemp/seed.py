"""F6 - backfill history so the system is warm the moment it boots.

Without this, a demonstration starts against an empty database: the forecaster has
nothing to train on, the anomaly detector's rolling window is empty, and the
dashboard is blank for as long as anyone is watching. Backfilling six months at
container init removes the cold start entirely and, as a side effect, makes
`docker compose down -v && up` a reliable way to get back to a known state.

History ends at "now" so the live simulator continues the same process forward
rather than starting a second one with a discontinuity at the join.

    python -m gemp.seed --months 6
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from gemp.config import get_settings
from gemp.db import (
    AnomalyRow,
    ForecastRow,
    IntegrityCheckpointRow,
    ReadingRow,
    get_engine,
    insert_ignore,
    session_scope,
)
from gemp.domain.catalog import load_catalog
from gemp.domain.portfolio import load_buildings
from gemp.ingest.integrity import GENESIS, sign
from gemp.paths import ground_truth_write_path
from gemp.repository import import_catalog, import_portfolio
from gemp.sim.profiles import generate_series
from gemp.timescale import ensure_timescale_objects, refresh_hourly

log = logging.getLogger("gemp.seed")

CHUNK_ROWS = 10_000


def seed_readings(months: int, step_minutes: int, seed: int,
                  anomalies_per_month: float) -> tuple[int, int]:
    """Generate and store signed history for every building.

    Returns (rows written, anomalies injected).
    """
    buildings = load_buildings()
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30 * months)

    key = get_settings().key_bytes
    total_rows = 0
    events: list[dict] = []

    for index, building in enumerate(buildings, start=1):
        series = generate_series(
            building, start, end,
            step_minutes=step_minutes,
            seed=seed + index,
            anomalies_per_month=anomalies_per_month,
        )

        prev_sig = GENESIS
        rows: list[dict] = []
        for seq, (ts, kw) in enumerate(zip(series.timestamps, series.kw, strict=True)):
            row = {
                "building_id": building.id,
                "ts": ts,
                "kw": round(float(kw), 4),
                "source": "seed",
                "seq": seq,
            }
            prev_sig = sign(key, row, prev_sig)
            rows.append({**row, "sig": prev_sig})

        with session_scope() as session:
            dialect = session.bind.dialect.name
            for offset in range(0, len(rows), CHUNK_ROWS):
                session.execute(
                    insert_ignore(ReadingRow, dialect), rows[offset:offset + CHUNK_ROWS]
                )
            # Anchor the chain head, exactly as the live ingester does.
            session.execute(
                insert_ignore(IntegrityCheckpointRow, dialect),
                [{
                    "building_id": building.id,
                    "ts": end,
                    "last_seq": rows[-1]["seq"],
                    "head_sig": rows[-1]["sig"],
                }],
            )

        total_rows += len(rows)
        events.extend({
            "building_id": e.building_id, "kind": e.kind,
            "start": e.start.isoformat(), "end": e.end.isoformat(),
            "magnitude": round(e.magnitude, 4),
        } for e in series.anomalies)

        if index % 10 == 0 or index == len(buildings):
            log.info("seeded %d/%d buildings (%d rows)", index, len(buildings), total_rows)

    write_ground_truth(events)
    return total_rows, len(events)


def write_ground_truth(events: list[dict]) -> None:
    """Injected anomalies, recorded so precision and recall are measurable.

    The proposal's evaluation plan depends on this file existing: without it,
    anomaly-detection quality can only be eyeballed.
    """
    path = ground_truth_write_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["building_id", "kind", "start", "end", "magnitude"]
        )
        writer.writeheader()
        writer.writerows(events)
    log.info("wrote %d ground-truth anomalies to %s", len(events), path)


def already_seeded() -> int:
    with session_scope() as session:
        return session.execute(select(func.count()).select_from(ReadingRow)).scalar_one()


def wipe_readings() -> None:
    """Delete the readings and everything that was derived from them.

    Anomalies and forecasts are not incidental rows: an anomaly names a reading by
    (building, timestamp) and a forecast is fit to a window of them. Leaving them
    behind after a re-seed leaves the alert inbox holding tens of thousands of open
    alerts about readings that no longer exist, each one un-openable, and the
    forecast panel drawing a line fit to deleted history.

    Measured on the staging host: a re-seed that kept them left 76,942 anomalies
    pointing into an empty table.

    What survives, deliberately: accounts, sessions, the audit log, stored
    allocations and the catalog. A stored allocation is a record of what was decided
    and still verifies against its own input hash.
    """
    with session_scope() as session:
        session.execute(delete(ReadingRow))
        session.execute(delete(IntegrityCheckpointRow))
        session.execute(delete(AnomalyRow))
        session.execute(delete(ForecastRow))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--step-minutes", type=int, default=15)
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--anomalies-per-month", type=float, default=1.5)
    parser.add_argument("--force", action="store_true",
                        help="wipe existing readings and reseed")
    parser.add_argument("--skip-portfolio", action="store_true")
    args = parser.parse_args(argv)

    started = time.perf_counter()

    try:
        existing = already_seeded()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  cannot reach the database: {exc}", file=sys.stderr)
        return 1

    if existing and not args.force:
        print(f"database already holds {existing:,} readings; use --force to reseed")
        return 0
    if existing and args.force:
        log.info("wiping %d existing readings", existing)
        wipe_readings()

    if not args.skip_portfolio:
        # `replace` is what makes a re-seed mean "the database now matches the
        # fixture". Without it every id collides, nothing is written, and the
        # readings generated below describe buildings the database does not hold.
        with session_scope() as session:
            buildings = import_portfolio(session, replace=True)
            interventions = import_catalog(session, load_catalog())
        log.info("imported %d buildings and %d interventions", buildings, interventions)

    ensure_timescale_objects(get_engine())

    rows, anomalies = seed_readings(
        args.months, args.step_minutes, args.seed, args.anomalies_per_month
    )

    try:
        refresh_hourly(get_engine())
        log.info("refreshed the hourly aggregate")
    except Exception as exc:  # noqa: BLE001 - aggregate is an optimisation
        log.warning("could not refresh the hourly aggregate: %s", exc)

    elapsed = time.perf_counter() - started
    print(f"\n  seeded {rows:,} readings and {anomalies} anomalies "
          f"over {args.months} months in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
