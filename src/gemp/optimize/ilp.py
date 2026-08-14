"""Exact allocation via CP-SAT (F1).

The programme is a multiple-choice knapsack:

    maximize    sum over (b,i) of  V[b,i] * x[b,i]
    subject to  sum over (b,i) of  K[b,i] * x[b,i]  <=  B
                sum over i in I(b) of x[b,i]        <=  1     for every building b
                sum over b in D    of x[b,i]        <=  M_d   for every district d
                x[b,i] in {0,1}

Two things about it are deliberate.

First, the objective is the ABSOLUTE benefit V, not the density V/K. The proposal
maximised a sum of densities, which is degenerate: densities with different
denominators do not add, the objective value carries no unit, and the solution
drifts toward cheap high-ratio items regardless of the benefit actually delivered.
Density survives as the greedy heuristic's sort key, where it belongs.

Second, the district cap is the constraint the greedy heuristic cannot express at
all. On an unconstrained knapsack, density-ordered greedy lands within a few per
cent of optimal, so "exact optimization" earns its place through constraints like
this one and through returning a proven optimum, not through a large margin.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from ortools.sat.python import cp_model

from gemp.domain.models import Allocation, Building, Candidate
from gemp.optimize.common import to_items
from gemp.optimize.objective import objective_fn

# CP-SAT is an integer solver. Costs are already whole EGP; benefits are rounded to
# whole units (kgCO2e, kWh, EGP). At portfolio magnitudes - benefits of 1e5 to 1e7 -
# unit rounding is far below the uncertainty in the catalog's own coefficients.
_MAX_SOLVE_SECONDS = 10.0


def solve_ilp(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
    max_seconds: float = _MAX_SOLVE_SECONDS,
) -> Allocation:
    value_of = objective_fn(objective)

    started = time.perf_counter()
    model = cp_model.CpModel()
    x = {c.key: model.NewBoolVar(c.key) for c in candidates}

    # Budget: upfront capital only. Replacement expenditure in later years is funded
    # from later budgets and must not compete for the money being allocated now.
    model.Add(
        sum(int(round(c.cost_egp)) * x[c.key] for c in candidates) <= int(round(budget_egp))
    )

    # Multiple choice: at most one option per building. Because compatible bundles
    # were pre-expanded into their own candidates, this single constraint also
    # prevents double-counting interacting interventions - no interaction terms
    # needed anywhere in the model.
    by_building: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_building.setdefault(c.building_id, []).append(c)
    for group in by_building.values():
        model.Add(sum(x[c.key] for c in group) <= 1)

    # Policy-style side constraint. Greedy cannot represent this.
    if max_funded_per_district is not None:
        by_district: dict[str, list[Candidate]] = {}
        for c in candidates:
            by_district.setdefault(c.district, []).append(c)
        for group in by_district.values():
            model.Add(sum(x[c.key] for c in group) <= max_funded_per_district)

    model.Maximize(sum(int(round(value_of(c))) * x[c.key] for c in candidates))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    picks: list[Candidate] = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        picks = [c for c in candidates if solver.Value(x[c.key])]

    return Allocation(
        solver="cpsat",
        objective=objective,
        status=solver.StatusName(status),
        budget_egp=budget_egp,
        items=to_items(picks, buildings),
        solve_ms=elapsed_ms,
    )
