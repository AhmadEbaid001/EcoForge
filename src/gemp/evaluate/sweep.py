"""The grid of solver runs every optimizer claim is measured on.

One sweep, many claims. Expanding the candidate set is the expensive part and does
not depend on the budget, the objective or the cap, so it happens once and every
cell of the grid reuses it - the same reason the anomaly sweep fits once and scores
many thresholds (`gemp.ml.evaluate`).

A row is one (budget, objective, cap, solver) run. Rows carry the funded candidate
keys as well as the totals, because two of the claims are about WHICH options were
funded rather than how much benefit they delivered, and re-solving to answer that
would let the two answers drift apart.
"""

from __future__ import annotations

import csv
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gemp.domain.candidates import expand_portfolio
from gemp.domain.catalog import Params, load_catalog, load_params
from gemp.domain.models import Allocation, Building, Candidate, Intervention
from gemp.domain.portfolio import load_buildings
from gemp.optimize.objective import objective_fn
from gemp.optimize.runner import SOLVERS, solve

log = logging.getLogger("gemp.evaluate.sweep")

# The reporting range, and it is a function of the portfolio rather than a constant.
# It used to be 2-40 M, which was right when the fixture was mostly apartment
# blocks. The portfolio is public buildings now - ministries, hospitals, schools -
# and their retrofits cost proportionally more: 25 M funds twelve of the fifty,
# 100 M funds thirty-six, 200 M funds all of them. A sweep that stopped at 40 M
# would report the bottom fifth of the curve and miss the point where the solvers
# converge, which is exactly where several of the claims are measured.
DEFAULT_BUDGETS: tuple[float, ...] = tuple(
    float(m) * 1_000_000 for m in range(10, 210, 10)
)

# lca_carbon is the default objective; raw_kwh is what a conventional monitoring
# tool would rank on, and the pair is what the LCA claim compares. tou_carbon (A5)
# rides along so the old-vs-new divergence is measurable from the same grid -
# forgetting it here would leave the new objective with no sweep rows and its
# claim silently SKIPping.
DEFAULT_OBJECTIVES: tuple[str, ...] = ("lca_carbon", "tou_carbon", "raw_kwh")

# None is the unconstrained knapsack; 2 is the policy-style cap that greedy cannot
# express at all, and is where exact optimization earns its place.
DEFAULT_CAPS: tuple[int | None, ...] = (None, 2)

# `select(district_cap=None)` has to mean the unconstrained runs, so "don't filter"
# needs a value that is not None.
ANY = object()


@dataclass(frozen=True)
class SweepRow:
    """One solver run. Every field here is either reported or asserted on."""

    budget_egp: float
    objective: str
    district_cap: int | None
    solver: str
    status: str
    solve_ms: float
    funded: int
    spent_egp: float
    objective_total: float
    kwh_saving: float
    benefit_kgco2e: float
    egp_saving: float
    # Not written to CSV - fifty keys per row would drown the file. Kept in memory
    # so the funded-set claims read the same solve everything else was scored on.
    funded_keys: frozenset[str] = field(default=frozenset(), compare=False, repr=False)

    @property
    def budget_used_frac(self) -> float:
        return self.spent_egp / self.budget_egp if self.budget_egp else 0.0


CSV_FIELDS = (
    "budget_egp", "objective", "district_cap", "solver", "status", "solve_ms",
    "funded", "spent_egp", "budget_used_frac", "objective_total", "kwh_saving",
    "benefit_kgco2e", "egp_saving",
)


@dataclass(frozen=True)
class Instance:
    """The optimizer's whole input, loaded once.

    Deliberately free of database, broker and web framework, like `domain/` itself:
    the solver claims must stay measurable when nothing else is up.
    """

    buildings: list[Building]
    catalog: list[Intervention]
    params: Params
    candidates: list[Candidate]

    @classmethod
    def load(cls, max_bundle_size: int | None = None) -> Instance:
        params = load_params()
        catalog = load_catalog()
        buildings = load_buildings()
        candidates = expand_portfolio(buildings, catalog, params, max_bundle_size)
        log.info(
            "instance: %d buildings, %d interventions, %d candidate options",
            len(buildings), len(catalog), len(candidates),
        )
        return cls(buildings=buildings, catalog=catalog, params=params, candidates=candidates)


def to_row(
    alloc: Allocation,
    *,
    budget_egp: float,
    objective: str,
    district_cap: int | None,
) -> SweepRow:
    value_of = objective_fn(objective)
    total = sum(
        value_of(_as_candidate(item)) for item in alloc.items
    )
    return SweepRow(
        budget_egp=budget_egp,
        objective=objective,
        district_cap=district_cap,
        solver=alloc.solver,
        status=alloc.status,
        solve_ms=alloc.solve_ms,
        funded=alloc.buildings_funded,
        spent_egp=alloc.total_cost_egp,
        objective_total=total,
        kwh_saving=alloc.total_kwh_saving,
        benefit_kgco2e=alloc.total_benefit_kgco2e,
        egp_saving=alloc.total_egp_saving,
        funded_keys=frozenset(item.candidate_key for item in alloc.items),
    )


def _as_candidate(item) -> Candidate:
    from gemp.optimize.runner import _as_candidate as convert

    return convert(item)


def run_sweep(
    instance: Instance,
    *,
    budgets: Sequence[float] = DEFAULT_BUDGETS,
    objectives: Sequence[str] = DEFAULT_OBJECTIVES,
    caps: Sequence[int | None] = DEFAULT_CAPS,
    solvers: Sequence[str] = tuple(SOLVERS),
) -> list[SweepRow]:
    """Solve every cell of the grid. Order is stable so the CSV diffs cleanly."""
    rows: list[SweepRow] = []
    started = time.perf_counter()

    for objective in objectives:
        for cap in caps:
            for budget in budgets:
                for solver_name in solvers:
                    alloc = solve(
                        instance.candidates,
                        instance.buildings,
                        budget,
                        solver=solver_name,
                        objective=objective,
                        max_funded_per_district=cap,
                    )
                    rows.append(
                        to_row(alloc, budget_egp=budget, objective=objective, district_cap=cap)
                    )

    log.info("%d solves in %.1f s", len(rows), time.perf_counter() - started)
    return rows


def select(
    rows: Sequence[SweepRow],
    *,
    objective: str | None = None,
    district_cap: object = ANY,
    solver: str | None = None,
    budget_egp: float | None = None,
) -> list[SweepRow]:
    """Filter rows.

    `district_cap=None` means the UNCONSTRAINED runs, not "don't filter" - None is a
    real value of that field. `ANY` is the sentinel for "don't filter", which is why
    it exists rather than reusing None.
    """
    out = list(rows)
    if objective is not None:
        out = [r for r in out if r.objective == objective]
    if district_cap is not ANY:
        out = [r for r in out if r.district_cap == district_cap]
    if solver is not None:
        out = [r for r in out if r.solver == solver]
    if budget_egp is not None:
        out = [r for r in out if r.budget_egp == budget_egp]
    return out


def paired(
    rows: Sequence[SweepRow], left: str, right: str
) -> list[tuple[SweepRow, SweepRow]]:
    """Match two solvers' rows on the same (budget, objective, cap) instance.

    Comparing solvers means comparing them on identical instances; pairing by key
    rather than by list position is what stops a changed grid from silently
    comparing a capped run against an uncapped one.
    """
    index = {
        (r.budget_egp, r.objective, r.district_cap): r for r in rows if r.solver == right
    }
    out = []
    for row in rows:
        if row.solver != left:
            continue
        match = index.get((row.budget_egp, row.objective, row.district_cap))
        if match is not None:
            out.append((row, match))
    return out


def gap_pct(better: SweepRow, worse: SweepRow) -> float:
    """Percentage by which `better` exceeds `worse` on their shared objective.

    Undefined against a baseline that achieved nothing, reported as infinity rather
    than silently divided by zero - the same rule as `runner.improvement_pct`.
    """
    if worse.objective_total == 0:
        return float("inf") if better.objective_total > 0 else 0.0
    return (better.objective_total - worse.objective_total) / worse.objective_total * 100.0


def write_sweep_csv(rows: Sequence[SweepRow], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "budget_egp": f"{row.budget_egp:.0f}",
                    "objective": row.objective,
                    "district_cap": "" if row.district_cap is None else row.district_cap,
                    "solver": row.solver,
                    "status": row.status,
                    "solve_ms": f"{row.solve_ms:.1f}",
                    "funded": row.funded,
                    "spent_egp": f"{row.spent_egp:.0f}",
                    "budget_used_frac": f"{row.budget_used_frac:.4f}",
                    "objective_total": f"{row.objective_total:.0f}",
                    "kwh_saving": f"{row.kwh_saving:.0f}",
                    "benefit_kgco2e": f"{row.benefit_kgco2e:.0f}",
                    "egp_saving": f"{row.egp_saving:.0f}",
                }
            )
    return path
