"""F11 - FastAPI application.

One process, several modules - the deployment shape argued for in the review. The
route handlers are deliberately thin; anything with judgement in it lives in
`gemp.domain` or `gemp.services`, which is what keeps the optimizer runnable from a
command line with no web server involved.

F11 asked for six independently deployed services to collapse into a modular
monolith, and this module is where that collapse is visible: the ingester and the
scheduler are threads started in the lifespan below rather than services of their
own. The seams the review said would survive the collapse are the package
boundaries, not process boundaries, and `gemp.domain` importing no database, broker
or web framework is what keeps them honest. The other two thirds of F11 - SMTP
dropped for a webhook, WireGuard never built - are labelled at
`gemp.ingest.webhook` and `gemp.sim.node`.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gemp.api import auth_routes, dashboard_routes, jobs
from gemp.api.deps import get_session
from gemp.auth.deps import (
    ADMIN,
    ANALYST,
    VIEWER,
    check_csrf,
    current_principal,
    is_public,
    require,
    resolve_principal,
)
from gemp.auth.service import Principal
from gemp.config import get_settings
from gemp.domain.catalog import load_params
from gemp.domain.lifecycle import (  # noqa: SLF001 - one costing truth, shared deliberately
    _dimension,
    upfront_cost_egp,
)
from gemp.domain.models import Allocation
from gemp.domain.savings import solar_kwp
from gemp.optimize.objective import OBJECTIVES
from gemp.optimize.runner import SOLVERS, improvement_pct
from gemp.repository import (
    acknowledge_anomalies,
    latest_reading_ts,
    load_building_rows,
    materialize_candidates,
    open_anomaly_count,
    read_series,
)
from gemp.services import OptimizerContext, get_run, run_optimization, verify_building_chain

log = logging.getLogger("gemp.api")

_context: OptimizerContext | None = None

# Set by the lifespan. /health reads it, which is the only reason it exists at module
# scope: a thread that has stopped writing is otherwise indistinguishable, from
# outside, from a portfolio that has gone quiet.
_ingester: object | None = None

# Rejected-reading count at the previous /health call, so the endpoint can report
# that readings are being refused right now rather than that they once were.
_rejected_seen = 0


# Sync endpoints run in a threadpool, so "if it is None, build it" is a
# check-then-act across threads: two requests arriving cold both saw None and both
# expanded the whole candidate set - about 1,400 candidates over fifty buildings,
# twice, while the first request was still doing it.
_context_lock = threading.Lock()


def get_context(session: Session = Depends(get_session)) -> OptimizerContext:
    """Cached candidate set, rebuilt when the portfolio or catalog changes."""
    global _context
    if _context is not None:
        return _context
    with _context_lock:
        # Re-checked under the lock: another thread may have built it while this
        # one waited, and rebuilding on top of that would throw away a context
        # a concurrent request is already solving against.
        if _context is None:
            _context = OptimizerContext.from_db(session)
        return _context


def reset_context() -> None:
    global _context
    with _context_lock:
        _context = None


def _refuse_multiple_workers() -> None:
    """Fail loudly rather than silently multiplying the login rate limit.

    Two things in this process are per-process state and are correct only because
    there is one of it: the login throttle counters in `gemp.auth.service`, and the
    ingester thread. Run this app under `--workers 4` and the throttle becomes four
    independent counters - five failures each, twenty attempts before anything is
    refused - and four ingesters race to write the same signed chain.

    Neither failure announces itself. The throttle still returns 429 eventually, the
    ingester still ingests, and the only symptom is that a security control is
    quietly four times weaker than it reads. So the assumption is checked instead of
    documented: this refuses to start rather than starting wrong.

    Both spellings are covered - the `--workers` flag and the WEB_CONCURRENCY
    environment variable uvicorn and gunicorn both honour - because the environment
    variable is the one that gets set in a compose file by someone who never read
    this module.
    """
    requested = os.environ.get("WEB_CONCURRENCY", "").strip()
    if requested.isdigit() and int(requested) > 1:
        raise RuntimeError(
            f"WEB_CONCURRENCY={requested}: this application holds its login throttle "
            "and its ingester in process memory, so it must run as a single worker. "
            "Scale it behind more containers, not more workers in one."
        )

    argv = sys.argv
    for flag in ("--workers", "-w"):
        if flag in argv:
            index = argv.index(flag)
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if value.isdigit() and int(value) > 1:
                raise RuntimeError(
                    f"{flag} {value}: this application holds its login throttle and "
                    "its ingester in process memory, so it must run as a single "
                    "worker. Scale it behind more containers, not more workers in one."
                )


def _start_ingester() -> tuple[object, threading.Thread] | tuple[None, None]:
    """Run the MQTT ingester as a thread inside the API process.

    This is the modular-monolith decision made concrete: the ingester is a module
    boundary, not a deployment boundary. Splitting it into its own container would
    add a Dockerfile, a healthcheck and a failure mode for a workload that is a
    batched INSERT every two seconds.

    Set GEMP_INGEST_ENABLED=0 to run the API without it - useful when driving the
    ingester manually from a test or a script.
    """
    if os.environ.get("GEMP_INGEST_ENABLED", "1") not in ("1", "true", "yes"):
        log.info("ingester disabled by GEMP_INGEST_ENABLED")
        return None, None

    from gemp.ingest.consumer import Ingester

    ingester = Ingester(get_settings())
    thread = threading.Thread(target=ingester.run, name="gemp-ingest", daemon=True)
    thread.start()
    log.info("ingester thread started")
    return ingester, thread


def configure_logging() -> None:
    """Make the application's own loggers visible under uvicorn.

    uvicorn installs its own handlers and does not touch other loggers, so
    everything `gemp.*` logs - the ingester connecting, the scheduler arming, a
    building skipped during a refit - goes nowhere by default. The containers looked
    healthy while being silent about the two background threads doing the actual
    work, which is precisely the state in which a failure goes unnoticed.
    """
    root = logging.getLogger("gemp")
    if root.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s  %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(os.environ.get("GEMP_LOG_LEVEL", "INFO"))
    root.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Before anything else: the throttle counters and the ingester below are both
    # per-process state, and both are silently wrong under more than one worker.
    _refuse_multiple_workers()
    log.info("gemp api starting")
    ingester, thread = _start_ingester()

    global _ingester
    _ingester = ingester

    from gemp.scheduler import start_scheduler

    scheduler = start_scheduler()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if ingester is not None:
            ingester.stop()
            thread.join(timeout=10)
        _ingester = None
        log.info("gemp api stopping")


# `/docs`, `/redoc` and `/openapi.json` are the complete endpoint inventory, every
# parameter shape and the role each route demands - a reconnaissance map for anyone
# who can reach the port. Off unless GEMP_API_DOCS says otherwise, and even then NOT
# anonymous: they live outside /api/, which is the prefix the deny-by-default
# middleware scopes to, so being listed anywhere as "public" would have been
# decorative rather than enforced. When mounted they are guarded here, like any
# other route; unmounted they fall through to a 404, which confirms nothing about
# configuration to a prober.
_docs = get_settings().api_docs

_SCHEMA_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"}) if _docs else frozenset()

app = FastAPI(
    title="GEMP",
    description="Budget-constrained retrofit decision support for public building portfolios.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)


app.include_router(auth_routes.router)
app.include_router(dashboard_routes.router)


# Sent on every response. Each one closes a class of attack that a single-origin app
# is otherwise still exposed to.
SECURITY_HEADERS = {
    # No inline scripts, no external anything. This is also the offline guarantee
    # (F13) expressed as a policy the browser enforces rather than a habit the team
    # remembers: a CDN link added "just for one icon" stops working immediately.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    # Clickjacking. `frame-ancestors` above covers modern browsers; this covers the
    # rest.
    "X-Frame-Options": "DENY",
    # Stops a browser from deciding that a JSON response is really HTML and running it.
    "X-Content-Type-Options": "nosniff",
    # Do not leak the page someone came from - map URLs carry run ids.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # Caching a page rendered for one user and serving it to the next is a data leak
    # that looks like a performance optimisation.
    "Cache-Control": "no-store",
}


@app.middleware("http")
async def security_middleware(request, call_next):
    """Authenticate first, then authorise per route. Deny by default.

    Authentication lives here rather than in a dependency because a dependency has to
    be remembered. A route added next month by someone who has not read this file is
    protected the moment it exists, and the only way to expose one is to add its path
    to `PUBLIC_PATHS` deliberately.
    """
    path = request.url.path
    request.state.principal = None

    needs_auth = (path.startswith("/api/") or path in _SCHEMA_PATHS) \
        and not is_public(path)

    # Resolve the session through the SAME provider the routes use, honouring any
    # override the application is configured with. Middleware sits outside FastAPI's
    # dependency injection, so calling `get_sessionmaker()` directly here would open a
    # second connection per request and - worse - ignore the override entirely, which
    # is how a test suite pointed at SQLite ends up trying to reach the production
    # database to check a cookie.
    provider = app.dependency_overrides.get(get_session, get_session)
    generator = provider()
    session = next(generator)
    try:
        try:
            request.state.principal = resolve_principal(session, request)
        except Exception:  # noqa: BLE001 - a broken session must not 500 the request
            log.exception("failed to resolve session")
            request.state.principal = None

        if needs_auth and request.state.principal is None:
            return _json_error(401, "authentication required")

        try:
            check_csrf(request)
        except HTTPException as exc:
            return _json_error(exc.status_code, exc.detail)
    finally:
        # Drive the generator to completion so it commits and closes exactly as it
        # would at the end of a request.
        with contextlib.suppress(StopIteration):
            next(generator, None)

    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if get_settings().cookie_secure:
        # Only over TLS. Sending HSTS from a plaintext origin is ignored by browsers
        # and, if it were not, would strand a demonstration on http.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def _json_error(status: int, detail: str) -> JSONResponse:
    response = JSONResponse({"error": "unauthorized" if status == 401 else "forbidden",
                             "detail": detail}, status_code=status)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.exception_handler(ValueError)
async def _value_error_is_a_bad_request(_request, exc: ValueError) -> JSONResponse:
    """A rejected input is the caller's problem, not a server fault.

    The domain layer raises ValueError for an unknown solver, a negative budget or a
    non-finite one. Without this it surfaces as a bare 500 and the caller cannot tell
    a typo from an outage - which is exactly what happened with `budget_egp:
    Infinity`, where `int(round(inf))` inside the CP-SAT model build produced an
    opaque Internal Server Error.
    """
    return JSONResponse({"error": "invalid_request", "detail": str(exc)}, status_code=422)


@app.exception_handler(Exception)
async def _unhandled_errors_are_named(_request, exc: Exception) -> JSONResponse:
    """Log the traceback, return the exception TYPE and nothing else.

    A stack trace in the response body is a gift to anyone probing the service, and
    an empty body is useless to whoever is trying to fix it during a demonstration.
    The type is the compromise: enough to say what broke, not enough to describe how
    the process is put together.
    """
    log.exception("unhandled error: %s", type(exc).__name__)
    return JSONResponse(
        {"error": "internal_error", "detail": type(exc).__name__}, status_code=500
    )


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


# Funding the entire portfolio costs about 40 M EGP. A trillion is far past any
# defensible input and still leaves CP-SAT's integer coefficients well inside range;
# the point of the bound is to turn nonsense into a 422 rather than a 500.
MAX_BUDGET_EGP = 1e12

# CP-SAT's own limit. Exposed per request because a demonstration that hangs is worse
# than one that returns a good feasible answer and says so.
DEFAULT_SOLVE_SECONDS = 10.0
MAX_SOLVE_SECONDS = 60.0

# Set by StreamingDetector._severity from the robust z score. Listed here so an
# unknown value is a 422 rather than a filter that silently matches nothing.
SEVERITIES = frozenset({"medium", "high", "critical"})

# Solving is CPU-bound and CP-SAT already takes eight search workers. A handful of
# concurrent requests will therefore saturate the machine and slow down the one
# request that matters - the person dragging the budget slider in front of judges.
# Excess requests are refused immediately rather than queued: a 429 arriving now is
# more useful than a correct answer arriving after the moment has passed.
MAX_CONCURRENT_SOLVES = max(1, min(4, (os.cpu_count() or 4) // 2))
_solve_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SOLVES)

# The global cap bounds the MACHINE; this one bounds an ACCOUNT. Without it, one
# analyst scripting the endpoint keeps every slot permanently busy - each request
# legally under the global cap - and the demonstration's own slider request is the
# one that starves. Half the global pool is a ceiling no honest interactive use
# approaches (the UI solves serially) and a floor a script cannot hide under.
MAX_SOLVES_PER_PRINCIPAL = max(1, MAX_CONCURRENT_SOLVES // 2)
_inflight_solves: dict[str, int] = {}
_inflight_lock = threading.Lock()


@contextmanager
def solve_slot(principal) -> Iterator[None]:
    """Hold one of the solver slots, or refuse the request."""
    who = principal.username
    with _inflight_lock:
        mine = _inflight_solves.get(who, 0)
        if mine >= MAX_SOLVES_PER_PRINCIPAL:
            raise HTTPException(
                429,
                f"this account already has {mine} solves in flight; "
                "wait for one to finish",
                headers={"Retry-After": "2"},
            )
        _inflight_solves[who] = mine + 1

    if not _solve_slots.acquire(blocking=False):
        with _inflight_lock:
            left = _inflight_solves.get(who, 1) - 1
            if left <= 0:
                _inflight_solves.pop(who, None)
            else:
                _inflight_solves[who] = left
        raise HTTPException(
            429,
            f"all {MAX_CONCURRENT_SOLVES} solver slots are busy; retry shortly",
            headers={"Retry-After": "2"},
        )
    try:
        yield
    finally:
        _solve_slots.release()
        with _inflight_lock:
            left = _inflight_solves.get(who, 1) - 1
            if left <= 0:
                _inflight_solves.pop(who, None)
            else:
                _inflight_solves[who] = left


class OptimizeRequest(BaseModel):
    budget_egp: float = Field(
        gt=0, le=MAX_BUDGET_EGP, allow_inf_nan=False,
        description="Capital available now, EGP",
    )
    objective: str = Field(default="lca_carbon")
    solver: str = Field(default="cpsat")
    max_funded_per_district: int | None = Field(default=None, ge=1)
    persist: bool = True
    max_seconds: float = Field(
        default=DEFAULT_SOLVE_SECONDS, gt=0, le=MAX_SOLVE_SECONDS, allow_inf_nan=False,
        description="Solver time limit. CP-SAT returns its best feasible solution "
                    "if it expires, and the response says so in `status`.",
    )


class AllocationItemOut(BaseModel):
    building_id: str
    building_code: str
    district: str
    label: str
    intervention_ids: list[str]
    cost_egp: float
    annual_kwh_saving: float
    annual_egp_saving: float
    lifetime_benefit_kgco2e: float


class OptimizeResponse(BaseModel):
    run_id: str | None
    solver: str
    objective: str
    status: str
    budget_egp: float
    total_cost_egp: float
    budget_used_frac: float
    buildings_funded: int
    total_kwh_saving: float
    total_egp_saving: float
    total_benefit_kgco2e: float
    benefit_per_egp: float
    solve_ms: float
    items: list[AllocationItemOut]

    @classmethod
    def build(cls, run_id: str | None, allocation: Allocation) -> OptimizeResponse:
        return cls(
            run_id=run_id,
            solver=allocation.solver,
            objective=allocation.objective,
            status=allocation.status,
            budget_egp=allocation.budget_egp,
            total_cost_egp=allocation.total_cost_egp,
            budget_used_frac=allocation.budget_used_frac,
            buildings_funded=allocation.buildings_funded,
            total_kwh_saving=allocation.total_kwh_saving,
            total_egp_saving=allocation.total_egp_saving,
            total_benefit_kgco2e=allocation.total_benefit_kgco2e,
            benefit_per_egp=allocation.benefit_per_egp,
            solve_ms=allocation.solve_ms,
            items=[
                AllocationItemOut(
                    building_id=i.building_id,
                    building_code=i.building_code,
                    district=i.district,
                    label=i.label,
                    intervention_ids=list(i.intervention_ids),
                    cost_egp=i.cost_egp,
                    annual_kwh_saving=i.annual_kwh_saving,
                    annual_egp_saving=i.annual_egp_saving,
                    lifetime_benefit_kgco2e=i.lifetime_benefit_kgco2e,
                )
                for i in allocation.items
            ],
        )


# ---------------------------------------------------------------------------
# health and metadata
# ---------------------------------------------------------------------------


# /health is public and unauthenticated, and `latest_reading_ts` is a max() across
# the whole reading hypertable - the single most expensive query the service runs,
# taking a lock per chunk while it does. An anonymous caller could therefore drive
# arbitrary database load through the one endpoint nginx serves with `access_log
# off`, so the flood would not even appear in a log.
#
# The answer is cached for a couple of seconds. Container healthchecks poll every
# five to thirty seconds and see no difference; a caller hammering the endpoint gets
# the same cached dict and touches the database once per window.
HEALTH_TTL_S = 2.0
_health_cache: tuple[float, dict, int] | None = None
_health_lock = threading.Lock()


@app.get("/health")
def health(session: Session = Depends(get_session)) -> JSONResponse:
    """Per-dependency status, so a failing container says which dependency failed."""
    global _health_cache

    now = time.monotonic()
    cached = _health_cache
    if cached is not None and now - cached[0] < HEALTH_TTL_S:
        return JSONResponse(cached[1], status_code=cached[2])

    # One prober refreshes; the rest are served the previous answer rather than
    # queueing behind it, which is the whole point of the cache.
    if not _health_lock.acquire(blocking=False):
        if cached is not None:
            return JSONResponse(cached[1], status_code=cached[2])
        _health_lock.acquire()

    try:
        status = {"api": "ok", "database": "unknown", "readings": None,
                  "ingest": _ingest_status()}
        code = 200
        try:
            session.execute(text("SELECT 1"))
            status["database"] = "ok"
            latest = latest_reading_ts(session)
            status["readings"] = latest.isoformat() if latest else None
        except Exception:  # noqa: BLE001 - health must report, not raise
            # Coarse on purpose: this endpoint is public, and the driver's exception
            # class names are operator detail. The full traceback goes to the log,
            # where it belongs.
            log.exception("health check: database query failed")
            status["database"] = "error"
            code = 503
        _health_cache = (time.monotonic(), status, code)
        return JSONResponse(status, status_code=code)
    finally:
        _health_lock.release()


def _ingest_status() -> str:
    """Whether readings are actually being written, in one word.

    Everything else on this endpoint can be green while ingestion is dead: the API
    answers, the database answers, and `readings` simply stops advancing - which
    nothing watches, and which the simulator reads to decide where to resume. A
    failing write path now says so here rather than only in the log.

    Coarse on purpose. /health is public, and the count of consecutive failures and
    the driver's error text are operator detail, not something to hand an anonymous
    caller.
    """
    if _ingester is None:
        return "disabled"
    if getattr(_ingester, "write_failures", 0):
        return "stalled"
    # Rejections are counted for the life of the process, so reporting on the total
    # would pin this to "rejecting" forever after one bad reading. What matters is
    # whether it is happening NOW, which is what the delta since the last check says.
    global _rejected_seen
    rejected = getattr(_ingester, "rejected", 0)
    recent = rejected > _rejected_seen
    _rejected_seen = rejected
    return "rejecting" if recent else "ok"


@app.get("/api/v1/meta", dependencies=[Depends(require(VIEWER))])
def meta(context: OptimizerContext = Depends(get_context)) -> dict:
    params = context.params
    return {
        "buildings": len(context.buildings),
        "interventions": len(context.catalog),
        "candidates": len(context.candidates),
        "inputs_hash": context.inputs_hash[:16],
        "objectives": sorted(OBJECTIVES),
        "solvers": sorted(SOLVERS),
        "horizon_yr": params.horizon_yr,
        "grid_emission_factor": params.grid_emission_factor,
        "tariff_egp_per_kwh": params.electricity_tariff_egp_per_kwh,
        "uncited_catalog_rows": [iv.id for iv in context.catalog if iv.needs_citation],
    }


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------


@app.get("/api/v1/buildings", dependencies=[Depends(require(VIEWER))])
def list_buildings(session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": row.id, "code": row.code, "name": row.name, "district": row.district,
            "lat": row.lat, "lon": row.lon,
            "floor_area_m2": row.floor_area_m2, "roof_area_m2": row.roof_area_m2,
            "occupancy_pattern": row.occupancy_pattern,
            "insulation_quality": row.insulation_quality,
            "hvac_age_yr": row.hvac_age_yr,
            "annual_kwh": row.annual_kwh,
        }
        for row in load_building_rows(session)
    ]


@app.get("/api/v1/map/geojson", dependencies=[Depends(require(VIEWER))])
def map_geojson(session: Session = Depends(get_session),
                run_id: str | None = Query(default=None)) -> dict:
    """Footprints, optionally annotated with an allocation for colouring."""
    funded: dict[str, dict] = {}
    if run_id:
        try:
            _run, items = get_run(session, run_id)
        except KeyError as exc:
            raise HTTPException(404, f"no run {run_id}") from exc
        funded = {
            item.building_id: {
                "label": item.label,
                "cost_egp": item.cost_egp,
                "annual_kwh_saving": item.annual_kwh_saving,
                "lifetime_benefit_kgco2e": item.lifetime_benefit_kgco2e,
            }
            for item in items
        }

    # Open anomalies per building, so the map can show where something is wrong now
    # rather than only where money should go next year.
    #
    # The cutoff is computed here rather than written as `INTERVAL '7 days'`, which is
    # PostgreSQL-only and breaks the SQLite-backed contract tests. It is also anchored
    # on the newest READING, not on the wall clock: under accelerated replay data time
    # runs ahead, and a wall-clock window would come back empty.
    latest = latest_reading_ts(session)
    anomalies: dict[str, int] = {}
    if latest is not None:
        cutoff = latest - timedelta(days=7)
        anomalies = dict(session.execute(
            text("""
                SELECT building_id, count(*) FROM anomaly
                WHERE acknowledged = false AND ts > :cutoff
                GROUP BY building_id
            """),
            {"cutoff": cutoff},
        ).all())

    features = []
    for row in load_building_rows(session):
        properties = {
            "id": row.id, "code": row.code, "name": row.name,
            "district": row.district,
            "annual_kwh": row.annual_kwh,
            "annual_kwh_source": row.annual_kwh_source,
            "occupancy_pattern": row.occupancy_pattern,
            "insulation_quality": row.insulation_quality,
            "roof_area_m2": row.roof_area_m2,
            "lat": row.lat, "lon": row.lon,
            "funded": row.id in funded,
            "anomalies": int(anomalies.get(row.id, 0)),
        }
        properties.update(funded.get(row.id, {}))
        features.append({
            "type": "Feature",
            "geometry": row.footprint or {
                "type": "Point", "coordinates": [row.lon, row.lat]
            },
            "properties": properties,
        })

    return {"type": "FeatureCollection", "features": features}


@app.get("/api/v1/metrics/forecast", dependencies=[Depends(require(VIEWER))])
def forecast_metrics(session: Session = Depends(get_session)) -> dict:
    """Which forecaster is in use per building, and how much history it has.

    Surfaces the fallback rule rather than hiding it: a portfolio where the learned
    model wins on only a handful of buildings is telling you the load is close to
    perfectly weekly, which is information, not a failure.
    """
    rows = session.execute(text("""
        SELECT model_version, count(DISTINCT building_id) AS buildings, count(*) AS points
        FROM forecast GROUP BY model_version
    """)).all()

    sources = session.execute(text("""
        SELECT annual_kwh_source, count(*) FROM building GROUP BY annual_kwh_source
    """)).all()

    # How current the forecasts are, in DATA time.
    #
    # Without this the forecast panel can only fail silently: it asks for the last
    # two weeks of readings and draws whatever forecast rows fall inside them, so a
    # refit that has not run for months produces an empty second series and a chart
    # that looks like a bug rather than like stale data. The screen can say which it
    # is only if the numbers are here to say it with.
    newest_forecast = session.execute(text("SELECT max(ts) FROM forecast")).scalar()
    newest_reading = latest_reading_ts(session)
    covered = session.execute(text("""
        SELECT count(DISTINCT building_id) FROM forecast
        WHERE ts >= :cutoff
    """), {"cutoff": (newest_reading - timedelta(hours=336))
           if newest_reading else None}).scalar() if newest_reading else 0

    return {
        "by_model": [
            {"model_version": r[0], "buildings": r[1], "points": r[2]} for r in rows
        ],
        # F3: how many buildings are costed against measured consumption rather than
        # the figure the portfolio fixture shipped with.
        "annual_kwh_source": {row[0]: row[1] for row in sources},
        "newest_forecast_ts": newest_forecast.isoformat() if newest_forecast else None,
        "newest_reading_ts": newest_reading.isoformat() if newest_reading else None,
        # Buildings whose forecast reaches into the window the panel draws. Zero
        # here is the difference between "no model ran" and "the model ran in
        # December and the readings are in May".
        "buildings_covered_recently": int(covered or 0),
    }


@app.get("/api/v1/metrics/anomaly", dependencies=[Depends(require(VIEWER))])
def anomaly_metrics(session: Session = Depends(get_session),
                    limit: int = Query(default=20, ge=1, le=200)) -> dict:
    """Current anomaly load, and the most severe open items."""
    by_severity = session.execute(text("""
        SELECT severity, count(*) FROM anomaly GROUP BY severity
    """)).all()

    worst = session.execute(text("""
        SELECT a.building_id, b.code, a.ts, a.observed_kw, a.expected_kw,
               a.robust_z, a.severity
        FROM anomaly a JOIN building b ON b.id = a.building_id
        WHERE NOT a.acknowledged
        ORDER BY abs(a.robust_z) DESC NULLS LAST
        LIMIT :limit
    """), {"limit": limit}).all()

    params = load_params()
    return {
        "threshold_k": params.anomaly_k,
        "window_days": params.anomaly_window_days,
        "by_severity": {row[0]: row[1] for row in by_severity},
        "worst": [
            {
                "building_id": r[0], "building_code": r[1], "ts": r[2].isoformat(),
                "observed_kw": r[3], "expected_kw": r[4],
                "robust_z": None if r[5] != r[5] else r[5], "severity": r[6],
            }
            for r in worst
        ],
    }


class AcknowledgeRequest(BaseModel):
    """Selectors for a bulk acknowledgement. At least one is required.

    An unfiltered call would close every open alert in the portfolio, which is not a
    thing anyone should be able to do by forgetting a field - so omitting every
    selector is still refused. Emptying the whole inbox is a real operation, though,
    and `all_open` is how a caller says it meant to: a field that has to be set on
    purpose cannot be set by leaving something out.
    """

    all_open: bool = Field(
        default=False,
        description="Close every open alert in the portfolio. Only honoured when no "
                    "other selector is given, and never the result of an omission.",
    )
    ids: list[int] | None = None
    building_id: str | None = None
    before: datetime | None = Field(
        default=None,
        description="Acknowledge anomalies older than this DATA timestamp. Under "
                    "accelerated replay this is not the wall clock - use the value "
                    "from /health.",
    )
    severity: str | None = None
    acknowledged: bool = Field(
        default=True,
        description="False un-acknowledges, which is what makes a bulk close safe.",
    )

    def selectors(self) -> dict:
        return {
            "ids": self.ids,
            "building_id": self.building_id,
            "before": self.before,
            "severity": self.severity,
        }


@app.post("/api/v1/anomalies/acknowledge", dependencies=[Depends(require(ANALYST))])
def acknowledge(
    request: AcknowledgeRequest, session: Session = Depends(get_session)
) -> dict:
    """Close alerts in bulk. The inbox has to be emptiable or it is not an inbox.

    `acknowledged` has been a column since Phase 2 and two queries filter on it, but
    nothing could set it: the map counted open anomalies and offered no way to clear
    one. Sixteen months of replay had accumulated eleven thousand critical alerts
    that no operator could act on, which makes the alert count meaningless - the
    number only ever goes up, so nobody reads it.
    """
    selectors = request.selectors()
    if not any(value for value in selectors.values()) and not request.all_open:
        raise HTTPException(
            422,
            "provide at least one of ids, building_id, before or severity - or set "
            "all_open to close the whole inbox deliberately; an unfiltered "
            "acknowledge would close every alert in the portfolio",
        )
    if request.severity and request.severity not in SEVERITIES:
        raise HTTPException(422, f"unknown severity; choose from {sorted(SEVERITIES)}")

    changed = acknowledge_anomalies(
        session, acknowledged=request.acknowledged, **selectors
    )
    session.commit()
    return {
        "changed": changed,
        "acknowledged": request.acknowledged,
        "open_remaining": open_anomaly_count(session, request.building_id),
    }


@app.post("/api/v1/anomalies/{anomaly_id}/acknowledge",
          dependencies=[Depends(require(ANALYST))])
def acknowledge_one(anomaly_id: int, session: Session = Depends(get_session)) -> dict:
    """Close one alert - what the map's popup calls when an operator dismisses it."""
    changed = acknowledge_anomalies(session, ids=[anomaly_id])
    if not changed:
        # Either it does not exist or it was already closed. Both mean "nothing to do
        # here", and distinguishing them would leak which ids exist.
        raise HTTPException(404, f"no open anomaly {anomaly_id}")
    session.commit()
    return {"changed": changed, "anomaly_id": anomaly_id}


@app.post("/api/v1/candidates/recompute", dependencies=[Depends(require(ADMIN))])
def recompute_candidates(session: Session = Depends(get_session)) -> dict:
    """Rebuild the candidate set from the current portfolio, catalog and parameters.

    Call this after Nada edits `data/`. It drops the cached context so the next
    `/optimize` reflects the change, and writes the new set to the database so a
    stored run remains explainable after the fact.

    The build happens under `_context_lock`, not just the assignment. Reset then
    building outside the lock let a concurrent cold request interleave its own
    rebuild between them - and this endpoint's unconditional store then clobbered a
    context some request was already solving against, un-pairing the cached context
    from the candidate set it was materialising. That invariant is the reason this
    endpoint exists, so it is the one thing that must not race.
    """
    with _context_lock:
        context = OptimizerContext.from_db(session)
        global _context
        _context = context

    written = materialize_candidates(session, context.candidates, context.inputs_hash)
    return {
        "buildings": len(context.buildings),
        "interventions": len(context.catalog),
        "candidates": written,
        "inputs_hash": context.inputs_hash[:16],
        "uncited_catalog_rows": [iv.id for iv in context.catalog if iv.needs_citation],
    }


@app.get("/api/v1/narrative/building-specific", dependencies=[Depends(require(VIEWER))])
def building_specific_narrative(
    context: OptimizerContext = Depends(get_context),
) -> dict:
    """Two real buildings whose best measure differs, and the attributes that drive it.

    This replaces the claim made in the proposal's Table 1 - that life-cycle scoring
    reverses solar and insulation - which does not survive contact with the corrected
    model. Measured on this catalog, rooftop generation never has the best benefit
    density at any building: cheap controls and lighting dominate it everywhere, which
    is the ordinary efficiency-before-generation loading order rather than a defect.

    The claim that DOES hold, and that this endpoint evidences, is the more useful
    one: the right measure is building-specific, so a single portfolio-wide priority
    list is wrong and per-building optimization is the point.
    """
    best: dict[str, object] = {}
    for c in context.candidates:
        if len(c.intervention_ids) != 1:
            continue
        current = best.get(c.building_id)
        if current is None or c.score_per_kegp > current.score_per_kegp:
            best[c.building_id] = c

    by_building = {b.id: b for b in context.buildings}
    groups: dict[str, list] = {}
    for building_id, candidate in best.items():
        groups.setdefault(candidate.intervention_ids[0], []).append(building_id)

    ranked = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    if len(ranked) < 2:
        raise HTTPException(
            409, "every building has the same best measure; there is no contrast to show"
        )

    def describe(building_id: str) -> dict:
        building = by_building[building_id]
        winner = best[building_id]
        options = sorted(
            (c for c in context.candidates
             if c.building_id == building_id and len(c.intervention_ids) == 1),
            key=lambda c: c.score_per_kegp, reverse=True,
        )
        return {
            "code": building.code,
            "occupancy_pattern": building.occupancy_pattern,
            "insulation_quality": building.insulation_quality,
            "hvac_age_yr": building.hvac_age_yr,
            "roof_area_m2": building.roof_area_m2,
            "annual_kwh": building.annual_kwh,
            "best": winner.intervention_ids[0],
            "options": [
                {
                    "intervention": c.intervention_ids[0],
                    "label": c.label,
                    "cost_egp": c.cost_egp,
                    "score_per_kegp": c.score_per_kegp,
                }
                for c in options
            ],
        }

    # The largest building in each of the most common groups, up to four: bigger
    # buildings make the contrast legible rather than marginal.
    #
    # ONE PER GROUP, and never more groups than exist. The whole point of the
    # dialog is that these buildings disagree about which measure wins, so two
    # cards with the same winner would be an illustration of the opposite claim.
    # If the catalog ever collapses to a single winner the endpoint 409s above
    # rather than showing a contrast that is not there.
    picks = []
    for _intervention, ids in ranked[:4]:
        picks.append(max(ids, key=lambda i: by_building[i].annual_kwh))

    return {
        "distribution": {k: len(v) for k, v in ranked},
        "buildings": [describe(i) for i in picks],
    }


@app.get("/api/v1/buildings/{building_id}/candidates", dependencies=[Depends(require(VIEWER))])
def building_candidates(
    building_id: str,
    session: Session = Depends(get_session),
    context: OptimizerContext = Depends(get_context),
    run_id: str | None = Query(default=None),
) -> dict:
    """Every option considered at one building, and which one a run chose.

    This is the "why was this building funded, and why this measure" view. A
    recommendation a reviewer cannot interrogate is a recommendation they are being
    asked to take on trust, which is the opposite of the point.
    """
    building = next((b for b in context.buildings if b.id == building_id), None)
    if building is None:
        raise HTTPException(404, f"no building {building_id}")

    chosen_key = None
    if run_id:
        try:
            _run, items = get_run(session, run_id)
        except KeyError as exc:
            raise HTTPException(404, f"no run {run_id}") from exc
        chosen_key = next(
            (i.candidate_key for i in items if i.building_id == building_id), None
        )

    lives = {iv.id: iv.service_life_yr for iv in context.catalog}
    options = sorted(
        (c for c in context.candidates if c.building_id == building_id),
        key=lambda c: c.score_per_kegp,
        reverse=True,
    )

    return {
        "building": {
            "id": building.id, "code": building.code, "name": building.name,
            "district": building.district,
            "occupancy_pattern": building.occupancy_pattern,
            "insulation_quality": building.insulation_quality,
            "hvac_age_yr": building.hvac_age_yr,
            # The age drives the condition multiplier; the type is what an
            # engineer reads it against, and the evidence panel names both when
            # it explains why one option beat the others at this building.
            "hvac_type": building.hvac_type,
            "roof_area_m2": building.roof_area_m2,
            "floor_area_m2": building.floor_area_m2,
            "annual_kwh": building.annual_kwh,
        },
        "chosen_key": chosen_key,
        "options": [
            {
                "key": c.key,
                "label": c.label,
                "intervention_ids": list(c.intervention_ids),
                "cost_egp": c.cost_egp,
                "annual_kwh_saving": c.annual_kwh_saving,
                "annual_egp_saving": c.annual_egp_saving,
                "lifetime_benefit_kgco2e": c.lifetime_benefit_kgco2e,
                "score_per_kegp": c.score_per_kegp,
                "chosen": c.key == chosen_key,
                # Shortest service life in the bundle, which is the one that decides
                # how often the option has to be bought again inside the 30-year
                # horizon. The evidence panel states it beside the lifetime carbon so
                # a reader can see WHY a cheap option with a ten-year life does not
                # beat a dearer one that lasts thirty.
                "service_life_yr": min(
                    (lives[i] for i in c.intervention_ids if i in lives), default=None
                ),
            }
            for c in options
        ],
    }


@app.get("/api/v1/buildings/{building_id}/series", dependencies=[Depends(require(VIEWER))])
def building_series(
    building_id: str,
    session: Session = Depends(get_session),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=2000, ge=1, le=20000),
) -> dict:
    rows = read_series(session, building_id, start, end, limit)
    return {
        "building_id": building_id,
        "points": [{"ts": r.ts.isoformat(), "kw": r.kw} for r in rows],
    }


# ---------------------------------------------------------------------------
# the core deliverable
# ---------------------------------------------------------------------------


@app.post("/api/v1/optimize", response_model=OptimizeResponse, dependencies=[Depends(require(ANALYST))])
def optimize(
    request: OptimizeRequest,
    session: Session = Depends(get_session),
    context: OptimizerContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> OptimizeResponse:
    if request.objective not in OBJECTIVES:
        raise HTTPException(422, f"unknown objective; choose from {sorted(OBJECTIVES)}")
    if request.solver not in SOLVERS:
        raise HTTPException(422, f"unknown solver; choose from {sorted(SOLVERS)}")

    with solve_slot(principal):
        run_id, allocation = run_optimization(
            session, context, request.budget_egp,
            objective=request.objective, solver=request.solver,
            max_funded_per_district=request.max_funded_per_district,
            persist=request.persist,
            max_seconds=request.max_seconds,
        )
    return OptimizeResponse.build(run_id if request.persist else None, allocation)


def _comparable(value: float | None) -> float | None:
    """A percentage that can be put on a screen, or None.

    `improvement_pct` reports an undefined comparison as infinity, which is the
    right answer for the CLI and the claims harness: the baseline achieved
    nothing, so the ratio has no value. It cannot travel to a browser. Pydantic
    serialises a non-finite float as `null`, which is indistinguishable from the
    `null` this endpoint puts on the baseline's own row - so at any budget where
    an equal split buys nothing, every method in the table reported itself as the
    baseline and the comparison column said nothing at all.

    Undefined is returned as None with a companion flag, so the client can say
    which of the two kinds of "no number" it is looking at.
    """
    if value is None:
        return None
    return value if math.isfinite(value) else None


@app.post("/api/v1/compare", dependencies=[Depends(require(ANALYST))])
def compare_solvers(
    request: OptimizeRequest,
    session: Session = Depends(get_session),
    context: OptimizerContext = Depends(get_context),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Every solver on one instance - the headline comparison, in one call."""
    if request.objective not in OBJECTIVES:
        raise HTTPException(422, f"unknown objective; choose from {sorted(OBJECTIVES)}")

    results = {}
    # One slot for the whole comparison, not one per solver: this endpoint runs every
    # solver on the instance, so it is the most expensive request the API serves.
    with solve_slot(principal):
        for solver in SOLVERS:
            _run_id, allocation = run_optimization(
                session, context, request.budget_egp,
                objective=request.objective, solver=solver,
                max_funded_per_district=request.max_funded_per_district,
                persist=False,
                max_seconds=request.max_seconds,
            )
            results[solver] = allocation

    baseline = results["equal_split"]
    return {
        "budget_egp": request.budget_egp,
        "objective": request.objective,
        "max_funded_per_district": request.max_funded_per_district,
        "results": {
            name: {
                "status": allocation.status,
                "buildings_funded": allocation.buildings_funded,
                "total_cost_egp": allocation.total_cost_egp,
                "total_benefit_kgco2e": allocation.total_benefit_kgco2e,
                "total_kwh_saving": allocation.total_kwh_saving,
                "solve_ms": allocation.solve_ms,
                "improvement_vs_equal_split_pct": (
                    None if name == "equal_split"
                    else _comparable(
                        improvement_pct(allocation, baseline, request.objective)
                    )
                ),
            }
            for name, allocation in results.items()
        },
        # The honest headline. Plain greedy never revisits a funded building, so above
        # roughly 16 M EGP it stops spending and any gap measured against it is its
        # saturation rather than the value of exact optimization. Both are returned so
        # the difference is visible instead of a matter of which key was chosen.
        "cpsat_vs_greedy_upgrade_pct": _comparable(improvement_pct(
            results["cpsat"], results["greedy_upgrade"], request.objective
        )),
        "cpsat_vs_greedy_pct": _comparable(improvement_pct(
            results["cpsat"], results["greedy"], request.objective
        )),
        # Whether the status quo achieved anything at all. Without this the client
        # sees a null and cannot tell "this row IS the baseline" from "the baseline
        # scored zero, so a percentage against it has no meaning".
        "equal_split_scored": baseline.buildings_funded > 0,
    }


@app.get("/api/v1/runs/{run_id}", dependencies=[Depends(require(VIEWER))])
def read_run(run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        run, items = get_run(session, run_id)
    except KeyError as exc:
        raise HTTPException(404, f"no run {run_id}") from exc

    return {
        "run_id": run.id,
        "created_at": run.created_at.isoformat(),
        "budget_egp": run.budget_egp,
        "objective": run.objective,
        "solver": run.solver,
        "status": run.status,
        "buildings_funded": run.buildings_funded,
        "total_cost_egp": run.total_cost_egp,
        "total_benefit_kgco2e": run.total_benefit_kgco2e,
        "solve_ms": run.solve_ms,
        "max_funded_per_district": run.max_funded_per_district,
        # The FULL 64 characters, not a prefix.
        #
        # This hash is the provenance of the allocation: it covers the fifty building
        # records, their consumption figures, the budget, the objective, the district
        # cap and the method, so the same hash means the same allocation and a
        # different one means an input moved and the two runs are not comparable. A
        # truncated hash cannot be checked against anything, which leaves the claim
        # unverifiable - and the interface shows it in full for exactly that reason.
        "inputs_hash": run.inputs_hash,
        "items": [
            {"building_code": i.building_code, "district": i.district, "label": i.label,
             "cost_egp": i.cost_egp, "annual_kwh_saving": i.annual_kwh_saving}
            for i in items
        ],
    }


# ---------------------------------------------------------------------------
# evidence and procurement
# ---------------------------------------------------------------------------


def _claims_csv_path() -> Path | None:
    """Where the claims harness last wrote, if it has written anywhere.

    `python -m gemp.evaluate` writes out/evaluation/claims.csv relative to the
    checkout; the container image ships neither the file nor the harness run, so a
    deployment that has never measured reports that honestly rather than serving an
    empty table that looks like data.
    """
    candidates = [
        Path("out") / "evaluation" / "claims.csv",
        Path(__file__).resolve().parents[3] / "out" / "evaluation" / "claims.csv",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


@app.get("/api/v1/jobs", dependencies=[Depends(require(ADMIN))])
def job_status_all() -> dict:
    """What the maintenance jobs are doing, so a screen can show it."""
    return {"jobs": jobs.all_status()}


@app.get("/api/v1/jobs/{kind}", dependencies=[Depends(require(ADMIN))])
def job_status(kind: str) -> dict:
    if kind not in jobs.JOB_COMMANDS:
        raise HTTPException(404, f"no job {kind}")
    return jobs.status(kind)


@app.post("/api/v1/jobs/{kind}", status_code=202,
          dependencies=[Depends(require(ADMIN))])
def job_start(kind: str, request: Request) -> dict:
    """Start a maintenance job, unless one is already running.

    Admin only, and one at a time across the process. Both of these jobs are
    minutes of CPU and both write; letting them overlap produces numbers that
    describe no moment that ever existed, and letting anybody start them on
    request is a denial of service with a button on it.

    409 rather than a queue when the slot is taken: a queue would let somebody
    hold the button down and book an hour of work.
    """
    if kind not in jobs.JOB_COMMANDS:
        raise HTTPException(404, f"no job {kind}")

    principal: Principal | None = getattr(request.state, "principal", None)
    username = principal.username if principal else "unknown"

    started, state = jobs.start(kind, username)
    if not started:
        raise HTTPException(
            409,
            f"{state['label']} is already running; wait for it to finish",
        )
    log.info("job %s started by %s", kind, username)
    return state


@app.get("/api/v1/evidence/claims", dependencies=[Depends(require(VIEWER))])
def evidence_claims() -> dict:
    """The claims harness's own output, on screen.

    Every sentence the paper makes is re-measured by `python -m gemp.evaluate`, and
    until now the result lived in a terminal. Serving the parsed CSV puts the
    project's strongest differentiator - claims measured against the running
    system, with gates - where a judge can read it without a shell.
    """
    path = _claims_csv_path()
    if path is None:
        return {
            "claims": [], "source": None, "measured_at": None,
            "note": "No measurement found. Run: python -m gemp.evaluate",
        }

    import csv as _csv

    rows = []
    with path.open(encoding="utf-8", newline="") as fh:
        for record in _csv.DictReader(fh):
            rows.append({
                "id": record.get("id", ""),
                "verdict": record.get("verdict", ""),
                "known_open": (record.get("known_open", "") or "").strip() != "",
                "statement": record.get("statement", ""),
                "measured": record.get("measured", ""),
                "detail": record.get("detail", ""),
            })
    # When the harness last ran, from the file it wrote.
    #
    # A claims table with no date on it is the one failure mode this screen has:
    # every row can read PASS while describing a system that has since changed.
    # The verdicts are only worth anything next to the moment they were measured.
    measured_at = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat()

    return {
        "claims": rows,
        "source": str(path),
        "measured_at": measured_at,
        "note": "",
    }


@app.get("/api/v1/runs/{run_id}/boq", dependencies=[Depends(require(VIEWER))])
def run_boq(run_id: str, session: Session = Depends(get_session),
            context: OptimizerContext = Depends(get_context)) -> dict:
    """A stored allocation as a bill of quantities.

    A funded line in the UI says what to do ("LED lighting at School X"); a tender
    document needs DIMENSIONS - how many square metres of roof, how many kWp - and
    unit rates, so a quantity surveyor can price the same work independently. The
    dimension mapping is exactly `_dimension` from the costing model, because two
    ways to count square metres is how a BOQ ends up disagreeing with the numbers
    beside it.

    Citation status rides per line: a rate still marked TODO(Nada) must be visible
    to whoever turns this into a procurement document.
    """
    try:
        _run, items = get_run(session, run_id)
    except KeyError as exc:
        raise HTTPException(404, f"no run {run_id}") from exc

    buildings = {b.id: b for b in context.buildings}
    catalog = {iv.id: iv for iv in context.catalog}
    params = context.params

    lines = []
    for item in items:
        building = buildings.get(item.building_id)
        if building is None:
            continue
        for iv_id in item.intervention_ids:
            iv = catalog.get(iv_id)
            if iv is None:
                continue
            if iv.cost_type == "solar":
                qty = solar_kwp(building, params)
                unit = f"{params.solar.solar_fixed_egp:,.0f} EGP fixed + " \
                       f"{params.solar.solar_egp_per_kwp:,.0f} EGP/kWp"
                basis = "kWp"
            else:
                qty = _dimension(building, iv.cost_type, params)
                unit = f"{iv.cost_value:,.0f} EGP per {iv.cost_type.removeprefix('per_').replace('_', ' ')}"
                basis = iv.cost_type.removeprefix("per_").replace("_m2", " m²")
            lines.append({
                "building_code": item.building_code,
                "building_name": building.name,
                "district": item.district,
                "intervention_id": iv.id,
                "measure": iv.label,
                "end_use": iv.end_use,
                "quantity": round(qty, 2),
                "basis": basis,
                "unit_rate": unit,
                "line_total_egp": round(upfront_cost_egp(building, iv, params), 2),
                "service_life_yr": iv.service_life_yr,
                "citation": None if not iv.needs_citation else iv.source_ref,
            })

    return {
        "run_id": run_id,
        "inputs_hash": _run.inputs_hash,
        "objective": _run.objective,
        "solver": _run.solver,
        "lines": lines,
        "total_egp": round(sum(line["line_total_egp"] for line in lines), 2),
    }


# ---------------------------------------------------------------------------
# integrity
# ---------------------------------------------------------------------------


@app.get("/api/v1/integrity/verify/{building_id}", dependencies=[Depends(require(VIEWER))])
def verify(building_id: str, session: Session = Depends(get_session)) -> dict:
    """Walk one building's hash chain and report the first break.

    This endpoint is the demonstration: edit a row in psql, call it, watch it name the
    row and say whether it was modified or deleted.
    """
    return verify_building_chain(session, building_id, get_settings().key_bytes)
