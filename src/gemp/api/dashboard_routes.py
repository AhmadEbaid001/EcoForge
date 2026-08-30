"""Read model for the in-app dashboards.

These endpoints exist because the dashboards moved in-house. Grafana reached into the
database with its own SQL, which meant the panels and the application disagreed about
what a number meant whenever one of them changed - and it needed a second service, a
second login and a second set of credentials to show figures the API already had.

Everything here is shaped for one panel and aggregated server-side. The alternative,
shipping raw rows to the browser and summing them in JavaScript, moves two million
readings over the wire to draw three hundred pixels.

Every window is anchored on the newest READING, never on `now()`. Under 720x replay
data time runs months ahead of the wall clock, and a wall-clock window is empty.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from gemp.api.deps import get_session
from gemp.auth.deps import VIEWER, require
from gemp.db import AnomalyRow, BuildingRow, OptimizationRunRow, ReadingRow
from gemp.repository import latest_reading_ts, open_anomaly_count, reading_count

log = logging.getLogger("gemp.api.dashboard")

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"],
                   dependencies=[Depends(require(VIEWER))])

# Roughly a point per pixel on a wide chart. More is invisible and slower.
MAX_POINTS = 400


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> dict:
    """The stat tiles: one round trip for the whole header row."""
    newest = latest_reading_ts(session)
    readings, readings_exact = reading_count(session)
    buildings = session.execute(select(func.count()).select_from(BuildingRow)).scalar_one()

    measured = session.execute(
        select(BuildingRow.annual_kwh_source, func.count())
        .group_by(BuildingRow.annual_kwh_source)
    ).all()

    severities = session.execute(
        select(AnomalyRow.severity, func.count())
        .where(AnomalyRow.acknowledged.is_(False))
        .group_by(AnomalyRow.severity)
    ).all()

    latest_run = session.execute(
        select(OptimizationRunRow).order_by(OptimizationRunRow.created_at.desc()).limit(1)
    ).scalars().first()

    return {
        "buildings": int(buildings),
        "readings": int(readings),
        # The screen prints "about 34M" rather than "34M" when this is false, so
        # a figure that is an estimate is never presented as a census.
        "readings_exact": readings_exact,
        "data_clock": newest.isoformat() if newest else None,
        "open_anomalies": open_anomaly_count(session),
        "open_by_severity": {row[0]: int(row[1]) for row in severities},
        # F3: how many buildings are costed against measured consumption rather than
        # the figure the portfolio fixture shipped with. The number the whole
        # forecasting layer exists to move.
        "annual_kwh_source": {row[0]: int(row[1]) for row in measured},
        "latest_run": None if latest_run is None else {
            "run_id": latest_run.id,
            "created_at": latest_run.created_at.isoformat(),
            "budget_egp": latest_run.budget_egp,
            "objective": latest_run.objective,
            "solver": latest_run.solver,
            # The cap is part of what was decided, and the overview says so on a
            # card. Without it the card had to print "none" for every run,
            # including the ones that were capped.
            "max_funded_per_district": latest_run.max_funded_per_district,
            "buildings_funded": latest_run.buildings_funded,
            "total_cost_egp": latest_run.total_cost_egp,
            "total_benefit_kgco2e": latest_run.total_benefit_kgco2e,
        },
    }


@router.get("/load")
def portfolio_load(
    session: Session = Depends(get_session),
    days: int = Query(default=14, ge=1, le=180),
) -> dict:
    """Total portfolio demand over recent DATA time.

    Reads the continuous aggregate when it exists, which is what it was created for -
    hourly buckets already materialised, four times fewer rows than the hypertable and
    no client-side resampling. Falls back to the raw table on a deployment without
    TimescaleDB, and on SQLite, where the tests run.
    """
    newest = latest_reading_ts(session)
    if newest is None:
        return {"points": [], "unit": "kW"}

    cutoff = newest - timedelta(days=days)

    try:
        rows = session.execute(text("""
            SELECT bucket AS ts, sum(avg_kw) AS kw
            FROM reading_hourly
            WHERE bucket >= :cutoff
            GROUP BY bucket
            ORDER BY bucket
        """), {"cutoff": cutoff}).all()
    except Exception:  # noqa: BLE001 - fall back rather than fail a dashboard
        session.rollback()
        rows = _raw_hourly(session, cutoff)

    return {"points": _downsample(rows), "unit": "kW", "from": cutoff.isoformat()}


def _raw_hourly(session: Session, cutoff) -> list:
    """Portable fallback: bucket in Python rather than in dialect-specific SQL.

    `date_trunc` is PostgreSQL, `strftime` is SQLite, and writing both means the
    dashboard silently means something different depending on where it runs.
    """
    # Streamed, not materialised. This is the path taken when the continuous
    # aggregate is unavailable - which includes the case where it failed because
    # the database is already under pressure - and `.all()` here pulled every
    # reading in the window into a list first. At 90 days across fifty buildings
    # that is millions of rows held at once, so the recovery path was heavier than
    # the query it was recovering from. The bucket dictionary is bounded by the
    # number of HOURS in the window either way.
    buckets: dict = defaultdict(float)
    result = session.execute(
        select(ReadingRow.ts, ReadingRow.kw)
        .where(ReadingRow.ts >= cutoff)
        .execution_options(stream_results=True, yield_per=10_000)
    )
    for ts, kw in result:
        buckets[ts.replace(minute=0, second=0, microsecond=0)] += float(kw)
    return sorted(buckets.items())


@router.get("/anomalies/daily")
def anomalies_daily(
    session: Session = Depends(get_session),
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Alerts per day of DATA time, split by severity - the triage trend."""
    newest = latest_reading_ts(session)
    if newest is None:
        return {"days": []}

    cutoff = newest - timedelta(days=days)
    rows = session.execute(
        select(AnomalyRow.ts, AnomalyRow.severity, AnomalyRow.acknowledged)
        .where(AnomalyRow.ts >= cutoff)
    ).all()

    by_day: dict[str, Counter] = defaultdict(Counter)
    for ts, severity, acknowledged in rows:
        key = ts.date().isoformat()
        by_day[key][severity] += 1
        by_day[key]["total"] += 1
        if not acknowledged:
            by_day[key]["open"] += 1

    # Every day in the window, including the quiet ones.
    #
    # Emitting only the days that HAVE alerts looks equivalent and is not: the chart
    # places bars by their index in this array, so a missing day does not leave a gap
    # - it pulls every later bar one slot to the left while the axis goes on claiming
    # the full span. Six alerts spread over three weeks would draw as six adjacent
    # days. A day with nothing to report is still a day, and saying so is also the
    # only way the trend reads as a rate.
    span = (newest.date() - cutoff.date()).days
    calendar = [cutoff.date() + timedelta(days=offset) for offset in range(span + 1)]

    return {
        "days": [
            {
                "date": day.isoformat(),
                **{k: int(v) for k, v in by_day.get(day.isoformat(), Counter()).items()},
            }
            for day in calendar
        ]
    }


@router.get("/alerts/summary")
def alerts_summary(session: Session = Depends(get_session)) -> dict:
    """The three fields the alert inbox needs, and nothing else.

    The inbox was calling /dashboard/summary for its total, its severity split
    and the data clock - and paying for the reading count on the way, which was
    six seconds of work for a number it never displays. These three cost about
    ninety milliseconds together.
    """
    severities = session.execute(
        select(AnomalyRow.severity, func.count())
        .where(AnomalyRow.acknowledged.is_(False))
        .group_by(AnomalyRow.severity)
    ).all()
    newest = latest_reading_ts(session)
    return {
        "open_anomalies": open_anomaly_count(session),
        "open_by_severity": {row[0]: int(row[1]) for row in severities},
        "data_clock": newest.isoformat() if newest else None,
    }


@router.get("/anomalies")
def anomaly_feed(
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=500),
    only_open: bool = True,
    severity: str | None = None,
    building_id: str | None = None,
) -> list[dict]:
    """The alert inbox, worst first. Carries ids so a row can be acknowledged."""
    stmt = (
        select(AnomalyRow, BuildingRow.code)
        .join(BuildingRow, BuildingRow.id == AnomalyRow.building_id)
        # NULLS LAST, like the sibling query in /metrics/anomaly. PostgreSQL sorts
        # nulls FIRST on a descending order and treats NaN as larger than every
        # number, so a row with no usable z score would have taken the top of a
        # list whose whole contract is "worst first" - and the inbox renders it as
        # an em dash, putting a blank row where the worst alert should be.
        .order_by(func.abs(AnomalyRow.robust_z).desc().nullslast())
        .limit(limit)
    )
    if only_open:
        stmt = stmt.where(AnomalyRow.acknowledged.is_(False))
    if severity:
        stmt = stmt.where(AnomalyRow.severity == severity)
    if building_id:
        stmt = stmt.where(AnomalyRow.building_id == building_id)

    return [
        {
            "id": row.id,
            "building_id": row.building_id,
            "building_code": code,
            "ts": row.ts.isoformat(),
            "observed_kw": row.observed_kw,
            "expected_kw": row.expected_kw,
            "robust_z": None if row.robust_z != row.robust_z else row.robust_z,
            "severity": row.severity,
            "acknowledged": row.acknowledged,
        }
        for row, code in session.execute(stmt).all()
    ]


@router.get("/runs")
def recent_runs(
    session: Session = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
) -> list[dict]:
    """Stored allocations, newest first. The record of what was decided and when."""
    rows = session.execute(
        select(OptimizationRunRow)
        .order_by(OptimizationRunRow.created_at.desc())
        .limit(limit)
    ).scalars().all()

    return [
        {
            "run_id": row.id,
            "created_at": row.created_at.isoformat(),
            "budget_egp": row.budget_egp,
            "objective": row.objective,
            "solver": row.solver,
            "status": row.status,
            "buildings_funded": row.buildings_funded,
            "total_cost_egp": row.total_cost_egp,
            "total_benefit_kgco2e": row.total_benefit_kgco2e,
            "solve_ms": row.solve_ms,
            "max_funded_per_district": row.max_funded_per_district,
            # Full length, for the same reason /runs/{id} returns it in full: the
            # hash is what makes a run re-findable and reproducible, and half of one
            # proves nothing.
            "inputs_hash": row.inputs_hash,
        }
        for row in rows
    ]


@router.get("/forecast/{building_id}")
def forecast_vs_actual(
    building_id: str,
    session: Session = Depends(get_session),
    hours: int = Query(default=168, ge=24, le=720),
) -> dict:
    """Actual against predicted for one building - the forecast panel.

    Two series on one axis is the only honest way to show a forecast: a MAPE figure
    alone tells a reviewer the model is good without letting them see where it is
    wrong.
    """
    newest = latest_reading_ts(session, building_id)
    if newest is None:
        return {"building_id": building_id, "actual": [], "forecast": [],
                "model_version": None}

    cutoff = newest - timedelta(hours=hours)

    actual = session.execute(
        select(ReadingRow.ts, ReadingRow.kw)
        .where(ReadingRow.building_id == building_id, ReadingRow.ts >= cutoff)
        .order_by(ReadingRow.ts)
    ).all()

    forecast = session.execute(text("""
        SELECT ts, yhat FROM forecast
        WHERE building_id = :b AND ts >= :cutoff
        ORDER BY ts
    """), {"b": building_id, "cutoff": cutoff}).all()

    # Which forecaster actually won for THIS building. The aggregate endpoint
    # reports the split across the portfolio, which does not answer "and what is
    # costing the building I am looking at" - and that is the question a reader
    # doubting one number has.
    model_version = session.execute(text("""
        SELECT model_version FROM forecast
        WHERE building_id = :b
        ORDER BY ts DESC
        LIMIT 1
    """), {"b": building_id}).scalar()

    return {
        "building_id": building_id,
        "model_version": model_version,
        "actual": _downsample(actual),
        "forecast": _downsample(forecast),
    }


def _downsample(rows: list) -> list[list]:
    """Every nth point. Crude on purpose.

    Averaging into buckets would smooth away the spikes an anomaly panel exists to
    show, which is the one thing this chart must not do.
    """
    if not rows:
        return []
    step = max(1, len(rows) // MAX_POINTS)
    return [
        [ts.isoformat(), round(float(value), 2)]
        for ts, value in rows[::step]
        if value is not None
    ]
