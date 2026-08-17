"""The two figures the paper needs, drawn from the sweep it already ran.

Figures are generated from the same rows the claims were checked against, so a
figure can never show a run that no claim was measured on. matplotlib is a dev
dependency, not a runtime one: if it is missing the harness says so and continues,
because the CSV is the deliverable and the plot is a convenience.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from gemp.evaluate.sweep import SweepRow, gap_pct, paired, select

log = logging.getLogger("gemp.evaluate.figures")

SOLVER_LABELS = {
    "cpsat": "CP-SAT (exact)",
    "greedy_upgrade": "Greedy + upgrade pass",
    "greedy": "Greedy (density-ordered, saturates)",
    "equal_split": "Equal split (no optimizer)",
}


def draw_all(rows: Sequence[SweepRow], out_dir: Path, objective: str = "lca_carbon") -> list[Path]:
    try:
        import matplotlib
    except ImportError:
        log.warning("matplotlib not installed - skipping figures (pip install -e .[dev])")
        return []

    matplotlib.use("Agg")        # no display in CI, and none needed
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    written = [
        _benefit_curve(plt, rows, out_dir, objective),
        _gap_curve(plt, rows, out_dir, objective),
    ]
    return [p for p in written if p is not None]


def _benefit_curve(plt, rows: Sequence[SweepRow], out_dir: Path, objective: str) -> Path | None:
    """What the budget buys, per solver. The headline figure."""
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    plotted = False

    for solver in ("cpsat", "greedy_upgrade", "greedy", "equal_split"):
        series = sorted(
            select(rows, objective=objective, district_cap=None, solver=solver),
            key=lambda r: r.budget_egp,
        )
        if not series:
            continue
        ax.plot(
            [r.budget_egp / 1e6 for r in series],
            [r.objective_total / 1e6 for r in series],
            marker="o", markersize=3, label=SOLVER_LABELS.get(solver, solver),
        )
        plotted = True

    if not plotted:
        plt.close(fig)
        return None

    ax.set_xlabel("Budget (million EGP)")
    ax.set_ylabel("Life-cycle benefit (thousand tCO₂e)" if objective == "lca_carbon"
                  else f"Total {objective} (millions)")
    ax.set_title("What the budget buys, by allocation method")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()

    path = out_dir / f"benefit_vs_budget_{objective}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _gap_curve(plt, rows: Sequence[SweepRow], out_dir: Path, objective: str) -> Path | None:
    """Where exact optimization actually earns its place.

    Two lines, because the honest argument needs both: the unconstrained gap is
    small, and the capped gap is not.
    """
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    plotted = False

    for cap, label in ((None, "Unconstrained"), (2, "District cap = 2")):
        subset = select(rows, objective=objective, district_cap=cap)
        pairs = sorted(
            paired(subset, "cpsat", "greedy_upgrade"), key=lambda p: p[0].budget_egp
        )
        points = [
            (c.budget_egp / 1e6, gap_pct(c, g)) for c, g in pairs if g.objective_total > 0
        ]
        if not points:
            continue
        ax.plot([p[0] for p in points], [p[1] for p in points],
                marker="o", markersize=3, label=label)
        plotted = True

    if not plotted:
        plt.close(fig)
        return None

    ax.axhline(0, linewidth=0.8, color="black")
    ax.set_xlabel("Budget (million EGP)")
    ax.set_ylabel("CP-SAT advantage over greedy + upgrade (%)")
    ax.set_title("Exact optimization pays under side constraints, not without them")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()

    path = out_dir / f"cpsat_gap_{objective}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path
