"""F3 and F4 - the savings model and how interventions compose."""

from __future__ import annotations

import pytest
from tests.conftest import make_building, make_intervention

from gemp.domain.catalog import Params
from gemp.domain.savings import (
    SavingsError,
    assert_physically_possible,
    bundle_saving_kwh,
    effective_saving_frac,
    intervention_saving_kwh,
    solar_is_capped,
    solar_saving_kwh,
)


def test_saving_is_a_fraction_of_the_end_use_not_of_total(params: Params):
    """The distinction the proposal never made explicit.

    100,000 kWh/yr total, HVAC is 50% of it, the measure removes 20% of HVAC.
    The answer is 10,000 kWh - not 20,000.
    """
    building = make_building(annual_kwh=100_000.0)
    iv = make_intervention(end_use="hvac", saving_frac=0.20)
    assert intervention_saving_kwh(building, iv, params) == pytest.approx(10_000.0)


def test_condition_multiplier_changes_the_answer(params: Params):
    iv = make_intervention(end_use="hvac", saving_frac=0.20, adj_key="insulation")

    poor = make_building(insulation_quality="poor")
    good = make_building(insulation_quality="good")

    assert effective_saving_frac(poor, iv, params) == pytest.approx(0.28)   # 0.20 x 1.4
    assert effective_saving_frac(good, iv, params) == pytest.approx(0.08)   # 0.20 x 0.4
    assert intervention_saving_kwh(poor, iv, params) > intervention_saving_kwh(
        good, iv, params
    )


def test_numeric_adj_buckets_match_ranges(params: Params):
    iv = make_intervention(end_use="hvac", saving_frac=0.30, adj_key="hvac_replacement")

    assert effective_saving_frac(make_building(hvac_age_yr=3), iv, params) == pytest.approx(0.15)
    assert effective_saving_frac(make_building(hvac_age_yr=9), iv, params) == pytest.approx(0.30)
    assert effective_saving_frac(make_building(hvac_age_yr=20), iv, params) == pytest.approx(0.39)


def test_same_end_use_composes_multiplicatively_not_additively(params: Params):
    """F4: the central correction.

    Two measures each removing 30% of HVAC do not remove 60%. Insulation cuts load,
    an HVAC swap raises COP, and both act on E_hvac = Load / COP, so the combined
    fraction is 1 - (0.7)(0.7) = 0.51.
    """
    building = make_building(annual_kwh=100_000.0)          # HVAC share 0.50 -> 50,000 kWh
    a = make_intervention(id="a", end_use="hvac", exclusive_group="ga", saving_frac=0.30)
    b = make_intervention(id="b", end_use="hvac", exclusive_group="gb", saving_frac=0.30)

    combined = bundle_saving_kwh(building, [a, b], params)
    naive_sum = intervention_saving_kwh(building, a, params) + intervention_saving_kwh(
        building, b, params
    )

    assert combined == pytest.approx(50_000.0 * 0.51)
    assert combined < naive_sum
    assert naive_sum / combined == pytest.approx(0.60 / 0.51, rel=1e-9)


def test_different_end_uses_do_add(params: Params):
    """Disjoint slices of consumption, so no interaction to model."""
    building = make_building(annual_kwh=100_000.0)          # hvac 0.50, lighting 0.20
    hvac = make_intervention(id="h", end_use="hvac", exclusive_group="gh", saving_frac=0.20)
    light = make_intervention(id="l", end_use="lighting", exclusive_group="gl", saving_frac=0.50)

    combined = bundle_saving_kwh(building, [hvac, light], params)
    assert combined == pytest.approx(50_000.0 * 0.20 + 20_000.0 * 0.50)


def test_solar_is_capped_by_self_consumption(params: Params):
    """A big roof on a low-consumption building clips; its cost does not clip."""
    building = make_building(roof_area_m2=5000.0, annual_kwh=50_000.0)
    saving = solar_saving_kwh(building, params)

    assert solar_is_capped(building, params)
    assert saving == pytest.approx(50_000.0 * 0.90)


def test_solar_uncapped_when_roof_is_small_relative_to_demand(params: Params):
    building = make_building(roof_area_m2=200.0, annual_kwh=500_000.0)
    assert not solar_is_capped(building, params)
    # 200 m2 x 0.5 usable / 7 = 14.29 kWp x 1600 kWh/kWp
    assert solar_saving_kwh(building, params) == pytest.approx(14.2857 * 1600, rel=1e-3)


def test_orientation_changes_yield(params: Params):
    south = make_building(roof_orientation="S")
    north = make_building(roof_orientation="N")
    assert solar_saving_kwh(south, params) > solar_saving_kwh(north, params)


def test_bundle_saving_is_capped_at_portfolio_ceiling(params: Params):
    """No bundle may claim to save more than the building consumes."""
    building = make_building(annual_kwh=100_000.0)
    greedy_ivs = [
        make_intervention(id=f"iv{i}", end_use=use, exclusive_group=f"g{i}", saving_frac=0.99)
        for i, use in enumerate(["hvac", "lighting", "plug", "other"])
    ]
    saving = bundle_saving_kwh(building, greedy_ivs, params)
    assert saving <= building.annual_kwh * params.bundling.cap_total_saving_frac + 1e-6


def test_guard_rejects_impossible_savings(params: Params):
    building = make_building(annual_kwh=100_000.0)
    assert_physically_possible(building, 99_000.0, params)      # fine
    with pytest.raises(SavingsError, match="exceeds"):
        assert_physically_possible(building, 130_000.0, params)
