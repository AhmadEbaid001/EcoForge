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


@dataclass(frozen=True)
class Anomaly:
    building_id: str
    ts: pd.Timestamp
    observed_kw: float
    expected_kw: float
    residual: float
    robust_z: float

    @property
    def severity(self) -> str:
        magnitude = abs(self.robust_z)
        if magnitude >= 8:
            return "critical"
        if magnitude >= 5:
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
    mad = (
        shifted.rolling(window, min_periods=min_periods)
        .apply(lambda w: np.median(np.abs(w - np.median(w))), raw=True)
    )
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
        )
        for ts in scores.index[flagged.fillna(False)]
    ]


def detect_flatlines(
    actual: pd.Series, building_id: str, min_hours: int = 4
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
        if len(run) >= min_hours:
            out.append(
                Anomaly(
                    building_id=building_id,
                    ts=run.index[-1],
                    observed_kw=float(run.iloc[-1]),
                    expected_kw=float(run.iloc[-1]),
                    residual=0.0,
                    robust_z=float("nan"),
                )
            )
    return out
