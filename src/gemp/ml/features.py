"""Feature construction for per-building load forecasting.

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
)

# Longest lookback any feature needs. A row earlier than this has no complete
# history and is dropped rather than imputed - imputing a lag invents the very
# signal the model is supposed to learn.
WARMUP_HOURS = max(max(LAG_HOURS), max(ROLLING_WINDOWS))


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


def build_features(hourly: pd.DataFrame) -> pd.DataFrame:
    """Attach lag, rolling and calendar features to an hourly series."""
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
