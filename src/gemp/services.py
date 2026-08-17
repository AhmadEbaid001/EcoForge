"""Application services: the operations the API and the CLI both need.

Sits between the HTTP layer and the domain layer so that "solve and persist a run"
exists once, rather than once per caller.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from gemp.db import AllocationRow, OptimizationRunRow
from gemp.domain.candidates import expand_portfolio
from gemp.domain.catalog import Params, load_catalog, load_params
from gemp.domain.models import Allocation, Building, Candidate, Intervention
from gemp.ingest.integrity import verify_against_checkpoint, verify_chain
from gemp.optimize.runner import solve
from gemp.repository import load_buildings, read_chain


def inputs_hash(buildings: list[Building], catalog: list[Intervention], params: Params) -> str:
    """Fingerprint of everything that determines a candidate set.

    Stored with each run so a stale candidate set is detectable rather than silently
    reused - the failure mode where the catalog is corrected but the recommendation
    on screen is still the old one.
    """
    payload = {
        "buildings": [
            [b.id, b.annual_kwh, b.roof_area_m2, b.floor_area_m2, b.hvac_age_yr,
             b.insulation_quality, b.occupancy_pattern]
            for b in buildings
        ],
        "catalog": [iv.model_dump(exclude={"notes", "label"}) for iv in catalog],
        "params": params.model_dump(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class OptimizerContext:
    """Portfolio, catalog, params and the candidate set derived from them.

    Candidate expansion costs about 50 ms for fifty buildings, so it is cached and
    invalidated by `inputs_hash` rather than recomputed on every slider movement.
    """

    def __init__(self, buildings: list[Building], catalog: list[Intervention],
                 params: Params):
        self.buildings = buildings
        self.catalog = catalog
        self.params = params
        self.candidates: list[Candidate] = expand_portfolio(buildings, catalog, params)
        self.inputs_hash = inputs_hash(buildings, catalog, params)

    @classmethod
    def from_db(cls, session: Session) -> OptimizerContext:
        return cls(load_buildings(session), load_catalog(), load_params())

    @classmethod
    def from_fixtures(cls) -> OptimizerContext:
        from gemp.domain.portfolio import load_buildings as load_from_geojson

        return cls(load_from_geojson(), load_catalog(), load_params())


def run_optimization(
    session: Session,
    context: OptimizerContext,
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    solver: str = "cpsat",
    max_funded_per_district: int | None = None,
    persist: bool = True,
    max_seconds: float | None = None,
) -> tuple[str, Allocation]:
    """Solve, optionally store the run, and return (run_id, allocation)."""
    if max_funded_per_district is None:
        max_funded_per_district = context.params.constraints.max_funded_per_district

    allocation = solve(
        context.candidates,
        context.buildings,
        budget_egp,
        solver=solver,
        objective=objective,
        max_funded_per_district=max_funded_per_district,
        max_seconds=max_seconds,
    )

    run_id = str(uuid.uuid4())
    if persist:
        # The run row is flushed before its allocations. `allocation.run_id` is a
        # foreign key to `optimization_run.id`, and relying on the unit of work to
        # order the two inserts was not enough here: the allocations went first and
        # the whole transaction died on a foreign-key violation at commit, which is
        # AFTER the response has been built. The endpoint therefore returned a
        # perfectly good run_id for a run that did not exist, and every later request
        # for it 404'd - the map went blank with no error anywhere the caller could
        # see. An explicit flush makes the ordering a property of this code rather
        # than of SQLAlchemy's dependency sort.
        session.add(OptimizationRunRow(
            id=run_id,
            created_at=datetime.now(UTC),
            budget_egp=budget_egp,
            objective=objective,
            solver=solver,
            status=allocation.status,
            max_funded_per_district=max_funded_per_district,
            objective_value=allocation.total_benefit_kgco2e,
            total_cost_egp=allocation.total_cost_egp,
            total_kwh_saving=allocation.total_kwh_saving,
            total_benefit_kgco2e=allocation.total_benefit_kgco2e,
            buildings_funded=allocation.buildings_funded,
            solve_ms=allocation.solve_ms,
            inputs_hash=context.inputs_hash,
        ))
        session.flush()
        session.add_all([
            AllocationRow(
                run_id=run_id,
                building_id=item.building_id,
                building_code=item.building_code,
                district=item.district,
                candidate_key=item.candidate_key,
                intervention_ids=list(item.intervention_ids),
                label=item.label,
                cost_egp=item.cost_egp,
                annual_kwh_saving=item.annual_kwh_saving,
                lifetime_benefit_kgco2e=item.lifetime_benefit_kgco2e,
                annual_egp_saving=item.annual_egp_saving,
            )
            for item in allocation.items
        ])

        # Committed here, not left to the request-scoped session.
        #
        # FastAPI resumes a `yield` dependency AFTER the response has been sent, so
        # the commit would land after the client already holds the run_id. A browser
        # issues its follow-up request faster than that: the map asked for
        # /map/geojson?run_id=... and got a 404 for a run that was moments from
        # existing. curl never reproduced it, because starting a process is slower
        # than the race window.
        #
        # A run_id must not leave this function until the run it names is durable.
        session.commit()

    return run_id, allocation


def get_run(session: Session, run_id: str) -> tuple[OptimizationRunRow, list[AllocationRow]]:
    run = session.get(OptimizationRunRow, run_id)
    if run is None:
        raise KeyError(run_id)
    items = list(session.execute(
        select(AllocationRow).where(AllocationRow.run_id == run_id)
    ).scalars())
    return run, items


def verify_building_chain(session: Session, building_id: str, key: bytes,
                          checkpoint_seq: int | None = None,
                          checkpoint_sig: bytes | None = None) -> dict:
    """Full integrity report for one building."""
    rows = read_chain(session, building_id)
    if not rows:
        return {"building_id": building_id, "rows": 0, "chain_ok": True,
                "checkpoint_ok": None, "break": None}

    walk = verify_chain(key, rows, expect_first_seq=rows[0]["seq"])

    checkpoint_ok = None
    if checkpoint_seq is not None and checkpoint_sig is not None:
        checkpoint_ok = verify_against_checkpoint(rows, checkpoint_seq, checkpoint_sig).ok

    # A break at the very first row means every signature after it would fail too, and
    # by far the most likely cause is the wrong key rather than a modified row: an
    # attacker who can edit the database has no reason to start at row zero. Saying
    # "modified" there sends whoever is debugging to look for tampering that is not
    # in the data. This happened during Phase 4 - the integration test picked up the
    # SQLite suite's test key from the process environment and reported a clean chain
    # as modified at seq 0.
    first_row_failed = (
        walk.first_break is not None
        and walk.first_break.index == 0
        and walk.first_break.reason == "modified"
    )

    return {
        "building_id": building_id,
        "rows": len(rows),
        "chain_ok": walk.ok,
        "checkpoint_ok": checkpoint_ok,
        "break": None if walk.first_break is None else {
            "reason": walk.first_break.reason,
            "seq": walk.first_break.seq,
            "ts": walk.first_break.ts,
        },
        "hint": (
            "the first row itself does not verify, so the signing key is probably "
            "not the one these rows were written with - check GEMP_HMAC_KEY before "
            "concluding the data was modified"
        ) if first_row_failed else None,
    }
