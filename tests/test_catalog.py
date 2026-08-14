"""The data contract: parsing, validation, and the rules that protect it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.conftest import PARAMS_DICT, make_building, make_intervention

from gemp.domain.catalog import (
    Params,
    applies,
    cross_validate,
    load_catalog,
    load_params,
)

# --- applies_if mini-DSL ----------------------------------------------------


def test_blank_rule_always_applies():
    assert applies(make_intervention(applies_if=""), make_building())


def test_numeric_comparison():
    iv = make_intervention(applies_if="hvac_age_yr>=8")
    assert applies(iv, make_building(hvac_age_yr=10))
    assert applies(iv, make_building(hvac_age_yr=8))
    assert not applies(iv, make_building(hvac_age_yr=7))


def test_membership_over_a_pipe_list():
    iv = make_intervention(applies_if="insulation_quality in poor|fair")
    assert applies(iv, make_building(insulation_quality="poor"))
    assert applies(iv, make_building(insulation_quality="fair"))
    assert not applies(iv, make_building(insulation_quality="good"))


def test_conditions_are_conjunctive():
    iv = make_intervention(applies_if="hvac_age_yr>=8;roof_area_m2>=500")
    assert applies(iv, make_building(hvac_age_yr=10, roof_area_m2=600))
    assert not applies(iv, make_building(hvac_age_yr=10, roof_area_m2=400))
    assert not applies(iv, make_building(hvac_age_yr=2, roof_area_m2=600))


def test_rule_can_reference_a_derived_property():
    iv = make_intervention(applies_if="glazing_ratio>=0.15")
    assert applies(iv, make_building(floor_area_m2=1000, glazing_area_m2=200))
    assert not applies(iv, make_building(floor_area_m2=1000, glazing_area_m2=100))


def test_unknown_field_is_an_error_not_a_silent_false():
    iv = make_intervention(applies_if="wingspan>=3")
    with pytest.raises(ValueError, match="not a Building field"):
        applies(iv, make_building())


# --- params.yaml validation -------------------------------------------------


def test_end_use_shares_must_sum_to_one():
    broken = PARAMS_DICT | {
        "end_use_share": {"office": {"hvac": 0.5, "lighting": 0.2, "plug": 0.2, "other": 0.05}}
    }
    with pytest.raises(ValidationError, match="sums to"):
        Params.model_validate(broken)


def test_unmatched_adj_bucket_is_an_error(params: Params):
    """A silently ignored multiplier is exactly the defect that produces a
    plausible-looking but wrong recommendation, so it must raise."""
    narrow = Params.model_validate(
        PARAMS_DICT | {"adj": {"hvac_replacement": {"hvac_age_yr": {"<5": 0.5}}}}
    )
    with pytest.raises(KeyError, match="no bucket matches"):
        narrow.adj_multiplier("hvac_replacement", make_building(hvac_age_yr=30))


def test_blank_adj_key_means_no_adjustment(params: Params):
    assert params.adj_multiplier("", make_building()) == 1.0


def test_unknown_adj_key_is_rejected(params: Params):
    with pytest.raises(KeyError, match="no such block"):
        params.adj_multiplier("nonexistent", make_building())


# --- Intervention model invariants -----------------------------------------


def test_generation_requires_the_solar_cost_model():
    with pytest.raises(ValidationError, match="cost_type=solar"):
        make_intervention(end_use="generation", cost_type="fixed")


def test_solar_cost_model_requires_generation():
    with pytest.raises(ValidationError, match="requires end_use=generation"):
        make_intervention(end_use="hvac", cost_type="solar")


def test_zero_saving_frac_is_rejected_for_non_generation():
    """A row that can never be selected is a data-entry error, not a valid option."""
    with pytest.raises(ValidationError, match="can never be selected"):
        make_intervention(saving_frac=0.0)


def test_source_ref_is_required():
    with pytest.raises(ValidationError):
        make_intervention(source_ref="")


def test_todo_source_ref_is_flagged():
    assert make_intervention(source_ref="TODO(Nada): cite").needs_citation
    assert not make_intervention(source_ref="ASHRAE 90.1-2019 Table 5.5").needs_citation


# --- the real files ---------------------------------------------------------


def test_shipped_data_files_load_and_cross_validate():
    """Structural check on data/. Deliberately asserts nothing about the VALUES,
    which are Nada's to change without breaking the build."""
    params = load_params()
    catalog = load_catalog()

    assert catalog, "catalog.csv has no rows"
    assert cross_validate(catalog, params) == []
