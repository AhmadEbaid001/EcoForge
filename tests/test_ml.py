"""Forecasting and anomaly detection: leakage, fallback, and robustness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gemp.ml.anomaly import MAD_TO_SIGMA, detect, detect_flatlines, robust_scale
from gemp.ml.features import (
    FEATURE_COLUMNS,
    WARMUP_HOURS,
    build_features,
    split_by_time,
    to_hourly,
    training_frame,
)
from gemp.ml.forecast import annualize, fit_building, mape, seasonal_naive

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def weekly_series(hours: int = 24 * 90, base: float = 100.0, noise: float = 0.0,
                  seed: int = 1) -> pd.DataFrame:
    """A load series with a strong weekly cycle, like real building load."""
    rng = np.random.default_rng(seed)
    ts = [T0 + timedelta(hours=i) for i in range(hours)]
    hour_of_week = np.arange(hours) % 168
    kw = base * (1 + 0.5 * np.sin(2 * np.pi * hour_of_week / 168))
    if noise:
        kw = kw * rng.lognormal(0, noise, hours)
    return pd.DataFrame({"ts": ts, "kw": kw})


# --- features ---------------------------------------------------------------


def test_hourly_resampling_absorbs_sub_hourly_gaps():
    ts = [T0, T0 + timedelta(minutes=15), T0 + timedelta(minutes=45)]
    frame = pd.DataFrame({"ts": ts, "kw": [10.0, 20.0, 30.0]})
    hourly = to_hourly(frame)
    assert len(hourly) == 1
    assert hourly["kw"].iloc[0] == pytest.approx(20.0)


def test_rolling_features_do_not_leak_the_target():
    """A rolling window that includes the current hour would let the model see its
    own answer - a model that scores beautifully in testing and fails in production."""
    frame = weekly_series(400)
    featured = build_features(to_hourly(frame))

    row = featured.iloc[300]
    manual = featured["kw"].iloc[300 - 24:300].mean()      # strictly before
    assert row["roll_mean_24h"] == pytest.approx(manual)


def test_warmup_rows_are_dropped_not_imputed():
    """Imputing a lag invents the signal the model is supposed to learn."""
    frame = weekly_series(24 * 60)
    ready = training_frame(frame)
    assert len(ready) == len(to_hourly(frame)) - WARMUP_HOURS
    assert ready[list(FEATURE_COLUMNS)].notna().all().all()


def test_cyclical_hour_encoding_wraps():
    frame = weekly_series(24 * 30)
    featured = build_features(to_hourly(frame))
    at_23 = featured[featured.index.hour == 23].iloc[0]
    at_00 = featured[featured.index.hour == 0].iloc[0]
    distance = np.hypot(at_23["hour_sin"] - at_00["hour_sin"],
                        at_23["hour_cos"] - at_00["hour_cos"])
    assert distance < 0.3          # adjacent, not 23 units apart


def test_egyptian_weekend_is_friday_saturday():
    frame = weekly_series(24 * 30)
    featured = build_features(to_hourly(frame))
    weekend = featured[featured["is_weekend"] == 1.0]
    assert set(weekend.index.dayofweek.unique()) == {4, 5}


def test_split_is_chronological_never_random():
    """Shuffling a time series lets the model train on the future to predict the
    past, which inflates the score and says nothing about tomorrow."""
    frame = training_frame(weekly_series(24 * 60))
    train, test = split_by_time(frame, test_hours=48)
    assert len(test) == 48
    assert train.index.max() < test.index.min()


def test_split_refuses_when_history_is_too_short():
    frame = training_frame(weekly_series(24 * 40))
    with pytest.raises(ValueError, match="need more than"):
        split_by_time(frame, test_hours=24 * 100)


# --- forecasting ------------------------------------------------------------


def test_seasonal_naive_repeats_the_previous_week():
    frame = to_hourly(weekly_series(24 * 30))
    predicted = seasonal_naive(frame)
    assert predicted.iloc[200] == pytest.approx(frame["kw"].iloc[200 - 168])


def test_mape_ignores_zero_actuals():
    """A building drawing no power is a meter fault; letting it contribute an
    unbounded percentage would describe the guard, not the forecast."""
    actual = np.array([0.0, 100.0, 200.0])
    predicted = np.array([50.0, 110.0, 180.0])
    assert mape(actual, predicted) == pytest.approx((0.10 + 0.10) / 2 * 100)


def test_baseline_is_kept_when_it_wins():
    """The proposal's rule (Section 4.5), and the honest one: a model that loses to
    'same hour last week' must not ship."""
    result = fit_building(weekly_series(24 * 120, noise=0.0), "b001", test_hours=48)
    assert result.chosen == "baseline"
    assert result.model_version == "seasonal-naive-1"


def test_full_predictions_cover_the_whole_history():
    """Anomaly detection needs an expectation for every hour it examines. Producing
    predictions only for the held-out window left most of the seeded history
    unscored, and the detector reported zero anomalies on data containing hundreds."""
    frame = weekly_series(24 * 120, noise=0.03)
    result = fit_building(frame, "b001", test_hours=48)

    assert len(result.full_predictions) > len(result.predictions) * 10
    assert len(result.predictions) == 48


def test_fit_refuses_insufficient_history():
    with pytest.raises(ValueError, match="need at least"):
        fit_building(weekly_series(24 * 10), "b001")


def test_annualize_uses_whole_weeks():
    """An odd number of days over-weights whichever weekdays it contains, and with a
    Friday-Saturday weekend that bias is large."""
    frame = to_hourly(weekly_series(24 * 30, base=100.0))
    annual = annualize(frame["kw"])
    assert annual == pytest.approx(frame["kw"].iloc[-4 * 168:].mean() * 8760)


# --- anomaly detection ------------------------------------------------------


def test_robust_scale_matches_sigma_under_normality():
    rng = np.random.default_rng(0)
    sample = rng.normal(0, 5, 20_000)
    assert robust_scale(sample) == pytest.approx(5.0, rel=0.05)


def test_mad_resists_contamination_that_breaks_the_standard_deviation():
    """F9, stated as a test. A window containing anomalies inflates sigma, raising
    the threshold and hiding the next anomaly. MAD survives up to 50% contamination."""
    clean = np.concatenate([np.full(90, 0.0), np.array([1.0, -1.0] * 5)])
    contaminated = clean.copy()
    contaminated[:20] = 500.0            # 20% of the window is anomalous

    assert np.std(contaminated) > np.std(clean) * 10        # sigma is destroyed
    assert robust_scale(contaminated) == pytest.approx(robust_scale(clean), rel=0.5)


def test_a_sustained_spike_is_flagged():
    hours = 24 * 60
    index = pd.date_range(T0, periods=hours, freq="1h", tz=UTC)
    expected = pd.Series(100.0, index=index)
    actual = expected.copy()
    actual.iloc[-12:] = 300.0            # a fault in the most recent half-day

    anomalies = detect(actual, expected, "b001", k=3.0, window_days=30)
    assert anomalies
    assert all(a.residual > 0 for a in anomalies)
    assert anomalies[0].direction == "over"


def test_ordinary_variation_is_not_flagged():
    rng = np.random.default_rng(3)
    hours = 24 * 60
    index = pd.date_range(T0, periods=hours, freq="1h", tz=UTC)
    expected = pd.Series(100.0, index=index)
    actual = expected * rng.lognormal(0, 0.05, hours)

    anomalies = detect(actual, expected, "b001", k=5.0, window_days=30)
    assert len(anomalies) / hours < 0.02


def test_severity_scales_with_the_score():
    index = pd.date_range(T0, periods=24 * 60, freq="1h", tz=UTC)
    expected = pd.Series(100.0, index=index)
    actual = expected.copy()
    actual.iloc[-1] = 1000.0

    anomalies = detect(actual, expected, "b001", k=3.0, window_days=30)
    assert anomalies[-1].severity in {"high", "critical"}


def test_flatline_detection_catches_a_stuck_meter():
    """Invisible to the residual detector: a constant series has zero spread, so its
    robust scale collapses and every score becomes undefined."""
    index = pd.date_range(T0, periods=48, freq="1h", tz=UTC)
    actual = pd.Series(np.arange(48, dtype=float), index=index)
    actual.iloc[20:32] = 17.0            # meter stuck for twelve hours

    flatlines = detect_flatlines(actual, "b001", min_hours=4)
    assert flatlines
    assert any(f.observed_kw == 17.0 for f in flatlines)


def test_a_perfectly_modelled_building_can_still_be_flagged():
    """Regression: a near-zero MAD used to null every score, silencing the detector
    on exactly the buildings the forecaster understands best - a failure that
    produces no output rather than wrong output, so nobody notices."""
    index = pd.date_range(T0, periods=24 * 60, freq="1h", tz=UTC)
    expected = pd.Series(100.0, index=index)
    actual = expected.copy()
    actual.iloc[-6:] = 400.0

    anomalies = detect(actual, expected, "b001", k=3.0, window_days=30)
    assert anomalies, "a 4x deviation on a perfectly modelled building must flag"


def test_detect_handles_an_empty_series():
    empty = pd.Series(dtype=float)
    assert detect(empty, empty, "b001") == []
    assert detect_flatlines(empty, "b001") == []


def test_mad_to_sigma_constant():
    assert pytest.approx(1.4826) == MAD_TO_SIGMA


def test_a_stuck_meter_reads_as_high_at_any_threshold():
    """The bug this pins: a sentinel score graded against a moving scale.

    FLATLINE_Z was fixed at 6.0, chosen when the default k was 3.0 and 6.0 cleared the
    high threshold of 4.2. Phase 2 tuning moved k to 8.0, the threshold to 11.2, and
    every stuck meter had since been filed "medium" - the lowest tier, alongside a
    marginal residual blip. A frozen meter is a certain fault and a quarter of the
    injected ones; it also meant the outbound webhook's critical-only default would
    never forward the entire class.
    """
    from gemp.ml.anomaly import Anomaly, flatline_z

    for k in (3.0, 8.0, 12.0, 20.0):
        anomaly = Anomaly(
            building_id="b001", ts=pd.Timestamp("2026-05-01", tz=UTC),
            observed_kw=99.0, expected_kw=99.0, residual=0.0,
            robust_z=flatline_z(k), threshold_k=k,
        )
        assert anomaly.severity == "high", f"k={k} filed a stuck meter as {anomaly.severity}"


def test_a_flatline_does_not_outrank_a_real_excursion():
    """The reason the score is finite rather than infinite.

    An infinity sorts ahead of every real detection and floods the "most severe" list
    with frozen meters, which is the opposite of a useful triage order.
    """
    from gemp.ml.anomaly import Anomaly, flatline_z

    def at(z, k=8.0):
        return Anomaly("b001", pd.Timestamp("2026-05-01", tz=UTC), 1.0, 1.0, 0.0, z, k)

    assert at(flatline_z(8.0)).severity == "high"
    assert at(40.0).severity == "critical"
    assert abs(at(40.0).robust_z) > abs(at(flatline_z(8.0)).robust_z)
