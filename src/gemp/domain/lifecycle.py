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
    """Net life-cycle carbon benefit V, kgCO2e, at the FLAT grid factor.

        V = (annual kWh saved) x (annuity factor) x (grid factor)
            - sum over members of (embodied per cycle x cycles inside the horizon)

    Note the energy term uses the BUNDLE's combined saving, which has already been
    composed multiplicatively, while the embodied term is summed per member because
    each member carries its own service life and therefore its own replacement count.

    Deliberately unchanged by A5. This is the flat-factor benefit the paper's F5/F7
    numbers were measured with; the TOU-weighted counterpart is
    `lifetime_tou_benefit_kgco2e`, carried alongside it so the harness can measure
    old against new instead of silently redefining the old number.
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


# ---------------------------------------------------------------------------
# A5 - time-of-use marginal carbon weighting
# ---------------------------------------------------------------------------


def effective_grid_factor(building: Building, params: Params) -> float:
    """The grid factor THIS building's saved kWh are actually worth, kgCO2e/kWh.

        factor = sum_h( shape[h] * grid[h] ) / sum_h( shape[h] )

    A consumption-weighted mean of the TOU marginal profile over the building's own
    MEASURED hourly shape. An office that empties at 15:00 saves most of its kWh
    before the evening peak; a 24x7 clinic keeps burning through it. Same building,
    same flat national average, genuinely different carbon per saved kWh - which is
    the whole reason a single constant factor could not see metered data.

    With `params.tou` unset this is EXACTLY `params.grid_emission_factor` - the
    pre-A5 world, bit for bit.

    With a profile SET but no measured shape, it is the profile's own mean rather
    than the flat constant, because a flat shape weights every hour equally. The
    shipped profile is scaled so those agree to 0.02%, which is far inside the
    catalog's own uncertainty - but they are not the same number, and a profile
    rescaled without that care would move every shapeless building silently. The
    flat objective is unaffected either way: it never calls this.
    """
    if params.tou is None:
        return params.grid_emission_factor
    weighted = sum(
        s * g for s, g in zip(building.hourly_shape, params.tou.profile, strict=True)
    )
    total = sum(building.hourly_shape)
    return weighted / total if total > 0 else params.grid_emission_factor


def lifetime_tou_benefit_kgco2e(
    saving_by_end_use: dict[str, float],
    building: Building,
    interventions: Sequence[Intervention],
    params: Params,
) -> float:
    """Net life-cycle carbon benefit under the TOU weighting, kgCO2e.

    Identical structure to `lifetime_benefit_kgco2e` - annuity times energy minus
    embodied - except the energy term weights each end-use slice of the saving by
    the building's own consumption-weighted grid factor. The slices come from
    `bundle_saving_by_end_use`, so a future per-end-use load shape only has to
    change the factor lookup here, not the composition upstream.

    When `params.tou` is None this reduces exactly to `lifetime_benefit_kgco2e`
    over the summed saving, which is what lets both numbers coexist on one
    candidate and let the harness measure old against new.
    """
    total_saving = sum(saving_by_end_use.values())
    energy = (
        total_saving
        * annuity_factor(params.horizon_yr, params.discount_rate)
        * effective_grid_factor(building, params)
    )
    embodied = sum(
        discounted_embodied(
            embodied_once_kgco2e(building, iv, params), iv.service_life_yr, params
        )
        for iv in interventions
    )
    return energy - embodied


def simple_payback_yr(
    annual_kwh_saving: float, cost_egp: float, params: Params
) -> float | None:
    """Undiscounted payback, years. `None` when there is no saving to pay it back."""
    annual = annual_egp_saving(annual_kwh_saving, params)
    if annual <= 0:
        return None
    return cost_egp / annual
