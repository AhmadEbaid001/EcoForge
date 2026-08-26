"""F2 - life-cycle accounting on a common horizon."""

from __future__ import annotations

import pytest
from tests.conftest import PARAMS_DICT, make_building, make_intervention

from gemp.domain.catalog import Params
from gemp.domain.lifecycle import (
    annuity_factor,
    discounted_embodied,
    effective_grid_factor,
    lifetime_benefit_kgco2e,
    lifetime_tou_benefit_kgco2e,
    replacement_cycles,
    upfront_cost_egp,
)

# An evening-peaked TOU profile: cheap solar-heavy midday trough (hours 10-15),
# expensive gas peak after sunset (17-21).
PEAKY_PROFILE = (
    tuple([0.35] * 7) + tuple([0.45] * 3) + tuple([0.28] * 6)
    + (0.55,) + tuple([0.85] * 5) + tuple([0.50] * 2)
)
assert len(PEAKY_PROFILE) == 24


def tou_params() -> Params:
    return Params.model_validate(
        PARAMS_DICT | {"tou": {"profile": list(PEAKY_PROFILE)}}
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


# --- A5: time-of-use marginal carbon weighting -------------------------------


def test_a_flat_shape_yields_the_profile_mean():
    """A flat shape weights every hour equally, so the effective factor IS the
    arithmetic mean of the profile - the mathematical guarantee that buildings
    with no measured data are unaffected in kind, only possibly in level."""
    building = make_building()
    assert effective_grid_factor(building, tou_params()) == pytest.approx(
        sum(PEAKY_PROFILE) / 24
    )


def test_no_tou_profile_reproduces_the_flat_factor_exactly(params: Params):
    shape = tuple(3.0 if h in (17, 18) else 0.5 for h in range(24))
    building = make_building(hourly_shape=shape)
    assert effective_grid_factor(building, params) == pytest.approx(0.45)


def test_shipped_profile_averages_the_flat_factor():
    """Continuity pin on the DATA file: the placeholder profile is scaled so its
    mean equals grid_emission_factor. Delete this block and the flat factor stops
    describing the average carbon of the shipped TOU profile."""
    from gemp.domain.catalog import load_params

    p = load_params()
    assert p.tou is not None
    mean_profile = sum(p.tou.profile) / len(p.tou.profile)
    assert mean_profile == pytest.approx(p.grid_emission_factor, abs=5e-3)


def test_a_peaked_building_is_weighted_toward_the_expensive_hours():
    # Consumption-weighted mean over a shape concentrated at 17-20h lands above
    # the profile's own mean; a midday-only shape lands below it.
    evening = make_building(
        hourly_shape=tuple(4.0 if h in (17, 18, 19, 20) else 1.0 for h in range(24))
    )
    midday = make_building(
        hourly_shape=tuple(4.0 if h in (11, 12, 13, 14) else 1.0 for h in range(24))
    )
    p = tou_params()
    profile_mean = sum(PEAKY_PROFILE) / 24
    assert effective_grid_factor(evening, p) > profile_mean
    assert effective_grid_factor(midday, p) < profile_mean
    assert effective_grid_factor(evening, p) > effective_grid_factor(midday, p)


def test_tou_benefit_equals_flat_benefit_when_no_profile(params: Params):
    building = make_building()
    iv = make_intervention(service_life_yr=30)
    slices = {"hvac": 6_000.0, "lighting": 4_000.0}
    flat = lifetime_benefit_kgco2e(10_000.0, building, [iv], params)
    tou = lifetime_tou_benefit_kgco2e(slices, building, [iv], params)
    assert tou == pytest.approx(flat)


def test_tou_benefits_diverge_by_building_shape_under_the_same_profile():
    """Two buildings with IDENTICAL savings but different shapes earn different
    carbon credit. This is the mechanism by which metered data finally reaches
    the funding decision."""
    iv = make_intervention(id="led", service_life_yr=30, embodied_value=0.0)
    office_like = make_building(
        hourly_shape=tuple(4.0 if 10 <= h <= 15 else 1.0 for h in range(24))
    )
    clinic_like = make_building(
        hourly_shape=tuple(4.0 if 17 <= h <= 21 else 1.0 for h in range(24))
    )
    p = tou_params()
    slices = {"lighting": 10_000.0}
    benefit_day = lifetime_tou_benefit_kgco2e(slices, office_like, [iv], p)
    benefit_night = lifetime_tou_benefit_kgco2e(slices, clinic_like, [iv], p)
    assert benefit_night > benefit_day


def test_hourly_shape_is_normalised_and_validated():
    # Whatever scale arrives, the model stores a mean-1.0 day.
    b = make_building(hourly_shape=[200.0] * 24)
    assert abs(sum(b.hourly_shape) / 24 - 1.0) < 1e-9
    with pytest.raises(ValueError):
        make_building(hourly_shape=[1.0] * 23)
    with pytest.raises(ValueError):
        make_building(hourly_shape=[0.0] * 24)
