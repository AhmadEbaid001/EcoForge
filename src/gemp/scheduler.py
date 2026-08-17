"""The nightly refit, scheduled in-process.

Running inside the API process rather than as a separate container or a cron entry is
not laziness - it is what makes the cache invalidation correct.

The job changes `building.annual_kwh` (F3). The API caches an `OptimizerContext`
holding the candidate set derived from those very values. If the two live in
different processes, nothing tells the API its cache is stale, and it keeps serving
recommendations costed against yesterday's consumption until somebody happens to
restart it. That is a silent wrong answer, which is the worst kind: the map still
renders, the numbers still look plausible, and only the timestamp gives it away.

In-process, the sequence is explicit and ordered:

    refit -> write annual_kwh -> rebuild candidates -> invalidate the cache

Set GEMP_SCHEDULER_ENABLED=0 to run the API without it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("gemp.scheduler")

JOB_ID = "nightly-refit"


def nightly_refit() -> dict:
    """Refit, then bring everything that depends on the refit back into step."""
    from gemp.api.main import reset_context
    from gemp.db import session_scope
    from gemp.ml.jobs import run_nightly
    from gemp.repository import materialize_candidates
    from gemp.services import OptimizerContext

    started = datetime.now()
    summary = run_nightly()

    # annual_kwh has just changed, so every candidate derived from it is stale.
    with session_scope() as session:
        context = OptimizerContext.from_db(session)
        written = materialize_candidates(session, context.candidates, context.inputs_hash)

    # Drop the API's cached context so the next /optimize rebuilds from the new
    # figures rather than serving the ones it happened to load at startup.
    reset_context()

    summary["candidates"] = written
    summary["inputs_hash"] = context.inputs_hash[:16]
    log.info(
        "nightly refit finished in %.0fs: %s",
        (datetime.now() - started).total_seconds(), summary,
    )
    return summary


def enabled() -> bool:
    return os.environ.get("GEMP_SCHEDULER_ENABLED", "1") in ("1", "true", "yes")


def start_scheduler() -> BackgroundScheduler | None:
    """Start the background scheduler, or return None when disabled.

    The hour is deliberately quiet rather than midnight: TimescaleDB's own
    maintenance and the continuous-aggregate refresh policy cluster around the top of
    the hour, and a fifty-model refit is the last thing that should contend with them.
    """
    if not enabled():
        log.info("scheduler disabled by GEMP_SCHEDULER_ENABLED")
        return None

    hour = int(os.environ.get("GEMP_REFIT_HOUR", "3"))
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        nightly_refit,
        trigger=CronTrigger(hour=hour, minute=17),
        id=JOB_ID,
        max_instances=1,
        # If the process was down at the scheduled time, run once on the next
        # opportunity rather than firing several catch-up jobs at once.
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    log.info("scheduler started; nightly refit at %02d:17 UTC", hour)
    return scheduler
