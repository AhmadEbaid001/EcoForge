"""F10 - feature construction for per-building load forecasting.

F10 rejected Prophet and prescribed lag plus calendar features on
`HistGradientBoostingRegressor` instead. This is that feature set, and it has grown
past what the finding listed: the Egyptian weekend, the holiday and Ramadan flags,
and day-of-year are all additions measured to matter here. See `gemp.ml.forecast`
for the dependency decision itself.

Three families, in descending order of how much they actually explain building load:

1. **Lags.** The single strongest predictor of consumption at hour *t* is
   consumption at *t*-168h - the same hour last week. Building load is dominated by
   an occupancy schedule that repeats weekly.
2. **Calendar.** Hour of day and day of week, encoded cyclically so that hour 23 sits
   next to hour 0 rather than 23 units away. Plus the Egyptian Friday-Saturday
   weekend, which a model assuming a Western week gets wrong two days in seven, and
   the public-holiday and Ramadan flags from `gemp.calendar_eg`.
3. **Rolling statistics.** Recent mean and spread, which carry season and weather
   without needing a weather feed the project does not have.
4. **The hour-of-week profile**, and the lags expressed relative to it. This one was
   added last and mattered most; `PROFILE_WEEKS` below says why.

Deliberately absent: temperature. It would help - cooling load is the largest single
component - but the platform has no weather source, and inventing one would be a
worse answer than saying so.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gemp.calendar_eg import holiday_flags

# Egypt: the weekend is Friday and Saturday. pandas dayofweek is Mon=0 .. Sun=6.
WEEKEND_DAYS = {4, 5}

LAG_HOURS = (1, 2, 3, 24, 48, 168)
ROLLING_WINDOWS = (24, 168)

# Lags re-expressed as "how busy was that hour, relative to normal for that hour of
# the week". 40 kW says nothing on its own; 40 kW when this building normally draws
# 20 kW at 3am on a Tuesday says a great deal.
PROFILE_RATIO_LAGS = (1, 24, 168)

# Weeks of history behind the hour-of-week profile.
#
# This feature family is the one that closed F9's precision target, and the reason is
# worth stating because it is not "more features helped". Everything else here is a
# persistence signal: the model predicts hour t largely from hour t-1, and that works
# until the load STEPS. Egyptian load steps at midnight - into the Friday-Saturday
# weekend, into a public holiday, out of one - and at exactly those hours the lags say
# "yesterday evening was busy" while the truth is that the building is shut.
#
# Measured before this feature existed: the relative residual had a standard deviation
# of 0.125 at hour 00 and 0.096 at hour 01, against 0.06 for every other hour of the
# day. The detector scales residuals against a window pooled across all hours, so a
# systematically worse hour breaches the threshold on ordinary days - 640 of 1,379
# false alarms began at hour 00, and the dates were public holidays and Fridays.
#
# A median over four same-hour-of-week observations knows that Friday 00:00 is not
# Tuesday 00:00, and dividing the target by it turns the holiday shutdown into a
# multiplier the model can learn from `is_holiday` alone instead of a shape it has to
# rebuild from lags that contradict it.
#
# Four weeks, not more: the median needs enough samples to ignore a fault, and few
# enough that a genuine seasonal change is not held back by two months of stale
# summer. Two are required at minimum, so a building becomes forecastable three weeks
# in rather than five.
PROFILE_WEEKS = 4
PROFILE_MIN_WEEKS = 2

# Below this the profile is not a scale worth dividing by. Expressed in kW because a
# building drawing under a watt is a dead meter, not a quiet one.
MIN_PROFILE_KW = 1e-3

FEATURE_COLUMNS: tuple[str, ...] = (
    *(f"lag_{h}h" for h in LAG_HOURS),
    *(f"roll_mean_{w}h" for w in ROLLING_WINDOWS),
    *(f"roll_std_{w}h" for w in ROLLING_WINDOWS),
    "hour_sin", "hour_cos",
    "dow_sin", "dow_cos",
    "is_weekend",
    "month_sin", "month_cos",
    # Day-of-year resolves the seasonal curve inside a month; month indicators alone
    # force a 30-day plateau onto a signal that moves continuously.
    "doy_sin", "doy_cos",
    # Without these the model mispredicts holiday load by ~70%, and the anomaly
    # detector faithfully reports every hour of it. Measured: the five worst days in
    # the evaluation were all public holidays.
    "is_holiday", "is_ramadan", "is_day_after_holiday", "is_week_after_holiday",
    # Typical load for this hour of the week, and the lags divided by it.
    "profile",
    *(f"profile_ratio_lag_{h}h" for h in PROFILE_RATIO_LAGS),
)

# Longest lookback any feature needs. A row earlier than this has no complete
# history and is dropped rather than imputed - imputing a lag invents the very
# signal the model is supposed to learn.
#
# The profile dominates it: `PROFILE_MIN_WEEKS` observations of the same hour of the
# week, each a week apart, plus the shift that keeps the current hour out of its own
# expectation. Three weeks of warm-up for a feature worth a third of the false alarms
# is a trade the seeded six-month history can easily afford.
WARMUP_HOURS = max(
    max(LAG_HOURS),
    max(ROLLING_WINDOWS),
    168 * (PROFILE_MIN_WEEKS + 1),
)


def to_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Resample raw readings to an hourly mean load series.

    Readings arrive at 15-minute resolution with occasional dropouts. Hourly is the
    resolution the forecast is used at, and averaging absorbs isolated gaps without
    pretending the missing samples were observed.
    """
    if frame.empty:
        return frame

    series = (
        frame.set_index("ts")["kw"]
        .sort_index()
        .resample("1h")
        .mean()
    )
    return series.to_frame("kw")


def hour_of_week_profile(kw: pd.Series) -> pd.Series:
    """Typical load at this hour of the week, from this building's own recent past.

    Strictly causal, which is the whole difficulty. Grouping by (day of week, hour)
    and taking a trailing median inside each group means each point is compared with
    the same hour of previous weeks and never with itself: `shift(1)` moves the window
    back one WEEK, because within a group the neighbouring observation is seven days
    away.

    A median rather than a mean because the history contains faults. A stuck meter or
    a spike in one of the four weeks must not drag the expectation it is going to be
    measured against - the same reason the detector downstream uses MAD.
    """
    key = kw.index.dayofweek * 24 + kw.index.hour
    return kw.groupby(key).transform(
        lambda s: s.shift(1).rolling(PROFILE_WEEKS, min_periods=PROFILE_MIN_WEEKS).median()
    )


def build_features(hourly: pd.DataFrame) -> pd.DataFrame:
    """Attach lag, rolling, calendar and profile features to an hourly series."""
    out = hourly.copy()

    for hours in LAG_HOURS:
        out[f"lag_{hours}h"] = out["kw"].shift(hours)

    for window in ROLLING_WINDOWS:
        # `shift(1)` first: a rolling window that includes the current hour would
        # leak the target into its own features and produce a model that scores
        # beautifully in testing and fails in production.
        shifted = out["kw"].shift(1)
        out[f"roll_mean_{window}h"] = shifted.rolling(window, min_periods=window // 2).mean()
        out[f"roll_std_{window}h"] = shifted.rolling(window, min_periods=window // 2).std()

    index = out.index
    hour = index.hour.to_numpy()
    dow = index.dayofweek.to_numpy()
    month = index.month.to_numpy()
    doy = index.dayofyear.to_numpy()

    # Cyclical encoding: hour 23 and hour 0 are adjacent, and a tree model given a
    # raw 0-23 integer has to spend splits rediscovering that.
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    out["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    out["dow_cos"] = np.cos(2 * np.pi * dow / 7)
    out["month_sin"] = np.sin(2 * np.pi * (month - 1) / 12)
    out["month_cos"] = np.cos(2 * np.pi * (month - 1) / 12)
    out["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    out["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
    out["is_weekend"] = np.isin(dow, list(WEEKEND_DAYS)).astype(float)

    for name, values in holiday_flags(index).items():
        out[name] = values

    profile = hour_of_week_profile(out["kw"])
    out["profile"] = profile
    floor = profile.clip(lower=MIN_PROFILE_KW)
    for lag in PROFILE_RATIO_LAGS:
        out[f"profile_ratio_lag_{lag}h"] = out["kw"].shift(lag) / floor.shift(lag)

    return out


def training_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Readings in, model-ready rows out. Incomplete warm-up rows are dropped."""
    featured = build_features(to_hourly(frame))
    return featured.dropna(subset=["kw", *FEATURE_COLUMNS])


def split_by_time(
    frame: pd.DataFrame, test_hours: int = 24 * 14
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological hold-out: the most recent `test_hours` are the test set.

    Never a random split. Randomly shuffling a time series lets the model train on
    the future to predict the past, which inflates the score and means nothing about
    how it will behave tomorrow.
    """
    if len(frame) <= test_hours:
        raise ValueError(
            f"need more than {test_hours} hours of history to hold out a test set; "
            f"have {len(frame)}"
        )
    return frame.iloc[:-test_hours], frame.iloc[-test_hours:]
