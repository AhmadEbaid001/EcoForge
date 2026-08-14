"""Test fixtures built in code, not read from data/.

Tests must not depend on the values in data/catalog.csv, because those values are
Nada's to change. A test that breaks when a cost estimate is revised is a test that
will be deleted under deadline pressure. The data files get their own structural
validation instead (tests/test_catalog.py).
"""

from __future__ import annotations

import pytest

from gemp.domain.catalog import Params
from gemp.domain.models import Building, Intervention

PARAMS_DICT = {
    "horizon_yr": 30,
    "grid_emission_factor": 0.45,
    "discount_rate": 0.0,
    "electricity_tariff_egp_per_kwh": 2.0,
    "anomaly_k": 3.0,
    "anomaly_window_days": 30,
    "end_use_share": {
        "office": {"hvac": 0.50, "lighting": 0.20, "plug": 0.25, "other": 0.05},
    },
    "adj": {
        "insulation": {"insulation_quality": {"poor": 1.40, "fair": 1.00, "good": 0.40}},
        "hvac_replacement": {"hvac_age_yr": {"<5": 0.50, "5-14": 1.00, ">=15": 1.30}},
    },
    "solar": {
        "usable_roof_frac": 0.50,
        "m2_per_kwp": 7.0,
        "yield_kwh_per_kwp": 1600.0,
        "orientation_factor": {
            "S": 1.0, "SE": 0.96, "SW": 0.96, "E": 0.88, "W": 0.88,
            "NE": 0.78, "NW": 0.78, "N": 0.72, "FLAT": 1.0,
        },
        "soiling_loss": 0.0,
        "self_consumption_cap": 0.90,
        "solar_fixed_egp": 100_000.0,
        "solar_egp_per_kwp": 20_000.0,
    },
    "bundling": {"max_bundle_size": 3, "cap_total_saving_frac": 0.95},
    "constraints": {"max_funded_per_district": None},
    "guards": {"max_building_saving_frac": 1.0},
}


@pytest.fixture
def params() -> Params:
    return Params.model_validate(PARAMS_DICT)


def make_building(**overrides) -> Building:
    base = {
        "id": "b001",
        "code": "TEST-001",
        "name": "Test Building",
        "district": "D1",
        "lat": 30.0,
        "lon": 31.7,
        "floor_area_m2": 1000.0,
        "roof_area_m2": 1000.0,
        "glazing_area_m2": 150.0,
        "roof_orientation": "FLAT",
        "hvac_type": "chiller",
        "hvac_age_yr": 10,
        "insulation_quality": "fair",
        "occupancy_pattern": "office",
        "annual_kwh": 100_000.0,
    }
    return Building.model_validate(base | overrides)


def make_intervention(**overrides) -> Intervention:
    base = {
        "id": "iv1",
        "label": "Test Intervention",
        "end_use": "hvac",
        "exclusive_group": "g1",
        "adj_key": "",
        "cost_type": "fixed",
        "cost_value": 100_000.0,
        "saving_frac": 0.20,
        "service_life_yr": 30,
        "embodied_type": "fixed",
        "embodied_value": 0.0,
        "applies_if": "",
        "source_ref": "test",
        "notes": "",
    }
    return Intervention.model_validate(base | overrides)


@pytest.fixture
def building() -> Building:
    return make_building()
