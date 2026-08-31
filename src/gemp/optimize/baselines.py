"""The three baselines the optimizer is measured against (F8).

Reporting an allocation without a baseline says nothing: any allocation looks good
in isolation. These bracket the comparison.

`equal_split` represents the status quo the proposal describes - allocation "driven
by convenience or estimation" rather than measured impact. It is the honest headline
comparison, and the gap is large for a structural reason: dividing 10 million EGP
across fifty buildings leaves 200,000 EGP each, which is below the cost of most
interventions in the catalog, so most of the budget funds nothing at all.

`greedy` is density-ordered first-fit. It is a fast, explainable fallback and it is
what the proposal's heuristic actually described.

`greedy_upgrade` exists because the evaluation harness caught plain greedy being a
straw man, and a straw man is worth less than an honest small gap. This is a
MULTIPLE-CHOICE knapsack: at most one option per building. Plain greedy takes each
building's best-density option and can never revisit it, so once every building holds
its cheapest dense option - about 57 M EGP on this portfolio - it stops spending
entirely. Its benefit then flatlines while the budget grows, and by 30 M EGP it is
beaten by equal split. The resulting "CP-SAT wins by 127%" is an artifact of a
baseline that leaves 60% of the money unspent, not a property of exact optimization.

`greedy_upgrade` adds the obvious repair - keep swapping a funded building up to a
costlier, better option while the money lasts - and is the baseline the headline
number should be quoted against. The claim that survives this is narrower and
defensible: exact optimization wins by a few per cent unconstrained, and decisively
once a policy-style side constraint is added, which no greedy variant can express.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence

from gemp.domain.models import Allocation, Building, Candidate
from gemp.optimize.common import to_items
from gemp.optimize.objective import density, objective_fn


def _density_pass(
    candidates: Sequence[Candidate],
    budget_egp: float,
    objective: str,
    max_funded_per_district: int | None,
) -> tuple[dict[str, Candidate], float, dict[str, int]]:
    """Density-ordered first fit. Returns the picks by building, spend and cap counts."""
    ordered = sorted(candidates, key=lambda c: density(c, objective), reverse=True)
    value_of = objective_fn(objective)

    chosen: dict[str, Candidate] = {}
    spent = 0.0
    district_counts: dict[str, int] = defaultdict(int)

    for c in ordered:
        if value_of(c) <= 0:
            continue
        if c.building_id in chosen:
            continue
        if spent + c.cost_egp > budget_egp:
            continue
        if (
            max_funded_per_district is not None
            and district_counts[c.district] >= max_funded_per_district
        ):
            continue

        chosen[c.building_id] = c
        spent += c.cost_egp
        district_counts[c.district] += 1

    return chosen, spent, district_counts


def solve_greedy(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
) -> Allocation:
    """Benefit-density heuristic: best value per EGP first, skip what does not fit.

    Never revisits a building, so it stops spending once every building holds an
    option - see the module docstring, and `greedy_upgrade` for the repair.
    """
    started = time.perf_counter()
    chosen, _spent, _counts = _density_pass(
        candidates, budget_egp, objective, max_funded_per_district
    )

    return Allocation(
        solver="greedy",
        objective=objective,
        status="HEURISTIC",
        budget_egp=budget_egp,
        items=to_items(list(chosen.values()), buildings),
        solve_ms=(time.perf_counter() - started) * 1000.0,
    )


# Each move strictly increases the objective and the candidate set is finite, so the
# loop terminates on its own. The cap is a guard against a future change to that
# invariant, not a tuning knob.
_MAX_UPGRADE_MOVES = 10_000


def solve_greedy_upgrade(
    candidates: Sequence[Candidate],
    buildings: Sequence[Building],
    budget_egp: float,
    *,
    objective: str = "lca_carbon",
    max_funded_per_district: int | None = None,
) -> Allocation:
    """Density-ordered first fit, then repeatedly spend what is left on the best swap.

    A move is either upgrading a funded building to a costlier option or funding a
    building that first-fit could not afford. Moves are ranked by benefit gained per
    extra EGP - the same density argument as the first pass, applied to the margin -
    and the best affordable one is taken until nothing improves.

    Still a heuristic: it commits to each swap without lookahead, so it can strand
    money that a different pair of swaps would have used. That is the point. It is
    the strongest baseline that remains explainable to a non-specialist, and CP-SAT
    has to beat it rather than beat plain greedy's saturation point.
    """
    started = time.perf_counter()
    value_of = objective_fn(objective)

    chosen, spent, district_counts = _density_pass(
        candidates, budget_egp, objective, max_funded_per_district
    )

    by_building: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_building[c.building_id].append(c)

    for _ in range(_MAX_UPGRADE_MOVES):
        best: tuple[float, Candidate] | None = None

        for building_id, options in by_building.items():
            current = chosen.get(building_id)
            base_value = value_of(current) if current else 0.0
            base_cost = current.cost_egp if current else 0.0

            for option in options:
                gain = value_of(option) - base_value
                if gain <= 0:
                    continue
                extra = option.cost_egp - base_cost
                if spent + extra > budget_egp:
                    continue
                # Funding a NEW building consumes a district slot; upgrading one in
                # place does not, which is why the check sits inside the loop rather
                # than beside the budget test.
                if (
                    current is None
                    and max_funded_per_district is not None
                    and district_counts[option.district] >= max_funded_per_district
                ):
                    continue

                ratio = gain / extra if extra > 0 else float("inf")
                if best is None or ratio > best[0]:
                    best = (ratio, option)

        if best is None:
            break

        option = best[1]
        current = chosen.get(option.building_id)
        spent += option.cost_egp - (current.cost_egp if current else 0.0)
        if current is None:
            district_counts[option.district] += 1
        chosen[option.building_id] = option

    return Allocation(
        solver="greedy_upgrade",
        objective=objective,
        status="HEURISTIC",
        budget_egp=budget_egp,
        items=to_items(list(chosen.values()), buildings),
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
