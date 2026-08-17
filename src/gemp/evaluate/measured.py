"""Forecast and anomaly accuracy, from one fit pass over the seeded portfolio.

Everything in here needs the database, which is why it is a separate module the
solver claims never import. `python -m gemp.evaluate` runs without it; only
`--with-db` reaches this code.

Host-side callers need `GEMP_DB_HOST=127.0.0.1` and `GEMP_DB_PORT=5433`. Using
`localhost` costs 130 s per connection on Windows, and presents as slowness rather
than as an error.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from gemp.ml.evaluate import Scores, fit_all, load_ground_truth, score_sweep

log = logging.getLogger("gemp.evaluate.measured")


@dataclass
class Measurement:
    buildings: int
    model_median_mape: float
    baseline_median_mape: float
    beat_baseline: int
    scores: list[Scores]

    def scores_at(self, k: float) -> Scores:
        """The row for the configured threshold, which is the one the paper quotes."""
        for row in self.scores:
            if row.k == k:
                return row
        raise KeyError(f"no scores at k={k}; swept {[s.k for s in self.scores]}")


def measure(k_values: Sequence[float], test_hours: int = 24 * 14) -> Measurement:
    load_ground_truth()          # fail before spending minutes on a fit we cannot score
    fitted = fit_all(test_hours)
    if not fitted.results:
        raise ValueError(
            "no building had enough history to fit. Seed first: "
            "`python -m gemp.seed --months 6`"
        )

    scores = score_sweep(fitted, list(k_values))
    model = [r.model_metrics.mape for r in fitted.results]
    baseline = [r.baseline_metrics.mape for r in fitted.results]

    return Measurement(
        buildings=len(fitted.results),
        model_median_mape=statistics.median(model),
        baseline_median_mape=statistics.median(baseline),
        beat_baseline=sum(1 for r in fitted.results if r.beat_baseline),
        scores=scores,
    )
