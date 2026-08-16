"""Loading training data for the whole portfolio in one read.

Two decisions here are worth more than they look.

**Read the continuous aggregate, not the raw hypertable.** The forecaster works at
hourly resolution, and `reading_hourly` is already hourly: four times fewer rows and
no client-side resampling. This is what the aggregate was created for.

**Read every building in one unparameterized query.** Measured on the running stack,
a per-building query bound to a parameter took 130 seconds to return 6,694 rows,
while an unparameterized query returned all 334,702 rows for all fifty buildings in
1.7 seconds. On a hypertable with 253 chunks the planner cannot exclude chunks for a
parameter value it has not seen, so it plans across all of them - and pays that cost
on every execution. Reading once and grouping in pandas sidesteps it entirely and
replaces fifty round trips with one.
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("gemp.ml.dataset")

HOURLY_QUERY = """
    SELECT building_id, bucket AS ts, avg_kw AS kw
    FROM reading_hourly
    ORDER BY building_id, bucket
"""

# Fallback for a deployment without TimescaleDB, where the aggregate does not exist.
RAW_HOURLY_QUERY = """
    SELECT building_id,
           date_trunc('hour', ts) AS ts,
           avg(kw) AS kw
    FROM reading
    GROUP BY building_id, date_trunc('hour', ts)
    ORDER BY building_id, 2
"""


def load_hourly_all(engine: Engine, use_aggregate: bool = True) -> dict[str, pd.DataFrame]:
    """Every building's hourly load series, keyed by building id."""
    query = HOURLY_QUERY if use_aggregate else RAW_HOURLY_QUERY

    try:
        frame = pd.read_sql(text(query), engine, parse_dates=["ts"])
    except Exception:  # noqa: BLE001 - fall back rather than fail the nightly job
        if not use_aggregate:
            raise
        log.warning("reading_hourly unavailable; falling back to the raw table")
        frame = pd.read_sql(text(RAW_HOURLY_QUERY), engine, parse_dates=["ts"])

    if frame.empty:
        return {}

    return {
        building_id: group[["ts", "kw"]].reset_index(drop=True)
        for building_id, group in frame.groupby("building_id", sort=True)
    }


def coverage(series: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Per-building row counts and spans, for deciding what is trainable."""
    rows = [
        {
            "building_id": bid,
            "hours": len(frame),
            "start": frame["ts"].min(),
            "end": frame["ts"].max(),
        }
        for bid, frame in series.items()
    ]
    return pd.DataFrame(rows).sort_values("hours")
