"""Streaming anomaly detection, evaluated as readings arrive.

The nightly job re-scores the whole portfolio, which is the right place to tune
thresholds and measure quality. It is the wrong place to notice that a chiller has
been running since midnight: a fault found at 03:17 the next morning has already
burned a night of energy, and during a demonstration nothing appears on the map at
all.

This detector keeps a small ring buffer per building and scores each reading as it is
written, using the same statistic as the batch path:

    expectation  = the reading 168 hours earlier (same hour, previous week)
    residual     = (actual - expected) / expected
    flag         = |residual - median(recent)| > k * 1.4826 * MAD(recent)

Seasonal-naive is the expectation here rather than the learned model, for a practical
reason: the model predicts from features that include lags of the value being
predicted, so scoring a reading the instant it arrives would mean running the whole
feature pipeline per reading. Last week's same hour is one array lookup, needs no
model to be loaded, and is the baseline the learned model only beats by about a
percentage point of MAPE anyway.

Memory is bounded and small: 168 hours of readings plus 30 days of residuals per
building is a few hundred kilobytes across fifty buildings.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select

from gemp.db import ReadingRow
from gemp.ingest.integrity import as_utc
from gemp.ml.anomaly import FLATLINE_Z, MAD_TO_SIGMA, MIN_RELATIVE_SPREAD

log = logging.getLogger("gemp.ingest.live")

SEASONAL_LAG = timedelta(hours=168)

# Enough residual history for the rolling statistic to be meaningful, and enough
# readings to look back a full week for the expectation.
RESIDUAL_WINDOW = 2880          # 30 days at 15-minute resolution
HISTORY_READINGS = 700          # slightly over 168 hours at 15-minute resolution
MIN_RESIDUALS = 200             # before this, the reference statistic is noise

# A meter reporting the identical value this many times running is stuck.
FLATLINE_RUN = 12               # three hours at 15-minute resolution


@dataclass
class BuildingState:
    """Ring buffers for one building. Bounded by construction."""

    readings: dict[datetime, float] = field(default_factory=dict)
    order: deque[datetime] = field(default_factory=lambda: deque(maxlen=HISTORY_READINGS))
    residuals: deque[float] = field(default_factory=lambda: deque(maxlen=RESIDUAL_WINDOW))
    flat_run: int = 0
    last_kw: float | None = None

    def remember(self, ts: datetime, kw: float) -> None:
        if len(self.order) == self.order.maxlen and self.order:
            self.readings.pop(self.order[0], None)
        self.order.append(ts)
        self.readings[ts] = kw


@dataclass
class LiveAnomaly:
    building_id: str
    ts: datetime
    observed_kw: float
    expected_kw: float
    residual: float
    robust_z: float
    severity: str
    kind: str


class StreamingDetector:
    """Scores readings as they arrive. Not thread-safe; the ingester is single-threaded."""

    def __init__(self, k: float = 8.0):
        self.k = k
        self.state: dict[str, BuildingState] = defaultdict(BuildingState)
        self.flagged = 0
        self.scored = 0

    # -- warm start ----------------------------------------------------------

    def warm_from_db(self, session, days: int = 31) -> int:
        """Load recent history so the detector is useful from the first reading.

        Without this the buffers start empty and nothing can be scored until a week
        of data has streamed past - which, at 720x, is still several minutes of a
        demonstration spent showing nothing.

        One query for the whole portfolio, never one per building: a per-building read
        of a many-chunk hypertable bound to a building id is pathologically slow
        (measured at 130 s for 6,694 rows versus 1.7 s for 334,702). The date bound is
        a parameter, which is fine - the pathology was chunk exclusion failing for an
        unseen BUILDING value, not for a timestamp.

        The cutoff is computed in Python and passed in, rather than written as
        `INTERVAL '31 days'` in the SQL. That is the house rule and this function was
        breaking it: the interval literal is PostgreSQL-only, so the ingester could not
        run against SQLite at all, despite `Ingester` taking an injectable session
        factory specifically so that it could. Nothing noticed until an integration
        test ran the real consumer against a temporary database. The literal also
        hardcoded 31 and ignored `days` entirely.

        Anchored on the newest READING, not on `now()`. Under 720x replay data time
        runs months ahead of the wall clock, and a wall-clock cutoff selects nothing.
        """
        newest = session.execute(select(func.max(ReadingRow.ts))).scalar()
        if newest is None:
            log.info("no readings stored; detector starts cold")
            return 0

        cutoff = as_utc(newest) - timedelta(days=days)
        rows = session.execute(
            select(ReadingRow.building_id, ReadingRow.ts, ReadingRow.kw)
            .where(ReadingRow.ts >= cutoff)
            .order_by(ReadingRow.building_id, ReadingRow.ts)
        ).all()

        for building_id, ts, kw in rows:
            state = self.state[building_id]
            state.remember(as_utc(ts), float(kw))

        log.info("warmed %d buildings from %d readings since %s",
                 len(self.state), len(rows), cutoff.isoformat())
        return len(rows)

    # -- scoring -------------------------------------------------------------

    def score(self, building_id: str, ts: datetime, kw: float) -> LiveAnomaly | None:
        state = self.state[building_id]

        anomaly = self._score_flatline(state, building_id, ts, kw)
        if anomaly is None:
            anomaly = self._score_residual(state, building_id, ts, kw)

        state.remember(ts, kw)
        state.last_kw = kw
        self.scored += 1
        if anomaly is not None:
            self.flagged += 1
        return anomaly

    def _score_flatline(self, state, building_id, ts, kw) -> LiveAnomaly | None:
        if state.last_kw is not None and abs(kw - state.last_kw) <= 1e-9:
            state.flat_run += 1
        else:
            state.flat_run = 0

        # Only the moment the run crosses the threshold, not every reading after -
        # otherwise a frozen meter emits an alert every fifteen minutes for hours.
        if state.flat_run != FLATLINE_RUN:
            return None

        return LiveAnomaly(
            building_id=building_id, ts=ts, observed_kw=kw, expected_kw=kw,
            residual=0.0, robust_z=FLATLINE_Z,
            severity=self._severity(FLATLINE_Z), kind="flatline",
        )

    def _score_residual(self, state, building_id, ts, kw) -> LiveAnomaly | None:
        expected = state.readings.get(ts - SEASONAL_LAG)
        if expected is None or abs(expected) < 1e-6:
            return None

        residual = (kw - expected) / abs(expected)

        if len(state.residuals) < MIN_RESIDUALS:
            state.residuals.append(residual)
            return None

        sample = np.fromiter(state.residuals, dtype=float)
        median = float(np.median(sample))
        scale = max(
            float(np.median(np.abs(sample - median))) * MAD_TO_SIGMA,
            MIN_RELATIVE_SPREAD,
        )
        z = (residual - median) / scale

        # Append AFTER scoring: a reading must not contribute to the statistic that
        # judges it, or a large deviation partially explains itself away.
        state.residuals.append(residual)

        if abs(z) <= self.k:
            return None

        return LiveAnomaly(
            building_id=building_id, ts=ts, observed_kw=kw, expected_kw=expected,
            residual=kw - expected, robust_z=z,
            severity=self._severity(z), kind="residual",
        )

    def _severity(self, z: float) -> str:
        magnitude = abs(z)
        if magnitude >= 2.0 * self.k:
            return "critical"
        if magnitude >= 1.4 * self.k:
            return "high"
        return "medium"
