"""Synthetic consumption profiles for the virtual sensor nodes.

Two design rules matter more than the shape details.

**Energy conservation.** The series for a building is scaled so its integral equals
that building's `annual_kwh` from the portfolio. Without this the forecast-derived
annual estimate would drift away from the profile the optimizer was costed against,
and savings figures would quietly stop meaning anything.

**Realistic difficulty.** A forecaster that scores a 2% MAPE on a clean sine wave
demonstrates nothing, and a judge who has fitted a time-series model before will say
so. The generator therefore includes the things that actually make building load hard
to predict: an Egyptian Friday-Saturday weekend, a summer cooling peak, public and
Ramadan schedule shifts, meter dropouts and stuck-meter flatlines.

Injected anomalies are logged to ground truth as they are created, which is what
makes precision and recall reportable rather than eyeballed (proposal Section 4.5).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from gemp.domain.models import Building

HOURS_PER_YEAR = 8760.0

# Egypt: the weekend is Friday and Saturday. datetime.weekday() is Mon=0 .. Sun=6.
WEEKEND_DAYS = {4, 5}

# Hour-of-day occupancy shape per pattern, 24 values, arbitrary scale - the series is
# normalised afterwards so only the SHAPE matters here.
DIURNAL: dict[str, list[float]] = {
    "office": [
        .30, .28, .27, .27, .28, .32, .45, .70, .95, 1.00, 1.00, .98,
        .92, .96, 1.00, .98, .90, .70, .50, .40, .36, .34, .32, .31,
    ],
    "school": [
        .22, .20, .20, .20, .22, .30, .55, .85, 1.00, 1.00, 1.00, .95,
        .80, .60, .45, .35, .30, .28, .26, .25, .24, .23, .23, .22,
    ],
    "clinic": [
        .45, .42, .40, .40, .42, .50, .65, .85, 1.00, 1.00, .98, .95,
        .90, .92, .95, .92, .85, .75, .68, .62, .58, .54, .50, .47,
    ],
    "admin_24x7": [
        .70, .68, .66, .65, .66, .70, .80, .90, 1.00, 1.00, 1.00, .98,
        .95, .97, 1.00, .98, .92, .86, .82, .80, .78, .76, .74, .72,
    ],
}

# Weekend load as a fraction of the weekday shape.
WEEKEND_FACTOR = {"office": 0.35, "school": 0.20, "clinic": 0.75, "admin_24x7": 0.90}

# Monthly cooling multiplier, January..December. Cairo: hot summers drive HVAC.
SEASONAL = [0.72, 0.74, 0.82, 0.92, 1.08, 1.24, 1.35, 1.34, 1.18, 1.00, 0.84, 0.74]

ANOMALY_KINDS = ("stuck_on", "spike", "drift", "flatline")


@dataclass(frozen=True)
class AnomalyEvent:
    """One injected anomaly, recorded as ground truth for evaluation."""

    building_id: str
    kind: str
    start: datetime
    end: datetime
    magnitude: float

    def covers(self, ts: datetime) -> bool:
        return self.start <= ts <= self.end


@dataclass
class Series:
    building_id: str
    timestamps: list[datetime]
    kw: np.ndarray
    anomalies: list[AnomalyEvent] = field(default_factory=list)
    dropouts: int = 0

    def __len__(self) -> int:
        return len(self.timestamps)

    def total_kwh(self, step_minutes: int) -> float:
        return float(self.kw.sum()) * (step_minutes / 60.0)


def _timestamps(start: datetime, end: datetime, step_minutes: int) -> list[datetime]:
    step = timedelta(minutes=step_minutes)
    out, ts = [], start
    while ts < end:
        out.append(ts)
        ts += step
    return out


def _base_shape(building: Building, timestamps: Sequence[datetime]) -> np.ndarray:
    """Deterministic load shape, before noise, anomalies and normalisation."""
    diurnal = np.array(DIURNAL[building.occupancy_pattern])
    weekend = WEEKEND_FACTOR[building.occupancy_pattern]

    shape = np.empty(len(timestamps))
    for i, ts in enumerate(timestamps):
        value = diurnal[ts.hour]
        if ts.weekday() in WEEKEND_DAYS:
            value *= weekend
        value *= SEASONAL[ts.month - 1]
        shape[i] = value
    return shape


def _apply_holidays(
    shape: np.ndarray, timestamps: Sequence[datetime], building: Building, rng: random.Random
) -> None:
    """Public holidays and a Ramadan schedule shift.

    Both are real features of Egyptian building load and both are exactly the kind of
    structure a naive seasonal baseline gets wrong, which is the point of including
    them.
    """
    holiday_days = {(1, 7), (1, 25), (4, 25), (5, 1), (6, 30), (7, 23), (10, 6)}

    for i, ts in enumerate(timestamps):
        if (ts.month, ts.day) in holiday_days:
            shape[i] *= 0.30
        # Ramadan approximated for 2026: roughly 18 Feb - 19 Mar. Working hours shift
        # earlier and shorten.
        elif building.occupancy_pattern != "admin_24x7" and (
            (ts.month == 2 and ts.day >= 18) or (ts.month == 3 and ts.day <= 19)
        ):
            shape[i] *= 0.80 if ts.hour >= 15 else 0.95


def _inject_anomalies(
    series_kw: np.ndarray,
    timestamps: Sequence[datetime],
    building: Building,
    rng: random.Random,
    per_month: float,
    step_minutes: int,
) -> list[AnomalyEvent]:
    """Add operational faults, and record each one as ground truth."""
    if per_month <= 0:
        return []

    span_days = (timestamps[-1] - timestamps[0]).days or 1
    count = max(1, int(round(per_month * span_days / 30.0)))
    steps_per_hour = 60 // step_minutes

    events: list[AnomalyEvent] = []
    for _ in range(count):
        kind = rng.choice(ANOMALY_KINDS)
        start_index = rng.randrange(0, max(1, len(timestamps) - 200))

        if kind == "stuck_on":
            # Equipment left running overnight: load holds at its daytime level.
            duration = rng.randint(6, 14) * steps_per_hour
            end_index = min(len(series_kw), start_index + duration)
            magnitude = rng.uniform(1.6, 2.6)
            series_kw[start_index:end_index] *= magnitude

        elif kind == "spike":
            duration = rng.randint(1, 3) * steps_per_hour
            end_index = min(len(series_kw), start_index + duration)
            magnitude = rng.uniform(2.5, 4.0)
            series_kw[start_index:end_index] *= magnitude

        elif kind == "drift":
            # Failing plant: consumption creeps up over days.
            duration = rng.randint(3, 10) * 24 * steps_per_hour
            end_index = min(len(series_kw), start_index + duration)
            magnitude = rng.uniform(1.25, 1.60)
            ramp = np.linspace(1.0, magnitude, end_index - start_index)
            series_kw[start_index:end_index] *= ramp

        else:  # flatline - stuck meter reporting a constant value
            duration = rng.randint(4, 20) * steps_per_hour
            end_index = min(len(series_kw), start_index + duration)
            magnitude = float(series_kw[start_index])
            series_kw[start_index:end_index] = magnitude

        events.append(
            AnomalyEvent(
                building_id=building.id,
                kind=kind,
                start=timestamps[start_index],
                end=timestamps[min(end_index, len(timestamps) - 1)],
                magnitude=float(magnitude),
            )
        )

    return events


def generate_series(
    building: Building,
    start: datetime,
    end: datetime,
    *,
    step_minutes: int = 15,
    seed: int | None = None,
    noise_cv: float = 0.06,
    anomalies_per_month: float = 1.5,
    dropout_rate: float = 0.001,
) -> Series:
    """A consumption series for one building over [start, end).

    Scaled so the integral matches the building's annual consumption, pro-rated for
    the span. Anomalies are applied AFTER scaling, so they represent genuine excess
    rather than being normalised away.
    """
    rng = random.Random(seed if seed is not None else hash(building.id) & 0xFFFFFFFF)
    np_rng = np.random.default_rng(rng.randrange(2**32))

    timestamps = _timestamps(start, end, step_minutes)
    if not timestamps:
        raise ValueError("empty time range")

    shape = _base_shape(building, timestamps)
    _apply_holidays(shape, timestamps, building, rng)
    shape *= np_rng.lognormal(mean=0.0, sigma=noise_cv, size=len(shape))

    # Scale so total energy over the span matches the building's annual figure.
    span_hours = (end - start).total_seconds() / 3600.0
    target_kwh = building.annual_kwh * (span_hours / HOURS_PER_YEAR)
    step_hours = step_minutes / 60.0
    kw = shape * (target_kwh / (shape.sum() * step_hours))

    events = _inject_anomalies(
        kw, timestamps, building, rng, anomalies_per_month, step_minutes
    )

    # Meter dropouts: readings that simply never arrive. Not anomalies to detect -
    # gaps the pipeline has to tolerate without crashing or silently interpolating.
    keep = np_rng.random(len(kw)) >= dropout_rate
    dropped = int((~keep).sum())

    return Series(
        building_id=building.id,
        timestamps=[t for t, k in zip(timestamps, keep, strict=True) if k],
        kw=kw[keep],
        anomalies=events,
        dropouts=dropped,
    )


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
