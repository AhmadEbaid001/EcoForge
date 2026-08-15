"""FastAPI application.

One process, several modules - the deployment shape argued for in the review. The
route handlers are deliberately thin; anything with judgement in it lives in
`gemp.domain` or `gemp.services`, which is what keeps the optimizer runnable from a
command line with no web server involved.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gemp.config import get_settings
from gemp.db import get_sessionmaker
from gemp.domain.models import Allocation
from gemp.optimize.objective import OBJECTIVES
from gemp.optimize.runner import SOLVERS, improvement_pct
from gemp.repository import latest_reading_ts, load_building_rows, read_series
from gemp.services import OptimizerContext, get_run, run_optimization, verify_building_chain

log = logging.getLogger("gemp.api")

_context: OptimizerContext | None = None


def get_session() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_context(session: Session = Depends(get_session)) -> OptimizerContext:
    """Cached candidate set, rebuilt when the portfolio or catalog changes."""
    global _context
    if _context is None:
        _context = OptimizerContext.from_db(session)
    return _context


def reset_context() -> None:
    global _context
    _context = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("gemp api starting")
    yield
    log.info("gemp api stopping")


app = FastAPI(
    title="GEMP",
    description="Budget-constrained retrofit decision support for public building portfolios.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class OptimizeRequest(BaseModel):
    budget_egp: float = Field(gt=0, description="Capital available now, EGP")
    objective: str = Field(default="lca_carbon")
    solver: str = Field(default="cpsat")
    max_funded_per_district: int | None = Field(default=None, ge=1)
    persist: bool = True


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
    status = {"api": "ok", "database": "unknown", "readings": None}
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


@app.get("/api/v1/meta")
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


@app.get("/api/v1/buildings")
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


@app.get("/api/v1/map/geojson")
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

    features = []
    for row in load_building_rows(session):
        properties = {
            "id": row.id, "code": row.code, "district": row.district,
            "annual_kwh": row.annual_kwh, "funded": row.id in funded,
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


@app.get("/api/v1/buildings/{building_id}/series")
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


@app.post("/api/v1/optimize", response_model=OptimizeResponse)
def optimize(
    request: OptimizeRequest,
    session: Session = Depends(get_session),
    context: OptimizerContext = Depends(get_context),
) -> OptimizeResponse:
    if request.objective not in OBJECTIVES:
        raise HTTPException(422, f"unknown objective; choose from {sorted(OBJECTIVES)}")
    if request.solver not in SOLVERS:
        raise HTTPException(422, f"unknown solver; choose from {sorted(SOLVERS)}")

    run_id, allocation = run_optimization(
        session, context, request.budget_egp,
        objective=request.objective, solver=request.solver,
        max_funded_per_district=request.max_funded_per_district,
        persist=request.persist,
    )
    return OptimizeResponse.build(run_id if request.persist else None, allocation)


@app.post("/api/v1/compare")
def compare_solvers(
    request: OptimizeRequest,
    session: Session = Depends(get_session),
    context: OptimizerContext = Depends(get_context),
) -> dict:
    """All three solvers on one instance - the headline comparison, in one call."""
    results = {}
    for solver in SOLVERS:
        _run_id, allocation = run_optimization(
            session, context, request.budget_egp,
            objective=request.objective, solver=solver,
            max_funded_per_district=request.max_funded_per_district,
            persist=False,
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
        "cpsat_vs_greedy_pct": improvement_pct(
            results["cpsat"], results["greedy"], request.objective
        ),
    }


@app.get("/api/v1/runs/{run_id}")
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


@app.get("/api/v1/integrity/verify/{building_id}")
def verify(building_id: str, session: Session = Depends(get_session)) -> dict:
    """Walk one building's hash chain and report the first break.

    This endpoint is the demonstration: edit a row in psql, call it, watch it name the
    row and say whether it was modified or deleted.
    """
    return verify_building_chain(session, building_id, get_settings().key_bytes)
