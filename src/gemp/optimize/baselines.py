"""The two baselines the optimizer is measured against (F8).

Reporting an allocation without a baseline says nothing: any allocation looks good
in isolation. These two bracket the comparison.

`equal_split` represents the status quo the proposal describes - allocation "driven
by convenience or estimation" rather than measured impact. It is the honest headline
comparison, and the gap is large for a structural reason: dividing 10 million EGP
across fifty buildings leaves 200,000 EGP each, which is below the cost of most
interventions in the catalog, so most of the budget funds nothing at all.

`greedy` is the strong baseline. On an unconstrained knapsack it lands within a few
per cent of optimal, and pretending otherwise would not survive questioning. It is
kept because it is genuinely useful - a fast, explainable fallback - and because
reporting a small gap honestly is better evidence of rigour than a large gap that
does not withstand a second look.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence

from gemp.domain.models import Allocation, Building, Candidate
from gemp.optimize.common import to_items
from gemp.optimize.objective import density, objective_fn


def solve_greedy(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
) -> Allocation:
    """Benefit-density heuristic: best value per EGP first, skip what does not fit."""
    started = time.perf_counter()

    ordered = sorted(candidates, key=lambda c: density(c, objective), reverse=True)
    value_of = objective_fn(objective)

    picks: list[Candidate] = []
    spent = 0.0
    used_buildings: set[str] = set()
    district_counts: dict[str, int] = defaultdict(int)

    for c in ordered:
        if value_of(c) <= 0:
            continue
        if c.building_id in used_buildings:
            continue
        if spent + c.cost_egp > budget_egp:
            continue
        if (
            max_funded_per_district is not None
            and district_counts[c.district] >= max_funded_per_district
        ):
            continue

        picks.append(c)
        spent += c.cost_egp
        used_buildings.add(c.building_id)
        district_counts[c.district] += 1

    return Allocation(
        solver="greedy",
        objective=objective,
        status="HEURISTIC",
        budget_egp=budget_egp,
        items=to_items(picks, buildings),
        solve_ms=(time.perf_counter() - started) * 1000.0,
    )


def solve_equal_split(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
) -> Allocation:
    """Divide the budget evenly, then let each building buy what it can afford.

    Unspent remainders are NOT pooled. That is not an oversight - it is the defining
    weakness of the policy being modelled. Money sits in fifty small piles, each too
    small to buy anything useful, which is precisely the failure the platform exists
    to fix.
    """
    started = time.perf_counter()

    if not buildings:
        return Allocation(
            solver="equal_split",
            objective=objective,
            status="BASELINE",
            budget_egp=budget_egp,
            items=[],
        )

    share = budget_egp / len(buildings)
    value_of = objective_fn(objective)

    by_building: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_building[c.building_id].append(c)

    picks: list[Candidate] = []
    for building in buildings:
        affordable = [
            c
            for c in by_building.get(building.id, [])
            if c.cost_egp <= share and value_of(c) > 0
        ]
        if affordable:
            picks.append(max(affordable, key=value_of))

    if max_funded_per_district is not None:
        capped: list[Candidate] = []
        counts: dict[str, int] = defaultdict(int)
        for c in sorted(picks, key=value_of, reverse=True):
            if counts[c.district] < max_funded_per_district:
                capped.append(c)
                counts[c.district] += 1
        picks = capped

    return Allocation(
        solver="equal_split",
        objective=objective,
        status="BASELINE",
        budget_egp=budget_egp,
        items=to_items(picks, buildings),
        solve_ms=(time.perf_counter() - started) * 1000.0,
    )
