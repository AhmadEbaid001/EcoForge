"""F3 - the model that turns a whole-building meter reading into per-intervention savings.

This is the bridge the proposal left unspecified. A meter reports one aggregate
number; the optimizer needs "an HVAC replacement at this building saves X kWh/yr".
Getting from one to the other takes three pieces of information:

    E = annual_kwh  x  end_use_share[occupancy][end_use]  x  saving_frac  x  adj(building)
        \_________/    \___________________________/      \___________/    \__________/
         metered            what fraction of              how much of      how much the
        (or forecast)       consumption is this           that end use     building's own
                            end use                       this removes     condition changes it

Rooftop generation does not fit that shape - it adds supply rather than removing
demand - so it takes a separate path with a fixed-cost term and a self-consumption
cap. Those two details are what make the per-building ranking genuinely flip
between solar and fabric measures (see F2 in the technical review).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from gemp.domain.catalog import Params
from gemp.domain.models import Building, Intervention


class SavingsError(ValueError):
    """Raised when a modelled saving is physically impossible."""


# ---------------------------------------------------------------------------
# rooftop generation
# ---------------------------------------------------------------------------


def solar_kwp(building: Building, params: Params) -> float:
    """Installable capacity, kWp.

    Only a fraction of a roof is usable once setbacks, plant, walkways and shading
    are removed.
    """
    usable_m2 = building.roof_area_m2 * params.solar.usable_roof_frac
    return usable_m2 / params.solar.m2_per_kwp


def solar_generation_kwh(building: Building, params: Params) -> float:
    """Gross annual generation before the self-consumption cap."""
    solar = params.solar
    factor = solar.orientation_factor[building.roof_orientation]
    specific_yield = solar.yield_kwh_per_kwp * factor * (1.0 - solar.soiling_loss)
    return solar_kwp(building, params) * specific_yield


def solar_saving_kwh(building: Building, params: Params) -> float:
    """Annual kWh actually saved by rooftop PV.

    Capped at what the building can consume itself. With no export tariff,
    generation beyond on-site demand delivers no saving, so an oversized roof on a
    low-consumption building clips its benefit while its cost does not clip. That
    asymmetry is one of the three mechanisms that make the best intervention
    building-specific rather than universal.
    """
    generated = solar_generation_kwh(building, params)
    ceiling = building.annual_kwh * params.solar.self_consumption_cap
    return min(generated, ceiling)


def solar_is_capped(building: Building, params: Params) -> bool:
    """True when self-consumption, not roof area, is the binding limit."""
    return solar_generation_kwh(building, params) > (
        building.annual_kwh * params.solar.self_consumption_cap
    )


# ---------------------------------------------------------------------------
# end-use reduction
# ---------------------------------------------------------------------------


def effective_saving_frac(building: Building, iv: Intervention, params: Params) -> float:
    """`saving_frac` after the building-condition multiplier, clamped to [0, 1]."""
    raw = iv.saving_frac * params.adj_multiplier(iv.adj_key, building)
    return min(max(raw, 0.0), 1.0)


def end_use_baseline_kwh(building: Building, end_use: str, params: Params) -> float:
    """Annual consumption attributable to one end use."""
    return building.annual_kwh * params.share(building.occupancy_pattern, end_use)


def bundle_saving_kwh(
    building: Building,
    interventions: Sequence[Intervention],
    params: Params,
) -> float:
    """Annual kWh saved by a set of interventions applied together (F4).

    Savings on a shared end use compose MULTIPLICATIVELY, not additively. Insulation
    reduces cooling load; an HVAC replacement raises COP. Both act on the same
    quantity, E_hvac = Load / COP, so the combined fraction saved is
    1 - (1-a)(1-b), not a + b. Naive addition overstates the benefit by 20-40% on a
    typical fabric-plus-plant bundle.

    Savings across *different* end uses do add, since they draw on disjoint slices
    of consumption.
    """
    by_end_use: dict[str, list[Intervention]] = defaultdict(list)
    for iv in interventions:
        by_end_use[iv.end_use].append(iv)

    total = 0.0
    for end_use, group in by_end_use.items():
        if end_use == "generation":
            # additive across generation units, but there is only ever one in practice
            total += sum(solar_saving_kwh(building, params) for _ in group)
            continue

        baseline = end_use_baseline_kwh(building, end_use, params)
        remaining = 1.0
        for iv in group:
            remaining *= 1.0 - effective_saving_frac(building, iv, params)
        total += baseline * (1.0 - remaining)

    ceiling = building.annual_kwh * params.bundling.cap_total_saving_frac
    return min(total, ceiling)


def intervention_saving_kwh(
    building: Building, iv: Intervention, params: Params
) -> float:
    """Annual kWh saved by a single intervention."""
    return bundle_saving_kwh(building, [iv], params)


# ---------------------------------------------------------------------------
# guard rail
# ---------------------------------------------------------------------------


def assert_physically_possible(
    building: Building, annual_kwh_saving: float, params: Params, context: str = ""
) -> None:
    """Reject a modelled saving that exceeds what the building actually consumes.

    Deliberately an assertion in the recompute path rather than only a unit test.
    A recommendation that saves 130% of a building's consumption is the single most
    embarrassing thing this system could put on a screen during judging, and it is
    the natural failure mode of a mis-entered `saving_frac` or a duplicated bundle.
    """
    ceiling = building.annual_kwh * params.guards.max_building_saving_frac
    if annual_kwh_saving > ceiling + 1e-6:
        raise SavingsError(
            f"{building.code}{' ' + context if context else ''}: modelled saving "
            f"{annual_kwh_saving:,.0f} kWh/yr exceeds {params.guards.max_building_saving_frac:.0%} "
            f"of consumption ({building.annual_kwh:,.0f} kWh/yr). Check saving_frac and "
            f"end_use_share in data/."
        )
