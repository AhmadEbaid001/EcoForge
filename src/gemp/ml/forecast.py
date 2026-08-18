"""F10 - per-building load forecasting, with a baseline that is allowed to win.

The proposal (Section 4.5) sets the rule this module implements: the learned model is
retained only if it beats a seasonal-naive predictor on held-out data, and otherwise
the naive predictor is used instead. That rule is kept because it is the honest one.
A forecaster that loses to "same hour last week" and ships anyway is a liability, and
on well-behaved building load the naive predictor is genuinely hard to beat.

Model choice is `HistGradientBoostingRegressor`: already in scikit-learn, no new
dependency, trains fifty per-building models in well under a minute, and handles the
hour-of-week structure that dominates building load. Prophet was rejected - a Stan
backend that inflates image size and build time, and weak on sub-daily load.

That rejection IS F10, and this module is the whole of its resolution: the proposal's
Table 3 said "scikit-learn / Prophet", and what shipped is scikit-learn alone. The
finding is recorded here rather than only in the review because the paper has to be
able to point at the code that settles it. The seasonal-naive fallback F10 asked to
keep is `seasonal_naive` below; on the current portfolio it never fires - HGBR is the
retained model at all 50 buildings - so the rule reads as ceremony until the day a
building's history is too short or too erratic for it, which is exactly when a
forecaster that shipped without the check would be believed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from gemp.ml.features import (
    FEATURE_COLUMNS,
    MIN_PROFILE_KW,
    WARMUP_HOURS,
    split_by_time,
    training_frame,
)

log = logging.getLogger("gemp.ml.forecast")

MODEL_VERSION = "hgbr-2"
BASELINE_VERSION = "seasonal-naive-1"

SEASONAL_LAG_HOURS = 168        # same hour, previous week
MIN_TRAINING_HOURS = WARMUP_HOURS + 24 * 21

# Load is never negative, and a forecast that says otherwise is not a small error.
#
# The model was predicting down to -1.32 kW at the bottom of the overnight trough on
# holidays. Four hundredths of a per cent of all hours, and it did real damage: the
# anomaly detector divides by the expected value, so an expectation near zero turned
# an ordinary 1 kW reading into a robust z-score of 15,319 and a guaranteed alert.
# Clamping here is cheaper and more honest than teaching every consumer to distrust
# the column.
MIN_EXPECTED_KW = MIN_PROFILE_KW


@dataclass
class Metrics:
    mape: float
    rmse: float
    mae: float
    n: int

    def as_dict(self) -> dict:
        return {"mape": self.mape, "rmse": self.rmse, "mae": self.mae, "n": self.n}


@dataclass
class ForecastResult:
    building_id: str
    model_version: str
    chosen: str                      # "model" | "baseline"
    model_metrics: Metrics
    baseline_metrics: Metrics
    predictions: pd.Series = field(repr=False)
    annual_kwh: float = 0.0
    # Expected load across the WHOLE history, not only the held-out window.
    # Anomaly detection needs an expectation for every hour it examines; scoring only
    # the test window would leave 24 of 26 weeks of seeded history unexamined, which
    # is how a detector reports zero anomalies on data containing hundreds.
    full_predictions: pd.Series = field(repr=False, default_factory=pd.Series)

    @property
    def beat_baseline(self) -> bool:
        return self.chosen == "model"


def mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Mean absolute percentage error, over non-zero actuals.

    Zero actuals are excluded rather than nudged with an epsilon: a building drawing
    no power is a meter fault, and letting it contribute an unbounded percentage
    error would say more about the divide-by-zero guard than about the forecast.
    """
    mask = actual != 0
    if not mask.any():
        return float("inf")
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])) * 100)


def rmse(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def evaluate(actual: np.ndarray, predicted: np.ndarray) -> Metrics:
    return Metrics(
        mape=mape(actual, predicted),
        rmse=rmse(actual, predicted),
        mae=float(np.mean(np.abs(actual - predicted))),
        n=len(actual),
    )


def seasonal_naive(frame: pd.DataFrame, horizon: pd.DatetimeIndex | None = None) -> pd.Series:
    """Predict each hour with the same hour of the previous week.

    The mandated baseline. It is strong precisely because building load is a weekly
    occupancy schedule, and any model that cannot beat it has learned nothing the
    calendar did not already say.
    """
    shifted = frame["kw"].shift(SEASONAL_LAG_HOURS)
    if horizon is None:
        return shifted
    return shifted.reindex(horizon)


def _predict_kw(model, frame: pd.DataFrame) -> np.ndarray:
    """Ratio prediction back into kW, floored at zero.

    The floor belongs here rather than at the call sites: every consumer of a forecast
    - the annual estimate, the dashboards, the anomaly denominator - is entitled to
    assume a load is a load.
    """
    profile = frame["profile"].clip(lower=MIN_PROFILE_KW).to_numpy()
    return np.maximum(model.predict(frame[list(FEATURE_COLUMNS)]) * profile,
                      MIN_EXPECTED_KW)


def fit_building(
    readings: pd.DataFrame,
    building_id: str,
    test_hours: int = 24 * 14,
    random_state: int = 20260814,
) -> ForecastResult:
    """Train, evaluate against the baseline, and keep whichever wins."""
    frame = training_frame(readings)

    if len(frame) < MIN_TRAINING_HOURS:
        raise ValueError(
            f"{building_id}: {len(frame)} usable hours, need at least "
            f"{MIN_TRAINING_HOURS}. Seed more history before forecasting."
        )

    train, test = split_by_time(frame, test_hours)

    model = HistGradientBoostingRegressor(
        max_iter=300,
        learning_rate=0.08,
        max_depth=None,
        min_samples_leaf=20,
        l2_regularization=1.0,
        early_stopping=True,
        validation_fraction=0.15,
        random_state=random_state,
    )
    # Predict the RATIO to the hour-of-week profile, not the load itself.
    #
    # A tree adds leaf values, so in kW it can only express "a holiday costs this
    # building 14 kW at 09:00" - one constant per building, per hour, per calendar
    # state, learned from the handful of holidays in the training window. In ratio
    # space the same fact is "a holiday is 0.3x", one split that holds at every hour
    # and every season, and the diurnal and weekly shape is carried by the profile
    # instead of being rebuilt from lags that disagree with it at every step change.
    #
    # Measured on the seeded portfolio: held-out MAPE 3.83% -> 3.24%, and episode
    # precision at the anomaly gate 0.554 -> 0.827 at a recall of 0.875. The precision
    # gain is far larger than the MAPE gain because what it removes is not noise but a
    # systematic failure at one hour of the day.
    profile = train["profile"].clip(lower=MIN_PROFILE_KW)
    model.fit(train[list(FEATURE_COLUMNS)], train["kw"] / profile)

    actual = test["kw"].to_numpy()
    model_pred = _predict_kw(model, test)
    model_metrics = evaluate(actual, model_pred)

    # The baseline needs the full frame so it can look back a week from the start of
    # the test window, not only within it.
    baseline_pred = seasonal_naive(frame).loc[test.index]
    valid = baseline_pred.notna().to_numpy()
    baseline_metrics = (
        evaluate(actual[valid], baseline_pred.to_numpy()[valid])
        if valid.any()
        else Metrics(mape=float("inf"), rmse=float("inf"), mae=float("inf"), n=0)
    )

    if model_metrics.mape < baseline_metrics.mape:
        chosen, version = "model", MODEL_VERSION
        predictions = pd.Series(model_pred, index=test.index)
        full = pd.Series(_predict_kw(model, frame), index=frame.index)
    else:
        log.info(
            "%s: seasonal-naive wins (%.1f%% vs %.1f%% MAPE), using the baseline",
            building_id, baseline_metrics.mape, model_metrics.mape,
        )
        chosen, version = "baseline", BASELINE_VERSION
        predictions = baseline_pred
        full = seasonal_naive(frame)

    return ForecastResult(
        building_id=building_id,
        model_version=version,
        chosen=chosen,
        model_metrics=model_metrics,
        baseline_metrics=baseline_metrics,
        predictions=predictions,
        annual_kwh=annualize(frame["kw"]),
        full_predictions=full,
    )


def annualize(hourly_kw: pd.Series) -> float:
    """Annual consumption implied by an hourly load series (F3).

    This is the number the savings model multiplies by an end-use share, and so the
    single point at which the metered time series reaches the allocation decision.

    A trailing mean over whole weeks, not over whatever span happens to be available:
    an odd number of days would over-weight whichever weekdays it happened to contain,
    and with a Friday-Saturday weekend that bias is large.
    """
    if hourly_kw.empty:
        return 0.0

    whole_weeks = len(hourly_kw) // 168
    if whole_weeks >= 1:
        hourly_kw = hourly_kw.iloc[-whole_weeks * 168:]

    return float(hourly_kw.mean() * 8760)
