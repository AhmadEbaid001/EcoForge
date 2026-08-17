"""One entry point for all three solvers.

Keeping a single signature and a single return type is what makes the evaluation
harness a for-loop over solver names, and the live baseline comparison in the UI a
dropdown rather than a separate code path.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from gemp.domain.models import Allocation, Building, Candidate
from gemp.optimize.baselines import solve_equal_split, solve_greedy, solve_greedy_upgrade
from gemp.optimize.ilp import solve_ilp
from gemp.optimize.objective import OBJECTIVES
from gemp.optimize.prune import prune_dominated

log = logging.getLogger("gemp.optimize")

SOLVERS = {
    "cpsat": solve_ilp,
    "greedy_upgrade": solve_greedy_upgrade,
    "greedy": solve_greedy,
    "equal_split": solve_equal_split,
}


def solve(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    solver: str = "cpsat",
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
    prune: bool = True,
    max_seconds: float | None = None,
) -> Allocation:
    """Run one solver. `max_seconds` applies to CP-SAT; the heuristics ignore it.

    A non-finite budget is rejected here rather than at the API boundary alone,
    because `int(round(inf))` raises deep inside the model build and surfaces as an
    opaque 500. The CLI and the harness reach this code without passing through
    FastAPI's validation at all.
    """
    if solver not in SOLVERS:
        raise ValueError(f"unknown solver {solver!r}; choose from {sorted(SOLVERS)}")
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}; choose from {sorted(OBJECTIVES)}")
    if not math.isfinite(budget_egp):
        raise ValueError(f"budget must be a finite number, got {budget_egp!r}")
    if budget_egp < 0:
        raise ValueError("budget must not be negative")

    if prune:
        candidates = prune_dominated(candidates, objective)

    kwargs = {}
    if solver == "cpsat" and max_seconds is not None:
        kwargs["max_seconds"] = max_seconds

    allocation = SOLVERS[solver](
        candidates,
        buildings,
        budget_egp,
        objective=objective,
        max_funded_per_district=max_funded_per_district,
        **kwargs,
    )

    # CP-SAT can expire without a feasible solution on a hard instance. Returning an
    # empty allocation would be technically honest and useless: the caller asked how
    # to spend the money. Fall back to the strong heuristic and say so in `status`,
    # so nobody mistakes a fallback for a proven optimum.
    if solver == "cpsat" and not allocation.items and budget_egp > 0:
        fallback = SOLVERS["greedy_upgrade"](
            candidates,
            buildings,
            budget_egp,
            objective=objective,
            max_funded_per_district=max_funded_per_district,
        )
        if fallback.items:
            log.warning(
                "cpsat returned %s with no solution; falling back to greedy_upgrade",
                allocation.status,
            )
            return fallback.model_copy(update={
                "status": f"FALLBACK_FROM_{allocation.status}",
                "solve_ms": allocation.solve_ms + fallback.solve_ms,
            })

    return allocation


def compare(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
    prune: bool = True,
) -> dict[str, Allocation]:
    """Run every solver on the same portfolio, budget and catalog."""
    return {
        name: solve(
            candidates,
            buildings,
            budget_egp,
            solver=name,
            objective=objective,
            max_funded_per_district=max_funded_per_district,
            prune=prune,
        )
        for name in SOLVERS
    }


def improvement_pct(result: Allocation, baseline: Allocation, objective: str) -> float:
    """Percentage improvement of `result` over `baseline` on the given objective.

    The headline number. Undefined against a baseline that achieved nothing, which is
    reported as infinity rather than silently divided by zero.
    """
    from gemp.optimize.objective import objective_fn

    value_of = objective_fn(objective)
    result_total = sum(
        value_of(_as_candidate(i)) for i in result.items
    )
    baseline_total = sum(value_of(_as_candidate(i)) for i in baseline.items)
    if baseline_total == 0:
        return float("inf") if result_total > 0 else 0.0
    return (result_total - baseline_total) / baseline_total * 100.0


def _as_candidate(item) -> Candidate:
    """AllocationItem carries every field the objective functions read."""
    return Candidate(
        key=item.candidate_key,
        building_id=item.building_id,
        district=item.district,
        intervention_ids=item.intervention_ids,
        label=item.label,
        cost_egp=item.cost_egp,
        annual_kwh_saving=item.annual_kwh_saving,
        lifetime_benefit_kgco2e=item.lifetime_benefit_kgco2e,
        annual_egp_saving=item.annual_egp_saving,
    )
