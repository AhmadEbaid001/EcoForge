"""F2 - life-cycle costing and carbon accounting on a COMMON horizon.

The proposal credited each intervention with savings over its own service life:
20 years for solar, 30 for insulation, 15 for HVAC, 12 for lighting. Options
evaluated over different horizons are not comparable, and that inconsistency - not
embodied carbon - is what produced the ranking reversal presented in the proposal's
Table 1 and Figure 3.

Two rules fix it:

1. Every intervention is credited over the same horizon T (default 30 years).
2. Replacement EMBODIED CARBON is charged for every cycle inside T, but replacement
   CAPITAL is not charged to the current budget. A ministry allocating this year's
   money pays for one installation now; the lamp replacement in year 12 comes out of
   a future budget. Mixing those two puts a cost in the constraint that nobody is
   actually being asked to fund today.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from gemp.domain.catalog import Params
from gemp.domain.models import Building, Intervention
from gemp.domain.savings import solar_kwp

# ---------------------------------------------------------------------------
# discounting
# ---------------------------------------------------------------------------


def annuity_factor(horizon_yr: int, discount_rate: float) -> float:
    """Present-value factor for one unit received annually for `horizon_yr` years.

    Reduces to `horizon_yr` when the discount rate is zero, which is the default and
    keeps the output reconcilable with the undiscounted figures in the proposal.
    """
    if discount_rate == 0.0:
        return float(horizon_yr)
    return sum(1.0 / (1.0 + discount_rate) ** t for t in range(horizon_yr))


def replacement_cycles(horizon_yr: int, service_life_yr: int) -> float:
    """How many times this intervention is purchased inside the horizon.

    Fractional on purpose. A 12-year lamp inside a 30-year horizon is charged 2.5
    embodied hits, not 3. Rounding up would penalise short-life measures at horizon
    boundaries for no physical reason.
    """
    return horizon_yr / service_life_yr


def discounted_embodied(
    embodied_once: float, service_life_yr: int, params: Params
) -> float:
    """Total embodied carbon charged over the horizon, discounted if requested."""
    cycles = replacement_cycles(params.horizon_yr, service_life_yr)
    if params.discount_rate == 0.0:
        return cycles * embodied_once

    full = math.floor(cycles)
    remainder = cycles - full
    total = sum(
        embodied_once / (1.0 + params.discount_rate) ** (j * service_life_yr)
        for j in range(full)
    )
    if remainder > 0:
        total += (
            remainder
            * embodied_once
            / (1.0 + params.discount_rate) ** (full * service_life_yr)
        )
    return total


# ---------------------------------------------------------------------------
# cost and embodied models
# ---------------------------------------------------------------------------


def _dimension(building: Building, cost_or_embodied_type: str, params: Params) -> float:
    """The quantity a per-unit price is multiplied by."""
    match cost_or_embodied_type:
        case "fixed":
            return 1.0
        case "per_m2_floor":
            return building.floor_area_m2
        case "per_m2_roof":
            return building.roof_area_m2
        case "per_m2_glazing":
            return building.glazing_area_m2
        case "per_kwp":
            return solar_kwp(building, params)
        case _:
            raise ValueError(f"unknown unit basis {cost_or_embodied_type!r}")


def upfront_cost_egp(building: Building, iv: Intervention, params: Params) -> float:
    """Capital cost payable now, EGP. This is what competes for the budget.

    Solar takes the fixed-plus-per-kWp model from params.yaml. The fixed term matters:
    inverters, mounting, grid connection and permitting do not scale with array size,
    so small installations are markedly worse per unit benefit. Under a purely
    proportional cost model the benefit density would be scale-invariant and could
    never flip between buildings.
    """
    if iv.cost_type == "solar":
        return params.solar.solar_fixed_egp + params.solar.solar_egp_per_kwp * solar_kwp(
            building, params
        )
    return iv.cost_value * _dimension(building, iv.cost_type, params)


def embodied_once_kgco2e(building: Building, iv: Intervention, params: Params) -> float:
    """Embodied carbon of a single installation, kgCO2e."""
    return iv.embodied_value * _dimension(building, iv.embodied_type, params)


def bundle_cost_egp(
    building: Building, interventions: Sequence[Intervention], params: Params
) -> float:
    """Costs ARE additive, unlike savings (F4)."""
    return sum(upfront_cost_egp(building, iv, params) for iv in interventions)


# ---------------------------------------------------------------------------
# benefit
# ---------------------------------------------------------------------------


def lifetime_benefit_kgco2e(
    annual_kwh_saving: float,
    building: Building,
    interventions: Sequence[Intervention],
    params: Params,
) -> float:
    """Net life-cycle carbon benefit V, kgCO2e.

        V = (annual kWh saved) x (annuity factor) x (grid factor)
            - sum over members of (embodied per cycle x cycles inside the horizon)

    Note the energy term uses the BUNDLE's combined saving, which has already been
    composed multiplicatively, while the embodied term is summed per member because
    each member carries its own service life and therefore its own replacement count.
    """
    energy = (
        annual_kwh_saving
        * annuity_factor(params.horizon_yr, params.discount_rate)
        * params.grid_emission_factor
    )
    embodied = sum(
        discounted_embodied(
            embodied_once_kgco2e(building, iv, params), iv.service_life_yr, params
        )
        for iv in interventions
    )
    return energy - embodied


def annual_egp_saving(annual_kwh_saving: float, params: Params) -> float:
    """Money saved per year at the current tariff. Used by the `egp_saved` objective."""
    return annual_kwh_saving * params.electricity_tariff_egp_per_kwh


def simple_payback_yr(
    annual_kwh_saving: float, cost_egp: float, params: Params
) -> float | None:
    """Undiscounted payback, years. `None` when there is no saving to pay it back."""
    annual = annual_egp_saving(annual_kwh_saving, params)
    if annual <= 0:
        return None
    return cost_egp / annual
