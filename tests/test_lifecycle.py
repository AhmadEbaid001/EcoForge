"""F2 - life-cycle accounting on a common horizon."""

from __future__ import annotations

import pytest
from tests.conftest import PARAMS_DICT, make_building, make_intervention

from gemp.domain.catalog import Params
from gemp.domain.lifecycle import (
    annuity_factor,
    discounted_embodied,
    lifetime_benefit_kgco2e,
    replacement_cycles,
    upfront_cost_egp,
)


def test_annuity_reduces_to_horizon_when_undiscounted():
    assert annuity_factor(30, 0.0) == 30.0


def test_annuity_is_less_than_horizon_when_discounted():
    assert annuity_factor(30, 0.05) < 30.0


def test_replacement_cycles_are_fractional():
    # A 12-year lamp inside a 30-year horizon is charged 2.5 embodied hits, not 3.
    # Rounding up would penalise short-life measures at horizon boundaries for no
    # physical reason.
    assert replacement_cycles(30, 12) == pytest.approx(2.5)
    assert replacement_cycles(30, 30) == pytest.approx(1.0)
    assert replacement_cycles(30, 15) == pytest.approx(2.0)


def test_undiscounted_embodied_is_linear_in_cycles(params: Params):
    assert discounted_embodied(1000.0, 12, params) == pytest.approx(2500.0)


def test_discounting_reduces_embodied_charge():
    discounted = Params.model_validate(PARAMS_DICT | {"discount_rate": 0.08})
    undiscounted = Params.model_validate(PARAMS_DICT)
    assert discounted_embodied(1000.0, 12, discounted) < discounted_embodied(
        1000.0, 12, undiscounted
    )


def test_benefit_is_energy_minus_embodied(params: Params):
    building = make_building()
    iv = make_intervention(service_life_yr=30, embodied_type="fixed", embodied_value=5_000.0)

    # 10,000 kWh/yr x 30 yr x 0.45 = 135,000 kgCO2e avoided, less 1 cycle of 5,000.
    benefit = lifetime_benefit_kgco2e(10_000.0, building, [iv], params)
    assert benefit == pytest.approx(135_000.0 - 5_000.0)


def test_short_life_intervention_is_charged_more_embodied(params: Params):
    building = make_building()
    long_life = make_intervention(id="a", service_life_yr=30, embodied_value=10_000.0)
    short_life = make_intervention(id="b", service_life_yr=10, embodied_value=10_000.0)

    long_benefit = lifetime_benefit_kgco2e(10_000.0, building, [long_life], params)
    short_benefit = lifetime_benefit_kgco2e(10_000.0, building, [short_life], params)

    # Same annual saving, same embodied per unit: the short-life option is charged
    # three cycles instead of one. This is exactly the effect the proposal's
    # own-service-life basis failed to capture.
    assert long_benefit - short_benefit == pytest.approx(20_000.0)


def test_cost_scales_with_the_declared_basis(params: Params):
    building = make_building(floor_area_m2=2000.0, roof_area_m2=800.0)

    per_floor = make_intervention(cost_type="per_m2_floor", cost_value=100.0)
    per_roof = make_intervention(cost_type="per_m2_roof", cost_value=100.0)
    fixed = make_intervention(cost_type="fixed", cost_value=100.0)

    assert upfront_cost_egp(building, per_floor, params) == pytest.approx(200_000.0)
    assert upfront_cost_egp(building, per_roof, params) == pytest.approx(80_000.0)
    assert upfront_cost_egp(building, fixed, params) == pytest.approx(100.0)


def test_solar_cost_has_a_fixed_component(params: Params):
    """The fixed term is what makes small installations worse per unit benefit.

    Without it the benefit density would be scale-invariant, and the per-building
    ranking could never flip - which is the mechanism the corrected F2 narrative
    depends on.
    """
    solar = make_intervention(
        id="pv", end_use="generation", cost_type="solar", saving_frac=0.0,
        embodied_type="per_kwp", embodied_value=1000.0, service_life_yr=20,
    )
    big = make_building(roof_area_m2=2000.0)
    small = make_building(roof_area_m2=200.0)

    big_cost_per_m2 = upfront_cost_egp(big, solar, params) / big.roof_area_m2
    small_cost_per_m2 = upfront_cost_egp(small, solar, params) / small.roof_area_m2

    assert small_cost_per_m2 > big_cost_per_m2
