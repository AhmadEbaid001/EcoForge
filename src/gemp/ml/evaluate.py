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
import json
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
from gemp.paths import anchor_dir, ground_truth_read_path

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
    # Alerts raised where no ground truth exists to judge them. Reported rather than
    # folded into precision: they are neither correct nor incorrect, and counting
    # them either way would be a claim the data cannot support.
    episodes_unscorable: int = 0

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
            f"{self.episodes_unscorable:>8,}  "
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


def _live_truth_path():
    """Faults injected by the running simulator node, appended as they happen."""
    return anchor_dir() / "live_anomalies.jsonl"


def split_live_runs(records: list[dict]) -> list[list[dict]]:
    """Group appended live records by the simulator run that wrote them.

    The file is append-ordered and each run advances its data clock monotonically, so
    a record whose start goes BACKWARDS is the first record of a new run. There is no
    run marker in the format, and adding one would not repair the file already on disk.
    """
    runs: list[list[dict]] = []
    previous = None
    for record in records:
        if previous is None or record["start"] < previous:
            runs.append([])
        runs[-1].append(record)
        previous = record["start"]
    return runs


def drop_rewound_runs(seed_end: datetime, runs: list[list[dict]]) -> list[list[dict]]:
    """Discard runs whose readings cannot have been stored.

    A simulator run that starts its data clock inside already-covered time publishes
    nothing but duplicates, and `insert_ignore` drops every one of them - while the
    run goes on recording the faults it "injected" into readings that were never
    written. Those events are not missed detections. There is no data in which to
    detect them, and counting them makes recall a measure of how badly the simulator
    restarted.

    Measured on this stack before `resume_point` was made to refuse a rewind: 1,762 of
    2,212 recorded events were phantoms of two rewound runs, and they read as recall
    0.375 for a detector whose recall on real data is 0.81. The monthly breakdown was
    unambiguous - 0.03 to 0.20 of events found across the rewound span, 0.87 to 0.90
    outside it.
    """
    kept, covered_until = [], seed_end
    for run in runs:
        start = min(r["start"] for r in run)
        end = max(r["end"] for r in run)
        if start < covered_until:
            log.warning(
                "dropping %d ground-truth events from a simulator run that rewound to "
                "%s, inside data already covered to %s - its readings were discarded "
                "as duplicates, so no data supports these events",
                len(run), start.isoformat(), covered_until.isoformat(),
            )
            continue
        kept.append(run)
        covered_until = max(covered_until, end)
    return kept


def load_ground_truth(path=None, live_path=None) -> pd.DataFrame:
    """Every injected fault: the seeded history plus whatever the live node added.

    Both sources are needed. `data/ground_truth.csv` covers only the seeded window,
    and the simulator keeps running past it at 720x - two hours of wall time is two
    months of data. Scoring the whole series against the seeded truth alone counts
    every correctly detected LIVE fault as a false alarm, which is not a small
    effect: measured on this stack it read episode precision as 0.26 where the
    detector's actual precision was 0.50.

    Live records are grouped into the runs that wrote them and rewound runs are
    dropped - see `drop_rewound_runs`, which exists because trusting this file cost
    the project a measurement it had already written down.
    """
    path = path or ground_truth_read_path()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. It is written by `python -m gemp.seed`; without it "
            f"precision and recall cannot be computed and k cannot be tuned."
        )

    with path.open(encoding="utf-8", newline="") as fh:
        rows = [
            {
                "building_id": r["building_id"],
                "kind": r["kind"],
                "start": datetime.fromisoformat(r["start"]),
                "end": datetime.fromisoformat(r["end"]),
                "source": "seed",
            }
            for r in csv.DictReader(fh)
        ]

    for row in rows:
        row["run"] = "seed"
    seed_end = max((row["end"] for row in rows), default=datetime.min)

    live_path = live_path or _live_truth_path()
    if live_path.exists():
        appended = []
        with live_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                appended.append({
                    "building_id": record["building_id"],
                    "kind": record["kind"],
                    "start": datetime.fromisoformat(record["start"]),
                    "end": datetime.fromisoformat(record["end"]),
                    "source": "live",
                })
        runs = drop_rewound_runs(seed_end, split_live_runs(appended))
        kept = sum(len(run) for run in runs)
        for index, run in enumerate(runs):
            for record in run:
                record["run"] = f"live-{index}"
                rows.append(record)
        log.info(
            "ground truth: %d events (seed %d + live %d over %d run(s); %d dropped as "
            "unsupported by any stored reading)",
            len(rows), len(rows) - kept, kept, len(runs), len(appended) - kept,
        )
    else:
        log.warning(
            "%s not found - only the seeded window has ground truth. Detections after "
            "it will be excluded from scoring rather than counted as false alarms.",
            live_path,
        )

    return pd.DataFrame(rows)


def truth_windows(truth: pd.DataFrame) -> list[tuple[datetime, datetime]]:
    """The intervals the ground truth actually covers, one per source.

    Outside them, an unflagged hour proves nothing and a flagged hour is unjudgeable -
    there is no record of whether a fault was injected. Scoring beyond them does not
    measure the detector, it measures how much data was collected after the last
    truth file was written.

    Per RUN, not one span from the earliest start to the latest end, and not per
    source either. The seeded window and the live windows are separated by however
    long the simulator ran while its ground-truth file was going nowhere - a gap of
    seven months of data time on this stack, because the `sim` container had no mount
    for `anchor/`. A single min-to-max span would swallow that gap and quietly resume
    counting correct detections in it as false alarms, which is the exact bug this
    function exists to remove. Grouping by source left the same hole open one level
    down: every restart of the simulator opens another gap inside the live records.
    """
    column = "run" if "run" in truth.columns else "source"
    windows = []
    for _run, group in truth.groupby(column):
        windows.append((group["start"].min(), group["end"].max()))
    return sorted(windows)


def score(
    detections: dict[str, list],
    truth: pd.DataFrame,
    *,
    min_episode_hours: int = 1,
    min_peak_z: float = 0.0,
) -> Scores:
    """Episode-level precision, recall and F1 - consistent units on both sides.

    Episodes outside the ground-truth window are DISCARDED, not counted against
    precision. That is the difference between reporting what the detector does and
    reporting how far the simulator has run since the truth file was last written.
    """
    tolerance = timedelta(hours=OVERLAP_TOLERANCE_HOURS)
    covered = truth_windows(truth)

    def is_judgeable(episode: Episode) -> bool:
        return any(
            episode.start >= start - tolerance and episode.end <= end + tolerance
            for start, end in covered
        )

    by_building: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in truth.itertuples():
        by_building.setdefault(row.building_id, []).append((row.start, row.end))

    flagged = sum(len(v) for v in detections.values())
    episodes: list[Episode] = []
    for building_id, anomalies in detections.items():
        episodes.extend(to_episodes(building_id, anomalies))

    # Episode-level suppression, applied before anything is judged. `k` alone only
    # moves a point ALONG one precision/recall curve; these two knobs were an attempt
    # to move the curve, by asking whether an alert looks like a fault rather than
    # only whether one hour looked extreme.
    #
    # They are off by default because the attempt failed, and they are kept so the
    # failure is reproducible rather than folklore. Over 64 combinations of k,
    # duration and peak z: duration trades recall for precision at roughly the same
    # rate as k itself, and a higher peak-z floor makes precision WORSE (0.607 to
    # 0.535 at k=8, 2-hour minimum). A frozen meter barely deviates while the largest
    # residuals are legitimate load the forecaster missed, so residual magnitude does
    # not separate real faults from misses on this data.
    surviving = [
        e for e in episodes
        if e.hours >= min_episode_hours and e.peak_z >= min_peak_z
    ]
    scorable = [e for e in surviving if is_judgeable(e)]
    if len(scorable) != len(surviving):
        log.info(
            "%d of %d episodes fall outside the ground-truth window and are not scored",
            len(surviving) - len(scorable), len(surviving),
        )

    true_positives = 0
    found: set[tuple[str, datetime]] = set()

    for episode in scorable:
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
        episodes=len(scorable),
        episodes_unscorable=len(episodes) - len(scorable),
        events=len(truth),
        events_found=len(found),
        true_positives=true_positives,
        flag_rate=0.0,
    )


@dataclass
class Fitted:
    """One fit pass over the whole portfolio.

    Fitting is the expensive part - minutes, against milliseconds to score - and it
    does not depend on `k`. Keeping the forecast results alongside the residual
    series is what lets the evaluation harness report forecast accuracy and anomaly
    accuracy from the same pass rather than fitting the portfolio twice.
    """

    series: dict[str, tuple[pd.Series, pd.Series]]      # building -> (actual, expected)
    results: list                                       # list[ForecastResult]
    total_points: int


def fit_all(test_hours: int = 24 * 14) -> Fitted:
    series = load_hourly_all(get_engine())

    fitted: dict[str, tuple[pd.Series, pd.Series]] = {}
    results = []
    total_points = 0
    for building_id, frame in series.items():
        try:
            result = fit_building(frame, building_id, test_hours=test_hours)
        except ValueError:
            continue
        actual = frame.set_index("ts")["kw"]
        expected = result.full_predictions.reindex(pd.DatetimeIndex(frame["ts"]))
        fitted[building_id] = (actual, expected)
        results.append(result)
        total_points += len(actual)

    log.info("fitted %d buildings, %d hourly points", len(fitted), total_points)
    return Fitted(series=fitted, results=results, total_points=total_points)


def score_sweep(fitted: Fitted, k_values: list[float]) -> list[Scores]:
    """Score an existing fit at each threshold."""
    params = load_params()
    truth = load_ground_truth()

    out: list[Scores] = []
    for k in k_values:
        detections = {
            building_id: detect_all(
                actual, expected, building_id,
                k=k, window_days=params.anomaly_window_days,
            )
            for building_id, (actual, expected) in fitted.series.items()
        }
        scores = score(detections, truth)
        scores.k = k
        scores.flag_rate = scores.flagged / fitted.total_points if fitted.total_points else 0.0
        out.append(scores)
        log.info("k=%.1f -> precision %.3f recall %.3f", k, scores.precision, scores.recall)

    return out


def sweep(k_values: list[float], test_hours: int = 24 * 14) -> list[Scores]:
    """Fit once, then score the detector at each threshold."""
    load_ground_truth()          # fail before spending minutes on a fit we cannot score
    return score_sweep(fit_all(test_hours), k_values)


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

    print("\n     k   flagged  episodes  unjudged  flag-rate  precision   recall"
          "      F1  events found")
    print(f"  {'-' * 96}")
    for scores in results:
        print(scores.row())

    best = max(results, key=lambda s: s.f1)
    print(f"\n  best F1 at k={best.k:g}: precision {best.precision:.3f}, "
          f"recall {best.recall:.3f}, flag rate {best.flag_rate:.2%}")
    print("  set anomaly_k in data/params.yaml accordingly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
