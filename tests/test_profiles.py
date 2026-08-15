"""The simulator must conserve energy and be realistically hard to forecast."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from tests.conftest import make_building

from gemp.sim.profiles import DIURNAL, SEASONAL, WEEKEND_DAYS, generate_series

START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def month():
    return START, START + timedelta(days=30)


def test_full_year_conserves_annual_energy():
    """A full year must integrate to the portfolio's annual_kwh.

    If it drifts, the forecast-derived annual estimate stops matching the profile the
    optimizer was costed against and every savings figure quietly loses meaning.
    """
    building = make_building(annual_kwh=1_200_000.0)
    series = generate_series(building, datetime(2026, 1, 1, tzinfo=UTC),
                             datetime(2027, 1, 1, tzinfo=UTC),
                             seed=1, dropout_rate=0.0, anomalies_per_month=0.0,
                             noise_cv=0.0)

    assert series.total_kwh(15) == pytest.approx(building.annual_kwh, rel=1e-9)


def test_a_month_is_not_forced_to_one_twelfth_of_the_year():
    """Scaling is global, not per-window, and that distinction matters.

    Normalising each generated span to its own pro-rata share would force January and
    July to the same energy, erasing the cooling peak - the strongest seasonal signal
    in Egyptian building load and the main thing a forecaster should learn.
    """
    building = make_building(annual_kwh=1_200_000.0)

    def month_kwh(month: int) -> float:
        start = datetime(2026, month, 1, tzinfo=UTC)
        end = datetime(2026, month + 1, 1, tzinfo=UTC)
        return generate_series(building, start, end, seed=2, dropout_rate=0.0,
                               anomalies_per_month=0.0, noise_cv=0.0).total_kwh(15)

    january = month_kwh(1)
    july = month_kwh(7)
    even_share = building.annual_kwh / 12

    assert january < even_share < july


def test_generation_is_deterministic_for_a_seed(month):
    start, end = month
    building = make_building()
    a = generate_series(building, start, end, seed=42)
    b = generate_series(building, start, end, seed=42)
    assert np.array_equal(a.kw, b.kw)
    assert [e.start for e in a.anomalies] == [e.start for e in b.anomalies]


def test_different_seeds_differ(month):
    start, end = month
    building = make_building()
    a = generate_series(building, start, end, seed=1, dropout_rate=0.0)
    b = generate_series(building, start, end, seed=2, dropout_rate=0.0)
    assert not np.array_equal(a.kw, b.kw)


def test_weekend_load_is_lower_for_an_office(month):
    """Egypt's weekend is Friday and Saturday, not Saturday and Sunday. A model that
    assumes the Western week gets two days a week wrong, every week."""
    start, end = month
    building = make_building(occupancy_pattern="office", annual_kwh=1_000_000.0)
    series = generate_series(building, start, end, seed=3, dropout_rate=0.0,
                             anomalies_per_month=0.0)

    weekend = np.array([
        k for t, k in zip(series.timestamps, series.kw, strict=True)
        if t.weekday() in WEEKEND_DAYS
    ])
    weekday = np.array([
        k for t, k in zip(series.timestamps, series.kw, strict=True)
        if t.weekday() not in WEEKEND_DAYS
    ])
    assert weekend.mean() < weekday.mean() * 0.6


def test_night_load_is_lower_than_day_load(month):
    start, end = month
    building = make_building(occupancy_pattern="office", annual_kwh=1_000_000.0)
    series = generate_series(building, start, end, seed=4, dropout_rate=0.0,
                             anomalies_per_month=0.0)

    night = np.array([k for t, k in zip(series.timestamps, series.kw, strict=True)
                      if t.hour in (2, 3, 4)])
    day = np.array([k for t, k in zip(series.timestamps, series.kw, strict=True)
                    if t.hour in (10, 11, 12)])
    assert night.mean() < day.mean() * 0.5


def test_24x7_building_has_a_flatter_profile_than_a_school(month):
    start, end = month
    admin = generate_series(make_building(occupancy_pattern="admin_24x7"),
                            start, end, seed=5, dropout_rate=0.0, anomalies_per_month=0.0)
    school = generate_series(make_building(occupancy_pattern="school"),
                             start, end, seed=5, dropout_rate=0.0, anomalies_per_month=0.0)

    def coefficient_of_variation(a: np.ndarray) -> float:
        return float(a.std() / a.mean())

    assert coefficient_of_variation(admin.kw) < coefficient_of_variation(school.kw)


def test_summer_load_exceeds_winter_load():
    """Cairo cooling season. A year-long run so both seasons are present."""
    building = make_building(annual_kwh=1_000_000.0)
    series = generate_series(building, datetime(2026, 1, 1, tzinfo=UTC),
                             datetime(2026, 12, 31, tzinfo=UTC),
                             seed=6, dropout_rate=0.0, anomalies_per_month=0.0)

    july = np.array([k for t, k in zip(series.timestamps, series.kw, strict=True)
                     if t.month == 7])
    january = np.array([k for t, k in zip(series.timestamps, series.kw, strict=True)
                        if t.month == 1])
    assert july.mean() > january.mean() * 1.5


def test_anomalies_are_logged_as_ground_truth(month):
    """Precision and recall are only reportable because the injector records what it
    injected (proposal Section 4.5)."""
    start, end = month
    building = make_building()
    series = generate_series(building, start, end, seed=7, anomalies_per_month=4.0)

    assert series.anomalies
    for event in series.anomalies:
        assert event.building_id == building.id
        assert event.start <= event.end
        assert event.kind in {"stuck_on", "spike", "drift", "flatline"}


def test_anomalies_actually_raise_consumption(month):
    start, end = month
    building = make_building(annual_kwh=1_000_000.0)

    clean = generate_series(building, start, end, seed=8, dropout_rate=0.0,
                            anomalies_per_month=0.0)
    dirty = generate_series(building, start, end, seed=8, dropout_rate=0.0,
                            anomalies_per_month=6.0)

    assert dirty.kw.sum() > clean.kw.sum()


def test_dropouts_remove_readings(month):
    start, end = month
    building = make_building()
    series = generate_series(building, start, end, seed=9, dropout_rate=0.05)

    expected_full = 30 * 24 * 4          # 30 days at 15-minute resolution
    assert series.dropouts > 0
    assert len(series) == expected_full - series.dropouts


def test_no_negative_or_nan_readings(month):
    start, end = month
    building = make_building()
    series = generate_series(building, start, end, seed=10, anomalies_per_month=6.0)

    assert np.all(np.isfinite(series.kw))
    assert np.all(series.kw >= 0)


def test_diurnal_and_seasonal_tables_are_well_formed():
    for pattern, curve in DIURNAL.items():
        assert len(curve) == 24, f"{pattern} diurnal curve must have 24 hours"
        assert all(v > 0 for v in curve)
    assert len(SEASONAL) == 12
