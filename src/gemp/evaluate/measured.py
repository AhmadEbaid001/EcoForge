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
class IntegrityReport:
    buildings: int
    chains_ok: int
    anchored: int
    checkpoints_ok: int
    anchor_agrees_with_database: int
    failures: list[str]


def measure_integrity(sample: int = 5) -> IntegrityReport:
    """Verify real chains against the external anchor, not only against themselves.

    A chain that verifies against itself proves nothing about a deleted tail. This
    checks the property the platform actually claims: that the anchor written outside
    the database volume still agrees with what the database holds.
    """
    from sqlalchemy.orm import Session

    from gemp.config import get_settings
    from gemp.db import BuildingRow, get_engine
    from gemp.services import verify_building_chain

    engine = get_engine()
    failures: list[str] = []
    chains_ok = anchored = checkpoints_ok = agrees = 0

    with Session(bind=engine) as session:
        ids = [
            row.id for row in session.query(BuildingRow).order_by(BuildingRow.id).limit(sample)
        ]
        for building_id in ids:
            report = verify_building_chain(session, building_id, get_settings().key_bytes)
            chains_ok += bool(report["chain_ok"])
            anchored += bool(report["anchored"])
            checkpoints_ok += report["checkpoint_ok"] is True
            agrees += report["anchor_matches_database"] is True
            if not report["chain_ok"] or report["checkpoint_ok"] is False:
                failures.append(f"{building_id}: {report.get('hint') or report.get('break')}")

    return IntegrityReport(
        buildings=len(ids),
        chains_ok=chains_ok,
        anchored=anchored,
        checkpoints_ok=checkpoints_ok,
        anchor_agrees_with_database=agrees,
        failures=failures,
    )


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
