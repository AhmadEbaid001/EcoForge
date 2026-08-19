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

import copy
import logging
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select

from gemp.db import ReadingRow
from gemp.ingest.integrity import as_utc
from gemp.ml.anomaly import (
    CRITICAL_SEVERITY_MULTIPLE,
    HIGH_SEVERITY_MULTIPLE,
    MAD_TO_SIGMA,
    MIN_RELATIVE_SPREAD,
    flatline_z,
)

log = logging.getLogger("gemp.ingest.live")

SEASONAL_LAG = timedelta(hours=168)

# Enough residual history for the rolling statistic to be meaningful, and enough
# readings to look back a full week for the expectation.
RESIDUAL_WINDOW = 2880          # 30 days at 15-minute resolution
HISTORY_READINGS = 700          # slightly over 168 hours at 15-minute resolution
MIN_RESIDUALS = 200             # before this, the reference statistic is noise

# A meter reporting the identical value this many times running is stuck.
FLATLINE_RUN = 12               # three hours at 15-minute resolution

# How many buildings the detector will hold state for at once.
#
# Each BuildingState is bounded by construction, but the DICTIONARY holding them was
# not, and its keys come straight out of an MQTT payload. A publisher on the broker
# sending readings for invented building ids would grow it for the life of the
# process - and the ingester scores a reading BEFORE the database gets a chance to
# reject it, so the foreign key does not help here.
#
# The portfolio is fifty. The cap is well clear of any real deployment, and when it
# is reached the least recently scored building is evicted rather than the oldest
# added - a real building that reports every fifteen minutes must never be dropped in
# favour of junk that arrived once.
MAX_TRACKED_BUILDINGS = 512


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
        # Insertion-ordered and used as an LRU: `_state_for` moves a building to the
        # end each time it is touched, so popping the front evicts the least recently
        # scored. A defaultdict cannot do that, and it also created an entry for every
        # id anyone ever published.
        self.state: dict[str, BuildingState] = {}
        self.evicted = 0
        self.flagged = 0
        self.scored = 0
        # Set only inside `rollback_on_error`. Maps a building to the state it had
        # before this batch touched it, or to None if the batch is what created it.
        self._saved: dict[str, BuildingState | None] | None = None

    @contextmanager
    def rollback_on_error(self):
        """Undo this batch's effect on the detector if the batch is not stored.

        Scoring MUTATES memory: the residual window that judges the next reading,
        the flat-run counter, the ring of recent values. That happens while the
        readings are still uncommitted, because a flat-line run has to be counted
        across the batch in order and the anomaly rows are written in the same
        transaction as the readings they describe.

        So a failed commit used to leave the detector having already seen readings
        the database does not have. The ingester requeues that batch by design, and
        the second pass scored the same readings again - appending every residual
        twice into the window the next reading is measured against, and advancing a
        flat-run counter that had already advanced. The statistic quietly drifts
        away from the data it claims to describe, in the one direction nobody
        checks: a doubled window is a wider window, so real faults stop being
        flagged.

        This is the same rule the chain heads already follow - see `_write` in the
        consumer, where `heads` is held aside and applied only after the commit
        returns. Memory that describes stored rows may only advance when the rows
        are stored.

        The snapshot is per building actually touched, taken once, on first touch.
        A batch spans at most a few dozen buildings and each state is bounded by
        construction, so the copy is small and it is taken once per flush.
        """
        saved: dict[str, BuildingState | None] = {}
        counters = (self.scored, self.flagged, self.evicted)
        self._saved = saved
        try:
            yield
        except BaseException:
            for building_id, snapshot in saved.items():
                if snapshot is None:
                    # The batch is what created this entry; remove it again.
                    self.state.pop(building_id, None)
                else:
                    self.state[building_id] = snapshot
            self.scored, self.flagged, self.evicted = counters
            raise
        finally:
            self._saved = None

    def _remember_for_rollback(self, building_id: str, state: BuildingState | None) -> None:
        if self._saved is not None and building_id not in self._saved:
            self._saved[building_id] = copy.deepcopy(state) if state is not None else None

    def _state_for(self, building_id: str) -> BuildingState:
        state = self.state.pop(building_id, None)
        self._remember_for_rollback(building_id, state)
        if state is None:
            state = BuildingState()
            if len(self.state) >= MAX_TRACKED_BUILDINGS:
                stale, _ = next(iter(self.state.items()))
                del self.state[stale]
                self.evicted += 1
                if self.evicted == 1 or self.evicted % 1000 == 0:
                    log.warning(
                        "tracking more than %d buildings; evicted %r (%d so far). "
                        "Something is publishing ids the portfolio does not contain.",
                        MAX_TRACKED_BUILDINGS, stale, self.evicted,
                    )
        self.state[building_id] = state
        return state

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
            self._state_for(building_id).remember(as_utc(ts), float(kw))

        log.info("warmed %d buildings from %d readings since %s",
                 len(self.state), len(rows), cutoff.isoformat())
        return len(rows)

    # -- scoring -------------------------------------------------------------

    def score(self, building_id: str, ts: datetime, kw: float) -> LiveAnomaly | None:
        state = self._state_for(building_id)

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
            residual=0.0, robust_z=flatline_z(self.k),
            severity=self._severity(flatline_z(self.k)), kind="flatline",
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
        if magnitude >= CRITICAL_SEVERITY_MULTIPLE * self.k:
            return "critical"
        if magnitude >= HIGH_SEVERITY_MULTIPLE * self.k:
            return "high"
        return "medium"
