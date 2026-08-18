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
import os
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gemp.api import auth_routes, dashboard_routes
from gemp.api.deps import get_session
from gemp.auth.deps import (
    ADMIN,
    ANALYST,
    VIEWER,
    check_csrf,
    is_public,
    require,
    resolve_principal,
)
from gemp.config import get_settings
from gemp.domain.catalog import load_params
from gemp.domain.models import Allocation
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


def get_context(session: Session = Depends(get_session)) -> OptimizerContext:
    """Cached candidate set, rebuilt when the portfolio or catalog changes."""
    global _context
    if _context is None:
        _context = OptimizerContext.from_db(session)
    return _context


def reset_context() -> None:
    global _context
    _context = None


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


app = FastAPI(
    title="GEMP",
    description="Budget-constrained retrofit decision support for public building portfolios.",
    version="0.1.0",
    lifespan=lifespan,
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

    needs_auth = path.startswith("/api/") and not is_public(path)

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


@contextmanager
def solve_slot() -> Iterator[None]:
    """Hold one of the solver slots, or refuse the request."""
    if not _solve_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            f"all {MAX_CONCURRENT_SOLVES} solver slots are busy; retry shortly",
            headers={"Retry-After": "2"},
        )
    try:
        yield
    finally:
        _solve_slots.release()


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


@app.get("/health")
def health(session: Session = Depends(get_session)) -> JSONResponse:
    """Per-dependency status, so a failing container says which dependency failed."""
    status = {"api": "ok", "database": "unknown", "readings": None,
              "ingest": _ingest_status()}
    code = 200
    try:
        session.execute(text("SELECT 1"))
        status["database"] = "ok"
        latest = latest_reading_ts(session)
        status["readings"] = latest.isoformat() if latest else None
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        status["database"] = f"error: {type(exc).__name__}"
        code = 503
    return JSONResponse(status, status_code=code)


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

    return {
        "by_model": [
            {"model_version": r[0], "buildings": r[1], "points": r[2]} for r in rows
        ],
        # F3: how many buildings are costed against measured consumption rather than
        # the figure the portfolio fixture shipped with.
        "annual_kwh_source": {row[0]: row[1] for row in sources},
    }


@app.get("/api/v1/metrics/anomaly", dependencies=[Depends(require(VIEWER))])
def anomaly_metrics(session: Session = Depends(get_session),
                    limit: int = Query(default=20, le=200)) -> dict:
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
    thing anyone should be able to do by forgetting a field.
    """

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
    if not any(value for value in selectors.values()):
        raise HTTPException(
            422,
            "provide at least one of ids, building_id, before or severity; an "
            "unfiltered acknowledge would close every alert in the portfolio",
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
    """
    reset_context()
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

    # The largest building in each of the two most common groups: bigger buildings
    # make the contrast legible rather than marginal.
    picks = []
    for _intervention, ids in ranked[:2]:
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
    limit: int = Query(default=2000, le=20000),
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
) -> OptimizeResponse:
    if request.objective not in OBJECTIVES:
        raise HTTPException(422, f"unknown objective; choose from {sorted(OBJECTIVES)}")
    if request.solver not in SOLVERS:
        raise HTTPException(422, f"unknown solver; choose from {sorted(SOLVERS)}")

    with solve_slot():
        run_id, allocation = run_optimization(
            session, context, request.budget_egp,
            objective=request.objective, solver=request.solver,
            max_funded_per_district=request.max_funded_per_district,
            persist=request.persist,
            max_seconds=request.max_seconds,
        )
    return OptimizeResponse.build(run_id if request.persist else None, allocation)


@app.post("/api/v1/compare", dependencies=[Depends(require(ANALYST))])
def compare_solvers(
    request: OptimizeRequest,
    session: Session = Depends(get_session),
    context: OptimizerContext = Depends(get_context),
) -> dict:
    """Every solver on one instance - the headline comparison, in one call."""
    if request.objective not in OBJECTIVES:
        raise HTTPException(422, f"unknown objective; choose from {sorted(OBJECTIVES)}")

    results = {}
    # One slot for the whole comparison, not one per solver: this endpoint runs every
    # solver on the instance, so it is the most expensive request the API serves.
    with solve_slot():
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
                    else improvement_pct(allocation, baseline, request.objective)
                ),
            }
            for name, allocation in results.items()
        },
        # The honest headline. Plain greedy never revisits a funded building, so above
        # roughly 16 M EGP it stops spending and any gap measured against it is its
        # saturation rather than the value of exact optimization. Both are returned so
        # the difference is visible instead of a matter of which key was chosen.
        "cpsat_vs_greedy_upgrade_pct": improvement_pct(
            results["cpsat"], results["greedy_upgrade"], request.objective
        ),
        "cpsat_vs_greedy_pct": improvement_pct(
            results["cpsat"], results["greedy"], request.objective
        ),
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
        "inputs_hash": run.inputs_hash[:16],
        "items": [
            {"building_code": i.building_code, "district": i.district, "label": i.label,
             "cost_egp": i.cost_egp, "annual_kwh_saving": i.annual_kwh_saving}
            for i in items
        ],
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
