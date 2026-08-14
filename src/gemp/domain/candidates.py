"""F4 - expansion of interventions and their compatible combinations into candidates.

The proposal says the optimizer may pick "at most one intervention (or a compatible
combination) per building" without saying how a combination is costed or scored.
Rather than adding interaction terms to the programme, every compatible combination
is pre-expanded into its own candidate option, with costs added and savings composed
multiplicatively.

The payoff is structural: with combinations pre-expanded, the solver needs NO
interaction constraints at all. The existing multiple-choice constraint

    sum_i x[b,i] <= 1

already forbids double counting, so the model stays a clean multiple-choice knapsack,
stays linear, and stays explainable to a judge. The interaction physics lives in the
catalog, which is the environmental engineer's domain, rather than in the solver,
which is the systems engineer's - a clean ownership boundary.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence

from gemp.domain.catalog import Params, applies
from gemp.domain.lifecycle import annual_egp_saving, bundle_cost_egp, lifetime_benefit_kgco2e
from gemp.domain.models import Building, Candidate, Intervention
from gemp.domain.savings import assert_physically_possible, bundle_saving_kwh


def compatible(combo: Sequence[Intervention]) -> bool:
    """False when two members compete for the same slot.

    Two HVAC plant replacements are not a bundle, they are a mistake. Membership of
    an `exclusive_group` is the catalog's way of saying so, which keeps the rule in
    the data file rather than hard-coded here.
    """
    groups = [iv.exclusive_group for iv in combo if iv.exclusive_group]
    return len(groups) == len(set(groups))


def applicable_interventions(
    building: Building, catalog: Sequence[Intervention]
) -> list[Intervention]:
    """Catalog rows whose `applies_if` rule this building satisfies."""
    return [iv for iv in catalog if applies(iv, building)]


def expand_building(
    building: Building,
    catalog: Sequence[Intervention],
    params: Params,
    max_bundle_size: int | None = None,
) -> list[Candidate]:
    """Every costed, scored option available at one building."""
    limit = max_bundle_size or params.bundling.max_bundle_size
    applicable = applicable_interventions(building, catalog)

    candidates: list[Candidate] = []
    for size in range(1, min(limit, len(applicable)) + 1):
        for combo in itertools.combinations(applicable, size):
            if not compatible(combo):
                continue

            kwh = bundle_saving_kwh(building, combo, params)
            if kwh <= 0:
                continue

            ids = tuple(iv.id for iv in combo)
            assert_physically_possible(building, kwh, params, context=f"[{'+'.join(ids)}]")

            cost = bundle_cost_egp(building, combo, params)
            if cost <= 0:
                continue

            candidates.append(
                Candidate(
                    key=f"{building.id}|{'+'.join(ids)}",
                    building_id=building.id,
                    district=building.district,
                    intervention_ids=ids,
                    label=" + ".join(iv.label for iv in combo),
                    cost_egp=cost,
                    annual_kwh_saving=kwh,
                    lifetime_benefit_kgco2e=lifetime_benefit_kgco2e(
                        kwh, building, combo, params
                    ),
                    annual_egp_saving=annual_egp_saving(kwh, params),
                )
            )

    return candidates


def expand_portfolio(
    buildings: Iterable[Building],
    catalog: Sequence[Intervention],
    params: Params,
    max_bundle_size: int | None = None,
) -> list[Candidate]:
    """Candidate set for the whole portfolio - the optimizer's input."""
    out: list[Candidate] = []
    for building in buildings:
        out.extend(expand_building(building, catalog, params, max_bundle_size))
    return out


def best_by_density(candidates: Sequence[Candidate], building_id: str) -> Candidate | None:
    """Highest benefit-density option at one building.

    Used for display and for the per-building comparison view, never as the solver's
    objective (F1).
    """
    options = [c for c in candidates if c.building_id == building_id]
    return max(options, key=lambda c: c.score_per_kegp, default=None)
