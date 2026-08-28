"""Long-running maintenance jobs, started from the interface.

Two jobs on this platform take minutes rather than milliseconds: refitting the
forecasters across the portfolio, and re-measuring every claim in the paper.
Both were shell-only, which meant the two things most worth re-running before a
demonstration were the two things nobody could re-run without a terminal and the
repository checked out.

Three properties this module exists to guarantee:

1. **They do not run in the request.** The refit takes about eight minutes on the
   staging host. A synchronous handler would hold a worker for that long, and
   the browser and the reverse proxy would both have given up long before it
   finished - leaving the job running with nobody able to see the result.

2. **One at a time, across the whole process.** Both jobs are CPU-bound and both
   write. Two refits interleaving their writes, or a claims run measuring a
   database midway through a refit, produce numbers that describe no moment that
   ever existed. A second request while one is running is refused, not queued:
   a queue would let somebody hold the button down and book an hour of work.

3. **They are subprocesses, not threads.** The command is a fixed argv - no part
   of it comes from the request, so there is nothing to inject - and running out
   of process means a job that dies takes nothing with it, and cannot
   reconfigure logging or exit the interpreter underneath the API.

Restricted to the admin role at the routing layer. These are the most expensive
operations the platform can be asked to perform, and an analyst who can spend
eight minutes of CPU on request is a denial-of-service waiting to be discovered.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone

log = logging.getLogger("gemp.api.jobs")

# The whole vocabulary. A kind that is not in here is a 404, so the path segment
# can never reach a shell.
JOB_COMMANDS: dict[str, list[str]] = {
    # Refits every building's forecaster and rewrites the stored forecasts and
    # the annual figures the optimizer costs against.
    "forecast-refit": [sys.executable, "-m", "gemp.ml.jobs"],
    # Re-measures every claim in the paper and rewrites out/evaluation/claims.csv,
    # which is what the evidence screen reads. `--quick` keeps it to the claims
    # rather than the full sweep, and figures are for the paper, not the screen.
    "evidence": [sys.executable, "-m", "gemp.evaluate", "--quick", "--no-figures"],
}

JOB_LABELS = {
    "forecast-refit": "Forecast refit",
    "evidence": "Claims harness",
}

# How long a job may run before it is abandoned. The refit is ~8 minutes on the
# staging host; this is generous enough for a slower one and short enough that a
# wedged process cannot block the next run forever.
TIMEOUT_SECONDS = 45 * 60

_lock = threading.Lock()
_state: dict[str, dict] = {
    kind: {"status": "idle", "started_at": None, "finished_at": None,
           "started_by": None, "summary": "", "error": ""}
    for kind in JOB_COMMANDS
}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def status(kind: str) -> dict:
    """A copy, so a caller cannot mutate the state it is reporting on."""
    with _lock:
        return dict(_state[kind], kind=kind, label=JOB_LABELS[kind])


def all_status() -> dict[str, dict]:
    with _lock:
        return {kind: dict(_state[kind], kind=kind, label=JOB_LABELS[kind])
                for kind in _state}


def running_kind() -> str | None:
    with _lock:
        for kind, entry in _state.items():
            if entry["status"] == "running":
                return kind
    return None


def _run(kind: str) -> None:
    command = JOB_COMMANDS[kind]
    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, nothing from the request
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        ok = finished.returncode == 0
        # The tail is what a summary line looks like for both of these jobs, and
        # the whole of stderr on a failure would put a traceback on a screen.
        tail = (finished.stdout or finished.stderr or "").strip().splitlines()[-12:]
        with _lock:
            _state[kind].update(
                status="done" if ok else "failed",
                finished_at=_now(),
                summary="\n".join(tail),
                error="" if ok else f"exit code {finished.returncode}",
            )
        log.info("job %s finished rc=%s", kind, finished.returncode)
    except subprocess.TimeoutExpired:
        with _lock:
            _state[kind].update(status="failed", finished_at=_now(),
                                error=f"gave up after {TIMEOUT_SECONDS // 60} minutes")
        log.warning("job %s timed out", kind)
    except Exception as exc:  # noqa: BLE001 - a job thread must not die silently
        with _lock:
            _state[kind].update(status="failed", finished_at=_now(),
                                error=f"{type(exc).__name__}: {exc}")
        log.exception("job %s raised", kind)


def start(kind: str, username: str) -> tuple[bool, dict]:
    """Start `kind` unless anything is already running.

    Returns `(started, status)`. `started` is False when another job holds the
    slot, and the status returned is then the state of THAT job, so the caller
    can say which one is in the way rather than only that something is.
    """
    with _lock:
        for other, entry in _state.items():
            if entry["status"] == "running":
                return False, dict(entry, kind=other, label=JOB_LABELS[other])
        _state[kind].update(status="running", started_at=_now(), finished_at=None,
                            started_by=username, summary="", error="")
        snapshot = dict(_state[kind], kind=kind, label=JOB_LABELS[kind])

    threading.Thread(target=_run, args=(kind,), daemon=True,
                     name=f"gemp-job-{kind}").start()
    return True, snapshot
