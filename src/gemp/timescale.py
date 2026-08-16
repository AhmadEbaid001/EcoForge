"""TimescaleDB objects: the reading hypertable and the hourly continuous aggregate.

These live outside Alembic on purpose. A continuous aggregate cannot be created
inside a transaction block, and Alembic wraps every migration in one. Running them
from an idempotent bootstrap with AUTOCOMMIT is simpler than fighting that, and it
also lets the stack degrade to plain PostgreSQL - useful because the container
runtime is not available on every machine the team builds on.

    python -m gemp.timescale
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import text
from sqlalchemy.engine import Engine

from gemp.db import get_engine

log = logging.getLogger("gemp.timescale")

CHUNK_INTERVAL = "1 day"

HOURLY_VIEW = """
CREATE MATERIALIZED VIEW IF NOT EXISTS reading_hourly
WITH (timescaledb.continuous) AS
SELECT
    building_id,
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    avg(kw)   AS avg_kw,
    max(kw)   AS max_kw,
    min(kw)   AS min_kw,
    count(*)  AS samples
FROM reading
GROUP BY building_id, bucket
WITH NO DATA;
"""

# Both offsets are NULL deliberately.
#
# A continuous aggregate policy's offsets are measured against the wall clock. Under
# accelerated replay the DATA clock runs ahead of the wall clock - at 720x, an hour of
# real time is a month of data - so every reading is "in the future" as far as
# PostgreSQL is concerned. A conventional `end_offset => INTERVAL '1 hour'` would
# therefore refuse to materialise the newest data, and the dashboard would show a
# permanently empty tail during the one demonstration that matters.
REFRESH_POLICY = """
SELECT add_continuous_aggregate_policy(
    'reading_hourly',
    start_offset => NULL,
    end_offset   => NULL,
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE
);
"""


def has_timescale(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(connection.execute(text(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"
        )).scalar())


def ensure_timescale_objects(engine: Engine | None = None) -> dict[str, str]:
    """Create the hypertable and continuous aggregate. Safe to run repeatedly."""
    engine = engine or get_engine()
    result: dict[str, str] = {}

    if engine.dialect.name != "postgresql":
        return {"skipped": f"dialect is {engine.dialect.name}, not postgresql"}

    if not has_timescale(engine):
        log.warning("timescaledb extension unavailable - running as plain PostgreSQL")
        return {"skipped": "timescaledb extension not available"}

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        result["extension"] = "ok"

        # The primary key is (building_id, ts), which already contains the
        # partitioning column - a hypertable requires that.
        connection.execute(text(f"""
            SELECT create_hypertable(
                'reading', 'ts',
                chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}',
                migrate_data => TRUE,
                if_not_exists => TRUE
            )
        """))
        result["hypertable"] = "ok"

        connection.execute(text(HOURLY_VIEW))
        result["continuous_aggregate"] = "ok"

        try:
            connection.execute(text(REFRESH_POLICY))
            result["refresh_policy"] = "ok"
        except Exception as exc:  # noqa: BLE001 - policy is an optimisation, not a gate
            log.warning("could not add refresh policy: %s", exc)
            result["refresh_policy"] = f"skipped: {type(exc).__name__}"

    return result


def refresh_hourly(engine: Engine | None = None) -> None:
    """Materialise the aggregate now, rather than waiting for the policy.

    Called after a seed backfill so the dashboard has data the moment it opens.
    """
    engine = engine or get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(
            "CALL refresh_continuous_aggregate('reading_hourly', NULL, NULL)"
        ))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true",
                        help="also materialise the aggregate immediately")
    args = parser.parse_args(argv)

    try:
        status = ensure_timescale_objects()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for key, value in status.items():
        print(f"  {key:22} {value}")

    if args.refresh and "skipped" not in status:
        refresh_hourly()
        print("  refreshed               ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
