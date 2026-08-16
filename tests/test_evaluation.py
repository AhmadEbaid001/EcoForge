"""Episode grouping and the calendar the simulator and forecaster share.

The metric is part of the deliverable. An evaluation that measures the wrong thing
is worse than none, because it produces a number people then act on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from gemp.calendar_eg import PUBLIC_HOLIDAYS, holiday_flags, is_holiday, is_ramadan
from gemp.ml.anomaly import Anomaly, detect_all
from gemp.ml.evaluate import MERGE_GAP_HOURS, score, to_episodes
from gemp.sim.profiles import HOLIDAYS, seasonal_factor

T0 = datetime(2026, 5, 10, tzinfo=UTC)


def anomaly_at(hours: int, z: float = 5.0) -> Anomaly:
    return Anomaly(
        building_id="b001",
        ts=pd.Timestamp(T0 + timedelta(hours=hours)),
        observed_kw=150.0,
        expected_kw=100.0,
        residual=50.0,
        robust_z=z,
    )


# --- the shared calendar ----------------------------------------------------


def test_simulator_and_forecaster_share_one_holiday_calendar():
    """When these disagreed, the model had no way to know the building was shut and
    reported the entire holiday as an anomaly. The five worst days in the evaluation
    were all public holidays."""
    assert HOLIDAYS is PUBLIC_HOLIDAYS


def test_public_holidays_are_recognised():
    assert is_holiday(datetime(2026, 7, 23, tzinfo=UTC))
    assert is_holiday(datetime(2026, 5, 1, tzinfo=UTC))
    assert not is_holiday(datetime(2026, 7, 24, tzinfo=UTC))


def test_ramadan_window():
    assert is_ramadan(datetime(2026, 3, 1, tzinfo=UTC))
    assert not is_ramadan(datetime(2026, 4, 1, tzinfo=UTC))


def test_lag_contamination_flags_exist():
    """The day after a holiday looks anomalous purely because lag_24h is a holiday
    value; a week after, because lag_168h is. Both need a feature or the model
    mispredicts and the detector reports it."""
    index = pd.DatetimeIndex([
        datetime(2026, 7, 23, tzinfo=UTC),      # holiday
        datetime(2026, 7, 24, tzinfo=UTC),      # day after
        datetime(2026, 7, 30, tzinfo=UTC),      # week after
    ])
    flags = holiday_flags(index)

    assert flags["is_holiday"][0] == 1.0
    assert flags["is_day_after_holiday"][1] == 1.0
    assert flags["is_week_after_holiday"][2] == 1.0


# --- smooth seasonality -----------------------------------------------------


def test_seasonal_factor_is_continuous_across_a_month_boundary():
    """A 17% step at midnight on 1 May is unpredictable because nothing causes it -
    real seasonal load follows temperature, which does not step on the calendar. The
    generator was manufacturing false positives in the detector."""
    april_30 = seasonal_factor(datetime(2026, 4, 30, 23, tzinfo=UTC))
    may_1 = seasonal_factor(datetime(2026, 5, 1, 0, tzinfo=UTC))
    assert abs(may_1 - april_30) < 0.01


def test_seasonal_factor_still_peaks_in_summer():
    july = seasonal_factor(datetime(2026, 7, 15, tzinfo=UTC))
    january = seasonal_factor(datetime(2026, 1, 15, tzinfo=UTC))
    assert july > january * 1.6


def test_seasonal_factor_wraps_at_new_year():
    december = seasonal_factor(datetime(2026, 12, 31, tzinfo=UTC))
    january = seasonal_factor(datetime(2026, 1, 1, tzinfo=UTC))
    assert abs(december - january) < 0.02


# --- episode grouping -------------------------------------------------------


def test_consecutive_flags_become_one_episode():
    """The unit that matters is the alert. A ten-hour fault flagged for six hours is
    one correct alert, not six predictions."""
    episodes = to_episodes("b001", [anomaly_at(h) for h in range(6)])
    assert len(episodes) == 1
    assert episodes[0].hours == 6


def test_a_short_dip_below_threshold_does_not_split_a_fault():
    hours = [0, 1, 2, 5, 6]          # a two-hour gap, under MERGE_GAP_HOURS
    episodes = to_episodes("b001", [anomaly_at(h) for h in hours])
    assert len(episodes) == 1


def test_a_long_gap_starts_a_new_episode():
    hours = [0, 1, 2, 40, 41]
    episodes = to_episodes("b001", [anomaly_at(h) for h in hours])
    assert len(episodes) == 2


def test_merge_gap_boundary_is_inclusive():
    episodes = to_episodes("b001", [anomaly_at(0), anomaly_at(MERGE_GAP_HOURS)])
    assert len(episodes) == 1


def test_episode_records_its_peak_score():
    episodes = to_episodes("b001", [anomaly_at(0, 4.0), anomaly_at(1, 9.0), anomaly_at(2, 5.0)])
    assert episodes[0].peak_z == pytest.approx(9.0)


def test_no_anomalies_means_no_episodes():
    assert to_episodes("b001", []) == []


# --- scoring ----------------------------------------------------------------


def truth_frame(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"building_id": b, "kind": "stuck_on", "start": s, "end": e} for b, s, e in rows]
    )


def test_precision_and_recall_use_the_same_unit():
    """The bug this pins: recall counted events while precision counted hours, so a
    single real fault flagged for six hours divided precision by six."""
    truth = truth_frame([("b001", T0, T0 + timedelta(hours=10))])
    detections = {"b001": [anomaly_at(h) for h in range(6)]}

    scores = score(detections, truth)
    assert scores.episodes == 1
    assert scores.true_positives == 1
    assert scores.precision == pytest.approx(1.0)
    assert scores.recall == pytest.approx(1.0)


def test_a_flag_far_from_any_event_is_a_false_alarm():
    truth = truth_frame([("b001", T0, T0 + timedelta(hours=4))])
    detections = {"b001": [anomaly_at(500)]}

    scores = score(detections, truth)
    assert scores.precision == 0.0
    assert scores.recall == 0.0


def test_a_flag_just_after_an_event_counts_as_a_late_alert():
    """Lag features carry a disturbance forward after the fault itself ends, so the
    boundary is not sharp."""
    truth = truth_frame([("b001", T0, T0 + timedelta(hours=4))])
    detections = {"b001": [anomaly_at(5)]}          # one hour after the end

    assert score(detections, truth).true_positives == 1


def test_missing_every_event_gives_zero_recall():
    truth = truth_frame([("b001", T0, T0 + timedelta(hours=4))])
    assert score({}, truth).recall == 0.0


# --- combined detector ------------------------------------------------------


def test_detect_all_finds_what_the_residual_test_cannot():
    """Flatlines are a quarter of the injected faults and are invisible to a residual
    test by construction: a constant series has no spread to deviate from."""
    index = pd.date_range(T0, periods=24 * 40, freq="1h", tz=UTC)
    rng = pd.Series(index=index, dtype=float)
    actual = pd.Series(100.0, index=index) + pd.Series(
        [(i % 7) * 0.5 for i in range(len(index))], index=index
    )
    expected = actual.copy()
    actual.iloc[-20:-8] = 103.0                      # meter frozen for twelve hours

    residual_only = detect_all(actual, expected, "b001", k=3.0, min_flatline_hours=999)
    combined = detect_all(actual, expected, "b001", k=3.0, min_flatline_hours=3)

    assert len(combined) > len(residual_only)
    del rng


def test_detect_all_does_not_double_report_one_hour():
    index = pd.date_range(T0, periods=24 * 40, freq="1h", tz=UTC)
    actual = pd.Series(100.0, index=index)
    expected = pd.Series(100.0, index=index)
    actual.iloc[-10:] = 400.0

    anomalies = detect_all(actual, expected, "b001", k=3.0, min_flatline_hours=3)
    stamps = [a.ts for a in anomalies]
    assert len(stamps) == len(set(stamps))
