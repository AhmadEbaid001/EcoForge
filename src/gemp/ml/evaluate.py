"""Tuning the anomaly threshold against injected ground truth.

The proposal (Sections 4.2 and 4.5) requires `k` to be chosen so that the flag rate
is "neither negligible nor overwhelming", and states that precision and recall are
measurable because the simulator records the anomalies it injects. This module is
that measurement. Without it `k = 3` is a guess, and a guess is not something to put
in front of judges next to the word "tuned".

Both sides are measured in EPISODES, and getting that right mattered more than any
change to the detector itself.

An earlier version counted recall over events but precision over individual flagged
hours. Mixing units makes the F1 meaningless: a single real ten-hour fault that the
detector flags for six hours scores as six predictions rather than one correct alert,
so precision is divided by a number that has nothing to do with how many times anyone
was actually told something. Measured on the seeded portfolio, that mis-specification
alone reported precision around 0.19 for a detector whose episode precision is far
higher.

The unit that matters is the alert. Consecutive flagged hours are grouped into one
detection episode, and then:

* **Recall** - a ground-truth event is found if any episode overlaps it.
* **Precision** - an episode is correct if it overlaps any event for that building.

Episodes separated by less than `MERGE_GAP_HOURS` are merged, because a fault whose
signal dips below threshold for an hour in the middle is still one fault. Overlap is
tested with a small tolerance for the same reason a fault's boundary is not sharp:
lag features carry a disturbance for hours after the event itself ends.

    python -m gemp.ml.evaluate
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from gemp.db import get_engine
from gemp.domain.catalog import load_params
from gemp.ml.anomaly import detect_all
from gemp.ml.dataset import load_hourly_all
from gemp.ml.forecast import fit_building
from gemp.paths import data_dir

log = logging.getLogger("gemp.ml.evaluate")


# A fault whose signal dips below threshold for an hour or two is still one fault,
# not three. Merging avoids counting the gaps as separate false alarms.
MERGE_GAP_HOURS = 3

# Lag features carry a disturbance forward after the event itself ends, so a flag
# shortly after a real fault is a late alert rather than a false one.
OVERLAP_TOLERANCE_HOURS = 3


@dataclass
class Episode:
    """A run of consecutive flagged hours - one alert an operator would receive."""

    building_id: str
    start: datetime
    end: datetime
    peak_z: float
    hours: int


@dataclass
class Scores:
    k: float
    flagged: int
    episodes: int
    events: int
    events_found: int
    true_positives: int
    flag_rate: float

    @property
    def recall(self) -> float:
        return self.events_found / self.events if self.events else 0.0

    @property
    def precision(self) -> float:
        """Fraction of ALERTS that were real - episodes, not hours."""
        return self.true_positives / self.episodes if self.episodes else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def row(self) -> str:
        return (
            f"  {self.k:>4.1f}  {self.flagged:>8,}  {self.episodes:>8,}  "
            f"{self.flag_rate:>7.2%}  {self.precision:>9.3f}  {self.recall:>7.3f}  "
            f"{self.f1:>6.3f}  {self.events_found:>4}/{self.events}"
        )


def to_episodes(building_id: str, anomalies: list) -> list[Episode]:
    """Group consecutive flagged hours into the alerts an operator would receive."""
    if not anomalies:
        return []

    ordered = sorted(anomalies, key=lambda a: a.ts)
    episodes: list[Episode] = []
    start = prev = ordered[0].ts
    peak = abs(ordered[0].robust_z)
    hours = 1

    for anomaly in ordered[1:]:
        gap = (anomaly.ts - prev).total_seconds() / 3600
        if gap <= MERGE_GAP_HOURS:
            prev = anomaly.ts
            peak = max(peak, abs(anomaly.robust_z))
            hours += 1
            continue

        episodes.append(Episode(building_id, start.to_pydatetime(),
                                prev.to_pydatetime(), peak, hours))
        start = prev = anomaly.ts
        peak = abs(anomaly.robust_z)
        hours = 1

    episodes.append(Episode(building_id, start.to_pydatetime(),
                            prev.to_pydatetime(), peak, hours))
    return episodes


def load_ground_truth(path=None) -> pd.DataFrame:
    """Anomalies the simulator injected, written during seeding."""
    path = path or (data_dir() / "ground_truth.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. It is written by `python -m gemp.seed`; without it "
            f"precision and recall cannot be computed and k cannot be tuned."
        )

    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    return pd.DataFrame([
        {
            "building_id": r["building_id"],
            "kind": r["kind"],
            "start": datetime.fromisoformat(r["start"]),
            "end": datetime.fromisoformat(r["end"]),
        }
        for r in rows
    ])


def score(detections: dict[str, list], truth: pd.DataFrame) -> Scores:
    """Episode-level precision, recall and F1 - consistent units on both sides."""
    tolerance = timedelta(hours=OVERLAP_TOLERANCE_HOURS)

    by_building: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in truth.itertuples():
        by_building.setdefault(row.building_id, []).append((row.start, row.end))

    flagged = sum(len(v) for v in detections.values())
    episodes: list[Episode] = []
    for building_id, anomalies in detections.items():
        episodes.extend(to_episodes(building_id, anomalies))

    true_positives = 0
    found: set[tuple[str, datetime]] = set()

    for episode in episodes:
        matched = False
        for start, end in by_building.get(episode.building_id, []):
            # Interval overlap, widened by the tolerance on both sides.
            if episode.start <= end + tolerance and episode.end >= start - tolerance:
                matched = True
                found.add((episode.building_id, start))
        if matched:
            true_positives += 1

    return Scores(
        k=0.0,
        flagged=flagged,
        episodes=len(episodes),
        events=len(truth),
        events_found=len(found),
        true_positives=true_positives,
        flag_rate=0.0,
    )


def sweep(k_values: list[float], test_hours: int = 24 * 14) -> list[Scores]:
    """Fit once, then score the detector at each threshold.

    Fitting is the expensive part and does not depend on `k`, so it happens once and
    the residuals are reused across the sweep.
    """
    params = load_params()
    truth = load_ground_truth()
    series = load_hourly_all(get_engine())

    fitted = {}
    total_points = 0
    for building_id, frame in series.items():
        try:
            result = fit_building(frame, building_id, test_hours=test_hours)
        except ValueError:
            continue
        actual = frame.set_index("ts")["kw"]
        expected = result.full_predictions.reindex(pd.DatetimeIndex(frame["ts"]))
        fitted[building_id] = (actual, expected)
        total_points += len(actual)

    log.info("fitted %d buildings, %d hourly points", len(fitted), total_points)

    out: list[Scores] = []
    for k in k_values:
        detections = {
            building_id: detect_all(
                actual, expected, building_id,
                k=k, window_days=params.anomaly_window_days,
            )
            for building_id, (actual, expected) in fitted.items()
        }
        scores = score(detections, truth)
        scores.k = k
        scores.flag_rate = scores.flagged / total_points if total_points else 0.0
        out.append(scores)
        log.info("k=%.1f -> precision %.3f recall %.3f", k, scores.precision, scores.recall)

    return out


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--k", type=float, nargs="+",
                        default=[3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0])
    parser.add_argument("--test-hours", type=int, default=24 * 14)
    args = parser.parse_args(argv)

    try:
        results = sweep(args.k, args.test_hours)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    print("\n     k   flagged  flag-rate  precision   recall      F1  events found")
    print(f"  {'-' * 76}")
    for scores in results:
        print(scores.row())

    best = max(results, key=lambda s: s.f1)
    print(f"\n  best F1 at k={best.k:g}: precision {best.precision:.3f}, "
          f"recall {best.recall:.3f}, flag rate {best.flag_rate:.2%}")
    print("  set anomaly_k in data/params.yaml accordingly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
