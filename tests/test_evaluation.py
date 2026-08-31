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


def truth_frame(rows, source: str = "seed") -> pd.DataFrame:
    return pd.DataFrame([
        {"building_id": b, "kind": "stuck_on", "start": s, "end": e, "source": source}
        for b, s, e in rows
    ])


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


# --- what the ground truth can and cannot judge ------------------------------


def test_alerts_past_the_end_of_the_ground_truth_are_not_false_alarms():
    """The bug this pins cost 24 points of reported precision.

    The simulator keeps running at 720x after the seeded window, and the faults it
    injects there were not being recorded - the `sim` container had no mount for the
    file it writes them to. Scoring the whole series against the seeded truth alone
    counted every correctly detected live fault as a false alarm and reported episode
    precision as 0.26 where it was 0.50.
    """
    truth = truth_frame([("b001", T0, T0 + timedelta(hours=4))])
    detections = {"b001": [anomaly_at(1), anomaly_at(5000)]}

    scores = score(detections, truth)

    assert scores.episodes == 1                 # only the judgeable one
    assert scores.episodes_unscorable == 1
    assert scores.precision == pytest.approx(1.0)


def test_the_gap_between_two_recorded_windows_is_not_judged():
    """Coverage is per source, not one span from earliest start to latest end.

    Once the live file is being written again, a single min-to-max window would
    swallow the months in which nothing was recorded and silently resume counting
    correct detections there as false alarms.
    """
    seeded = truth_frame([("b001", T0, T0 + timedelta(hours=4))], source="seed")
    live = truth_frame(
        [("b001", T0 + timedelta(days=200), T0 + timedelta(days=200, hours=4))],
        source="live",
    )
    truth = pd.concat([seeded, live], ignore_index=True)

    # Squarely inside the unrecorded gap between the two windows.
    detections = {"b001": [anomaly_at(24 * 100)]}
    scores = score(detections, truth)

    assert scores.episodes == 0
    assert scores.episodes_unscorable == 1


def test_episode_suppression_drops_alerts_before_they_are_judged():
    """The knobs Phase 4 used to show that suppression does not move the curve.

    Kept working, and off by default, so the negative result stays reproducible
    instead of becoming folklore that someone re-derives from scratch.
    """
    truth = truth_frame([("b001", T0, T0 + timedelta(hours=10))])
    detections = {"b001": [anomaly_at(h, z=5.0) for h in range(2)]}

    assert score(detections, truth).episodes == 1
    assert score(detections, truth, min_episode_hours=3).episodes == 0
    assert score(detections, truth, min_peak_z=9.0).episodes == 0
    assert score(detections, truth, min_peak_z=4.0).episodes == 1


def test_an_alert_inside_the_live_window_is_still_judged():
    truth = pd.concat([
        truth_frame([("b001", T0, T0 + timedelta(hours=4))], source="seed"),
        truth_frame(
            [("b001", T0 + timedelta(days=200), T0 + timedelta(days=200, hours=4))],
            source="live",
        ),
    ], ignore_index=True)

    detections = {"b001": [anomaly_at(24 * 200 + 1)]}
    scores = score(detections, truth)

    assert scores.episodes == 1
    assert scores.true_positives == 1


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


# --- ground truth a rewound simulator cannot support -------------------------


def test_live_records_split_into_the_runs_that_wrote_them():
    from datetime import UTC, datetime

    from gemp.ml.evaluate import split_live_runs

    def record(day):
        return {"start": datetime(2026, 1, day, tzinfo=UTC),
                "end": datetime(2026, 1, day, 1, tzinfo=UTC)}

    runs = split_live_runs([record(1), record(2), record(5),      # one run
                            record(3), record(4)])                # clock went back
    assert [len(run) for run in runs] == [3, 2]


def test_events_from_a_rewound_run_are_dropped():
    """Their readings were discarded as duplicates, so no data supports them.

    Counting them made recall a measure of how badly the simulator restarted: 1,762
    of 2,212 events were phantoms, and they read as recall 0.375 for a detector whose
    recall is 0.84.
    """
    from datetime import UTC, datetime

    from gemp.ml.evaluate import drop_rewound_runs

    seed_end = datetime(2026, 6, 1, tzinfo=UTC)
    forward = [{"start": datetime(2026, 7, 1, tzinfo=UTC),
                "end": datetime(2026, 8, 1, tzinfo=UTC)}]
    rewound = [{"start": datetime(2026, 6, 2, tzinfo=UTC),
                "end": datetime(2026, 6, 3, tzinfo=UTC)}]

    assert drop_rewound_runs(seed_end, [forward, rewound]) == [forward]

    # A run that begins after everything already covered is legitimate, however long
    # the gap before it - the simulator spent seven months of data time writing its
    # ground truth into a container with no mount, and that history is real.
    assert drop_rewound_runs(seed_end, [forward]) == [forward]

    # And the first live run in the file is not exempt: starting inside the SEEDED
    # window is the same duplicate-publishing failure.
    into_the_seed = [{"start": datetime(2026, 5, 1, tzinfo=UTC),
                      "end": datetime(2026, 5, 2, tzinfo=UTC)}]
    assert drop_rewound_runs(seed_end, [into_the_seed]) == []

# ---------------------------------------------------------------- where the
# ground truth lives


def _paths_with(tmp_path, monkeypatch):
    """A checkout-shaped pair of directories, resolved the way a container does."""
    (tmp_path / "data").mkdir()
    (tmp_path / "anchor").mkdir()
    monkeypatch.setenv("GEMP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GEMP_ANCHOR_DIR", raising=False)
    return tmp_path / "data", tmp_path / "anchor"


HEADER = "building_id,kind,start,end,magnitude\n"


def test_the_seed_writes_ground_truth_where_it_can_actually_write(tmp_path, monkeypatch):
    """data/ is mounted read-only into the containers; anchor/ is not. Writing the
    generated file into the shipped directory failed on a deployed host AFTER the
    rows were already in the database."""
    from gemp.paths import ground_truth_write_path

    _data, anchor = _paths_with(tmp_path, monkeypatch)
    assert ground_truth_write_path() == anchor / "ground_truth.csv"


def test_a_file_left_by_an_older_seed_is_still_read(tmp_path, monkeypatch):
    """Moving where it is written must not blind an existing checkout to the copy it
    already has, or F9 stops being measurable until somebody re-seeds."""
    from gemp.paths import ground_truth_read_path

    data, _anchor = _paths_with(tmp_path, monkeypatch)
    (data / "ground_truth.csv").write_text(HEADER, encoding="utf-8")
    assert ground_truth_read_path() == data / "ground_truth.csv"


def test_the_writable_copy_wins_when_both_exist(tmp_path, monkeypatch):
    """A re-seed updates the anchor copy; the stale one in data/ must not shadow it."""
    from gemp.paths import ground_truth_read_path

    data, anchor = _paths_with(tmp_path, monkeypatch)
    (data / "ground_truth.csv").write_text(HEADER, encoding="utf-8")
    (anchor / "ground_truth.csv").write_text(HEADER, encoding="utf-8")
    assert ground_truth_read_path() == anchor / "ground_truth.csv"


# ------------------------------------------------------- picking the row the
# paper quotes


def _scores(k: float):
    from gemp.ml.evaluate import Scores

    return Scores(k=k, flagged=10, episodes=4, events=5, events_found=4,
                  true_positives=3, flag_rate=0.01)


def test_the_measurement_returns_the_row_for_the_configured_threshold():
    """`anomaly_k` is a parameter, and the sweep is measured at several values. The
    claim is quoted at the configured one, so picking the wrong row would report a
    precision the deployment does not run at."""
    from gemp.evaluate.measured import Measurement

    measurement = Measurement(buildings=50, model_median_mape=3.2,
                              baseline_median_mape=8.6, beat_baseline=50,
                              scores=[_scores(3.0), _scores(5.0), _scores(7.0)])
    assert measurement.scores_at(5.0).k == 5.0


def test_asking_for_a_threshold_that_was_not_swept_says_what_was():
    """A KeyError naming the sweep is the difference between "the harness is broken"
    and "params.yaml moved anomaly_k and the sweep did not follow"."""
    import pytest

    from gemp.evaluate.measured import Measurement

    measurement = Measurement(buildings=50, model_median_mape=3.2,
                              baseline_median_mape=8.6, beat_baseline=50,
                              scores=[_scores(3.0), _scores(5.0)])
    with pytest.raises(KeyError) as caught:
        measurement.scores_at(4.0)
    assert "3.0" in str(caught.value) and "5.0" in str(caught.value)
