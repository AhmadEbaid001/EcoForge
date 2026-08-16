"""The Egyptian working calendar, shared by the simulator and the forecaster.

One definition, imported by both. When the simulator knew about holidays and the
feature builder did not, the model mispredicted holiday load by roughly seventy per
cent, the anomaly detector correctly reported the deviation, and the result was a
wall of false positives: the five worst days in the whole evaluation were all public
holidays, and the top twenty dates accounted for 58 per cent of every false alarm.

Giving the model the calendar is not leakage. Public holidays and Ramadan dates are
published years ahead; any real deployment would supply exactly this, and a load
forecaster that does not know when the building is closed is not a serious forecaster.
What *would* be leakage is telling the model about the injected faults, which nothing
here does.

Fixed-date holidays only. The moveable Islamic holidays shift roughly eleven days a
year against the Gregorian calendar and are not modelled - stated so that the gap is
a known limitation rather than an oversight.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

# (month, day). Egyptian fixed-date public holidays.
PUBLIC_HOLIDAYS: frozenset[tuple[int, int]] = frozenset({
    (1, 7),     # Coptic Christmas
    (1, 25),    # Revolution Day / Police Day
    (4, 25),    # Sinai Liberation Day
    (5, 1),     # Labour Day
    (6, 30),    # 30 June Revolution
    (7, 23),    # Revolution Day
    (10, 6),    # Armed Forces Day
})

# Ramadan 2026, approximately 18 February to 19 March. Working hours shorten and
# shift earlier, which changes the shape of the load curve for a month - far too
# large an effect to leave out of a model that is judged on percentage error.
RAMADAN_2026 = (date(2026, 2, 18), date(2026, 3, 19))


def is_holiday(when: datetime | date | pd.Timestamp) -> bool:
    return (when.month, when.day) in PUBLIC_HOLIDAYS


def is_ramadan(when: datetime | date | pd.Timestamp) -> bool:
    start, end = RAMADAN_2026
    as_date = when.date() if hasattr(when, "date") else when
    # Compared on month/day so the window applies in any year of simulated data.
    return (start.month, start.day) <= (as_date.month, as_date.day) <= (end.month, end.day)


def holiday_flags(index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Calendar features for a whole index at once.

    `is_day_after_holiday` exists because the lag features carry holiday load forward:
    the day after a holiday looks anomalous purely because `lag_24h` is a holiday
    value. The same mechanism a week later is why `is_week_after_holiday` is here too
    - `lag_168h` reaches back exactly that far.
    """
    dates = index.normalize()
    holiday = np.array([is_holiday(ts) for ts in dates], dtype=float)
    ramadan = np.array([is_ramadan(ts) for ts in dates], dtype=float)

    day_after = np.array(
        [is_holiday(ts - pd.Timedelta(days=1)) for ts in dates], dtype=float
    )
    week_after = np.array(
        [is_holiday(ts - pd.Timedelta(days=7)) for ts in dates], dtype=float
    )

    return {
        "is_holiday": holiday,
        "is_ramadan": ramadan,
        "is_day_after_holiday": day_after,
        "is_week_after_holiday": week_after,
    }
