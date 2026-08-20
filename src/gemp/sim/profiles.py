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
from functools import lru_cache

import numpy as np

from gemp.calendar_eg import PUBLIC_HOLIDAYS
from gemp.domain.models import Building

HOURS_PER_YEAR = 8760.0

# Shared with the forecaster's feature builder. When these two disagreed, the model
# had no way to know the building was shut and the detector reported the whole
# holiday as an anomaly.
HOLIDAYS = PUBLIC_HOLIDAYS

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
# Anchored at mid-month and interpolated - see `seasonal_factor`.
SEASONAL = [0.72, 0.74, 0.82, 0.92, 1.08, 1.24, 1.35, 1.34, 1.18, 1.00, 0.84, 0.74]

# Day-of-year midpoints of each month, for interpolation.
_MONTH_MID_DOY = np.array([15, 45, 74, 105, 135, 166, 196, 227, 258, 288, 319, 349])
# Wrapped on both ends so December interpolates smoothly into January.
_SEASONAL_X = np.concatenate(([_MONTH_MID_DOY[-1] - 365], _MONTH_MID_DOY,
                              [_MONTH_MID_DOY[0] + 365]))
_SEASONAL_Y = np.concatenate(([SEASONAL[-1]], SEASONAL, [SEASONAL[0]]))


def seasonal_factor(ts: datetime) -> float:
    """Cooling multiplier, interpolated smoothly across the year.

    Applying the monthly value as a step made load jump discontinuously at midnight
    on the first of each month - 17 per cent between April and May. Nothing in the
    data predicts that jump, because nothing causes it: real seasonal load follows
    temperature, which does not step on the calendar. The forecaster mispredicted
    every month boundary and the anomaly detector duly reported each one, so a
    modelling shortcut in the generator was manufacturing false positives in the
    detector.

    Interpolating between mid-month anchors keeps the same seasonal shape while
    making it continuous, and therefore learnable.
    """
    return float(np.interp(ts.timetuple().tm_yday, _SEASONAL_X, _SEASONAL_Y))

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


def shape_at(building: Building, ts: datetime) -> float:
    """Deterministic load shape at one instant, before noise and anomalies."""
    value = DIURNAL[building.occupancy_pattern][ts.hour]
    if ts.weekday() in WEEKEND_DAYS:
        value *= WEEKEND_FACTOR[building.occupancy_pattern]
    value *= seasonal_factor(ts)

    if (ts.month, ts.day) in HOLIDAYS:
        value *= 0.30
    elif building.occupancy_pattern != "admin_24x7" and (
        (ts.month == 2 and ts.day >= 18) or (ts.month == 3 and ts.day <= 19)
    ):
        value *= 0.80 if ts.hour >= 15 else 0.95

    return value


@lru_cache(maxsize=512)
def _mean_annual_shape(occupancy: str, weekend_factor: float) -> float:
    """Mean of the deterministic shape over a representative year.

    Cached per occupancy pattern - the shape depends only on the pattern, so fifty
    buildings share four computations.
    """
    diurnal = DIURNAL[occupancy]
    total, count = 0.0, 0
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2027, 1, 1, tzinfo=UTC)
    while ts < end:
        value = diurnal[ts.hour]
        if ts.weekday() in WEEKEND_DAYS:
            value *= weekend_factor
        value *= seasonal_factor(ts)
        if (ts.month, ts.day) in HOLIDAYS:
            value *= 0.30
        elif occupancy != "admin_24x7" and (
            (ts.month == 2 and ts.day >= 18) or (ts.month == 3 and ts.day <= 19)
        ):
            value *= 0.80 if ts.hour >= 15 else 0.95
        total += value
        count += 1
        ts += timedelta(hours=1)
    return total / count


def annual_scale(building: Building) -> float:
    """kW per unit of shape, such that a full year integrates to `annual_kwh`.

    Scaling is global rather than per-window on purpose. Normalising each generated
    span to its own pro-rata share of the annual total would force a January month and
    a July month to the same energy, erasing the seasonal cooling peak - which is one
    of the strongest signals in Egyptian building load and the main thing a forecaster
    should be picking up.
    """
    mean_shape = _mean_annual_shape(
        building.occupancy_pattern, WEEKEND_FACTOR[building.occupancy_pattern]
    )
    return building.annual_kwh / (mean_shape * HOURS_PER_YEAR)


def _base_shape(building: Building, timestamps: Sequence[datetime]) -> np.ndarray:
    """Deterministic load shape across a series of timestamps."""
    return np.fromiter(
        (shape_at(building, ts) for ts in timestamps), dtype=float, count=len(timestamps)
    )


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

    Scaled by the building's global annual factor, so a full year integrates to
    `annual_kwh` while a January month legitimately uses less energy than a July one.
    Anomalies are applied AFTER scaling, so they represent genuine excess rather than
    being normalised away.
    """
    # A synthetic load curve, not a secret.
    rng = random.Random(  # nosec B311
        seed if seed is not None else hash(building.id) & 0xFFFFFFFF)
    np_rng = np.random.default_rng(rng.randrange(2**32))

    timestamps = _timestamps(start, end, step_minutes)
    if not timestamps:
        raise ValueError("empty time range")

    shape = _base_shape(building, timestamps)
    shape *= np_rng.lognormal(mean=0.0, sigma=noise_cv, size=len(shape))
    kw = shape * annual_scale(building)

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


def point_kw(
    building: Building,
    ts: datetime,
    *,
    scale: float | None = None,
    noise_cv: float = 0.06,
    rng: np.random.Generator | None = None,
) -> float:
    """Load at a single instant, for the live simulator.

    Uses the same shape and the same global scale factor as `generate_series`, so a
    seeded backfill and the live stream that continues from it are drawn from one
    consistent process - a discontinuity at the handover would show up as a fleet-wide
    anomaly the moment the demonstration starts.
    """
    scale = annual_scale(building) if scale is None else scale
    value = shape_at(building, ts) * scale
    if noise_cv > 0:
        generator = rng or np.random.default_rng()
        value *= float(generator.lognormal(mean=0.0, sigma=noise_cv))
    return max(value, 0.0)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
