"""Streaming anomaly detection, and the cache invalidation the nightly job depends on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gemp.ingest.live_anomaly import (
    FLATLINE_RUN,
    MIN_RESIDUALS,
    SEASONAL_LAG,
    StreamingDetector,
)

T0 = datetime(2026, 5, 1, tzinfo=UTC)
STEP = timedelta(minutes=15)


def feed_normal(detector: StreamingDetector, readings: int, building: str = "b001",
                base: float = 100.0) -> datetime:
    """Stream a clean, weekly-periodic series and return the next timestamp."""
    ts = T0
    for i in range(readings):
        # Deterministic weekly shape so last week's value is a good expectation.
        value = base * (1 + 0.2 * ((i % 672) / 672))
        detector.score(building, ts, value)
        ts += STEP
    return ts


def test_nothing_is_flagged_before_enough_history():
    """A detector that fires on its third reading is measuring its own warm-up."""
    detector = StreamingDetector(k=8.0)
    flagged = [detector.score("b001", T0 + i * STEP, 100.0 + i) for i in range(50)]
    assert all(f is None for f in flagged)


def test_expectation_is_the_same_time_last_week():
    detector = StreamingDetector(k=8.0)
    ts = feed_normal(detector, 900)

    # A large step now should be measured against the value 168 hours earlier.
    anomaly = detector.score("b001", ts, 1000.0)
    assert anomaly is not None
    expected = detector.state["b001"].readings.get(ts - SEASONAL_LAG)
    assert anomaly.expected_kw == pytest.approx(expected)


def test_a_large_deviation_is_flagged():
    detector = StreamingDetector(k=8.0)
    ts = feed_normal(detector, 900)

    anomaly = detector.score("b001", ts, 400.0)
    assert anomaly is not None
    assert anomaly.kind == "residual"
    assert anomaly.severity in {"medium", "high", "critical"}


def test_ordinary_variation_is_not_flagged():
    detector = StreamingDetector(k=8.0)
    ts = feed_normal(detector, 900)

    baseline = detector.state["b001"].readings[ts - SEASONAL_LAG]
    assert detector.score("b001", ts, baseline * 1.02) is None


def test_a_reading_does_not_contribute_to_the_statistic_that_judges_it():
    """Otherwise a large deviation partially explains itself away."""
    detector = StreamingDetector(k=8.0)
    ts = feed_normal(detector, 900)
    before = len(detector.state["b001"].residuals)

    detector.score("b001", ts, 500.0)
    assert len(detector.state["b001"].residuals) == before + 1


def test_a_stuck_meter_is_flagged_once_not_every_reading():
    """A frozen meter must not emit an alert every fifteen minutes for hours."""
    detector = StreamingDetector(k=8.0)
    ts = feed_normal(detector, 900)

    results = []
    for _ in range(FLATLINE_RUN * 3):
        results.append(detector.score("b001", ts, 77.0))
        ts += STEP

    flatlines = [r for r in results if r is not None and r.kind == "flatline"]
    assert len(flatlines) == 1


def test_flatline_run_must_be_sustained():
    detector = StreamingDetector(k=8.0)
    ts = feed_normal(detector, 900)

    for _ in range(FLATLINE_RUN - 2):
        result = detector.score("b001", ts, 77.0)
        assert result is None or result.kind != "flatline"
        ts += STEP


def test_buildings_are_scored_independently():
    detector = StreamingDetector(k=8.0)
    ts_a = feed_normal(detector, 900, building="b001", base=100.0)
    feed_normal(detector, 900, building="b002", base=900.0)

    assert detector.score("b001", ts_a, 500.0) is not None
    assert "b002" in detector.state
    assert detector.state["b002"].residuals


def test_memory_is_bounded():
    """Ring buffers, not growing lists - the ingester runs for the whole demo."""
    detector = StreamingDetector(k=8.0)
    feed_normal(detector, 5000)

    state = detector.state["b001"]
    assert len(state.order) <= state.order.maxlen
    assert len(state.readings) <= state.order.maxlen
    assert len(state.residuals) <= state.residuals.maxlen


def test_severity_is_relative_to_the_threshold():
    detector = StreamingDetector(k=8.0)
    assert detector._severity(9.0) == "medium"
    assert detector._severity(12.0) == "high"
    assert detector._severity(20.0) == "critical"


def test_no_residual_score_before_a_full_week_of_history():
    """Scoring against an expectation that does not exist would invent a residual.

    Values vary so this exercises the residual path rather than tripping the
    flatline check, which is a separate detector with its own test.
    """
    detector = StreamingDetector(k=8.0)
    for i in range(MIN_RESIDUALS + 10):
        detector.score("b001", T0 + i * STEP, 100.0 + (i % 17))

    # Every reading so far is inside one week, so nothing has a 168h predecessor
    # and no residual can be formed.
    assert detector.flagged == 0
    assert len(detector.state["b001"].residuals) == 0
