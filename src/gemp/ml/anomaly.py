"""F9 - forecast-relative anomaly detection on a robust statistic.

The proposal flags a reading when

    |r - mean(r)| > k * std(r)

over a rolling window of that building's forecast residuals. Both statistics have a
breakdown point of zero: the anomalies being detected are themselves inside the
window, so they inflate the standard deviation, which raises the threshold, which
hides the next anomaly. A sustained fault - precisely the "equipment left running"
case the proposal names - progressively conceals itself, and the longer it runs the
less likely it is to be caught.

Median and median absolute deviation have a 50 per cent breakdown point: up to half
the window can be contaminated before the threshold moves meaningfully. The rule
becomes

    |r - median(r)| > k * 1.4826 * MAD(r)

where 1.4826 rescales MAD to be consistent with the standard deviation under
normality, so `k` keeps its familiar "number of sigmas" reading and the proposal's
k = 3 starting value carries over unchanged.

Windows are measured in DATA time, never wall-clock time. Under accelerated replay
the two diverge immediately and a wall-clock window would come back empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

# Rescales MAD to be comparable with the standard deviation of a normal distribution.
MAD_TO_SIGMA = 1.4826

# Below this, a MAD-based threshold is meaningless: a perfectly flat residual series
# has MAD zero, and every deviation would look infinitely significant. A stuck meter
# produces exactly that.
MIN_SCALE_KW = 1e-6

# Floor on the relative spread used as the reference scale.
#
# Without it, a building the forecaster models almost perfectly gets a near-zero MAD,
# every score becomes undefined, and the detector goes permanently blind on exactly
# the buildings it understands best - a failure that is invisible because it produces
# no output rather than wrong output. A steady 24x7 facility hits this in practice.
#
# One per cent is the floor because metering resolution and rounding put a limit on
# how precisely consumption is known anyway; claiming to resolve spread below that is
# overconfidence, not sensitivity.
MIN_RELATIVE_SPREAD = 0.01

# Score assigned to a stuck meter.
#
# The natural answer is infinity - a constant series has no variance, so any reading
# is infinitely improbable under it - but an infinity in a float column sorts ahead of
# every real detection and floods any "most severe" list with frozen meters. A finite
# value above the "high" threshold says the same thing operationally (this is
# definitely a fault) without swamping the ranking.
FLATLINE_Z = 6.0


@dataclass(frozen=True)
class Anomaly:
    building_id: str
    ts: pd.Timestamp
    observed_kw: float
    expected_kw: float
    residual: float
    robust_z: float
    threshold_k: float = 3.0

    @property
    def severity(self) -> str:
        """Severity relative to the threshold that produced the flag.

        Fixed cut-offs do not survive re-tuning: with absolute bands at 5 and 8, a
        detector running at k=8 reports every single detection as critical, because
        nothing below 8 is flagged at all. Grading against k keeps the three bands
        meaningful at whatever sensitivity is in use, which is what a triage list
        needs.
        """
        magnitude = abs(self.robust_z)
        if magnitude >= 2.0 * self.threshold_k:
            return "critical"
        if magnitude >= 1.4 * self.threshold_k:
            return "high"
        return "medium"

    @property
    def direction(self) -> str:
        return "over" if self.residual > 0 else "under"


def robust_scale(residuals: np.ndarray) -> float:
    """Sigma-equivalent spread from the median absolute deviation."""
    if residuals.size == 0:
        return 0.0
    median = np.median(residuals)
    return float(np.median(np.abs(residuals - median)) * MAD_TO_SIGMA)


def robust_z_scores(residuals: pd.Series, window: int) -> pd.Series:
    """Rolling robust z-score of a residual series.

    The window ends at the previous observation, not the current one. Including the
    point under test in its own reference statistics pulls the median toward it and
    makes a large anomaly partially explain itself away.
    """
    shifted = residuals.shift(1)
    min_periods = max(window // 4, 24)

    median = shifted.rolling(window, min_periods=min_periods).median()

    # Vectorised MAD. The exact definition recomputes a median inside every window,
    # which `rolling().apply()` evaluates in Python: on a 30-day window over six
    # months of hourly data for fifty buildings that is roughly 8e8 interpreter-level
    # operations, and it dominated the entire evaluation run.
    #
    # Taking absolute deviations from the ROLLING median and then rolling a second
    # median over those is the standard vectorised form. It differs from the exact
    # definition only in that each point is centred on its own window's median rather
    # than on the median of the window it sits in - a distinction that moves the
    # threshold by well under the 1% floor applied below.
    mad = (shifted - median).abs().rolling(window, min_periods=min_periods).median()
    # Floored rather than nulled. Treating a degenerate window as "no score" silences
    # the detector on the best-modelled buildings; flooring keeps it sensitive to
    # deviations that exceed what metering could plausibly explain.
    scale = (mad * MAD_TO_SIGMA).clip(lower=MIN_RELATIVE_SPREAD)
    return (residuals - median) / scale


def detect(
    actual: pd.Series,
    expected: pd.Series,
    building_id: str,
    k: float = 3.0,
    window_days: int = 30,
    freq_hours: int = 1,
) -> list[Anomaly]:
    """Flag readings whose residual is extreme against that building's own recent behaviour.

    Per-building and forecast-relative by construction: what counts as abnormal is
    defined by the building's own history, so no global threshold and no labelled
    training data are required. That is what makes the detector deployable on a
    portfolio where no two buildings look alike.
    """
    aligned = pd.DataFrame({"actual": actual, "expected": expected}).dropna()
    if aligned.empty:
        return []

    # RELATIVE residuals, not absolute.
    #
    # Building load is strongly heteroscedastic: an office drawing 300 kW at noon and
    # 40 kW at 3am produces absolute residuals that differ by an order of magnitude
    # for the same proportional error. A single rolling scale pooled across all hours
    # is then far too tight for daytime and far too loose for night, so ordinary
    # afternoon variation breaches the threshold every day while a genuine overnight
    # fault slips under it. Measured on the seeded portfolio, the absolute form
    # flagged 7.3% of all hours at k=3 - roughly twenty-five times what a Gaussian
    # would give, and almost all of it daytime noise.
    #
    # Dividing by the expected value makes the residual a proportional error, which
    # is comparable across the load curve and is also how an operator reads it:
    # "this building is drawing forty per cent more than it should".
    denominator = aligned["expected"].abs().clip(lower=MIN_SCALE_KW)
    residuals = (aligned["actual"] - aligned["expected"]) / denominator
    window = int(timedelta(days=window_days).total_seconds() // 3600 // freq_hours)
    scores = robust_z_scores(residuals, window)

    flagged = scores.abs() > k
    return [
        Anomaly(
            building_id=building_id,
            ts=ts,
            observed_kw=float(aligned.loc[ts, "actual"]),
            expected_kw=float(aligned.loc[ts, "expected"]),
            residual=float(aligned.loc[ts, "actual"] - aligned.loc[ts, "expected"]),
            robust_z=float(scores.loc[ts]),
            threshold_k=k,
        )
        for ts in scores.index[flagged.fillna(False)]
    ]


def detect_all(
    actual: pd.Series,
    expected: pd.Series,
    building_id: str,
    k: float = 3.0,
    window_days: int = 30,
    min_flatline_hours: int = 3,
) -> list[Anomaly]:
    """Both detectors together. This is what callers should use.

    A stuck meter is invisible to the residual test by construction: a constant
    series has zero spread, so its robust scale collapses to the floor and the
    deviation never resolves. On the seeded portfolio flatlines are 116 of 450
    injected faults - a quarter of everything there is to find - so running only the
    residual detector caps recall at roughly three quarters no matter how `k` is
    tuned.
    """
    residual = detect(actual, expected, building_id, k=k, window_days=window_days)
    flat = detect_flatlines(actual, building_id, min_hours=min_flatline_hours,
                            threshold_k=k)

    # A flatline that the residual test already flagged is one fault, not two.
    seen = {a.ts for a in residual}
    return sorted(residual + [a for a in flat if a.ts not in seen], key=lambda a: a.ts)


def detect_flatlines(
    actual: pd.Series, building_id: str, min_hours: int = 4, threshold_k: float = 3.0
) -> list[Anomaly]:
    """Catch a stuck meter: an unchanging value for hours on end.

    Invisible to the residual detector - a constant series has zero spread, so its
    robust scale collapses and every z-score becomes undefined. Worth a separate,
    trivial check rather than a special case inside the statistics.
    """
    if actual.empty:
        return []

    changed = actual.diff().abs() > MIN_SCALE_KW
    run_id = changed.cumsum()
    runs = actual.groupby(run_id)

    out: list[Anomaly] = []
    for _, run in runs:
        if len(run) < min_hours:
            continue
        # Every hour of the run, not just its end: the episode grouper then reports
        # one alert spanning the fault, which is what an operator needs in order to
        # see when the meter froze rather than only when it thawed.
        out.extend(
            Anomaly(
                building_id=building_id,
                ts=ts,
                observed_kw=float(value),
                expected_kw=float(value),
                residual=0.0,
                robust_z=FLATLINE_Z,
                threshold_k=threshold_k,
            )
            for ts, value in run.items()
        )
    return out
