"""Dominance pruning must be invisible: faster, never a different answer."""

from __future__ import annotations

import pytest
from tests.conftest import make_building
from tests.test_optimize import candidate

from gemp.domain.candidates import expand_portfolio
from gemp.domain.catalog import load_catalog, load_params
from gemp.domain.portfolio import load_buildings
from gemp.optimize.objective import OBJECTIVES
from gemp.optimize.prune import prune_dominated
from gemp.optimize.runner import SOLVERS, compare, solve


@pytest.fixture(scope="module")
def portfolio():
    params = load_params()
    catalog = load_catalog()
    buildings = load_buildings()
    return buildings, expand_portfolio(buildings, catalog, params)


def test_dominated_option_is_removed():
    """B costs more and delivers less, so no solution can prefer it."""
    candidates = [
        candidate("b1", "good", 100, 500),
        candidate("b1", "bad", 200, 400),
    ]
    kept = prune_dominated(candidates, "lca_carbon")
    assert [c.key for c in kept] == ["good"]


def test_pareto_frontier_survives():
    """Cheaper-but-weaker and dearer-but-stronger are both potentially optimal."""
    candidates = [
        candidate("b1", "cheap", 100, 300),
        candidate("b1", "dear", 200, 500),
    ]
    kept = prune_dominated(candidates, "lca_carbon")
    assert {c.key for c in kept} == {"cheap", "dear"}


def test_pruning_is_per_building():
    """An option is only ever dominated by another option at the SAME building."""
    candidates = [
        candidate("b1", "b1_weak", 200, 400),
        candidate("b2", "b2_strong", 100, 500),
    ]
    kept = prune_dominated(candidates, "lca_carbon")
    assert len(kept) == 2


def test_dominance_is_objective_specific():
    """An option dominated on carbon may sit on the frontier on raw kWh."""
    from gemp.domain.models import Candidate

    a = Candidate(
        key="a", building_id="b1", district="D", intervention_ids=("a",), label="a",
        cost_egp=100, annual_kwh_saving=10, lifetime_benefit_kgco2e=900,
        annual_egp_saving=1,
    )
    b = Candidate(
        key="b", building_id="b1", district="D", intervention_ids=("b",), label="b",
        cost_egp=120, annual_kwh_saving=90, lifetime_benefit_kgco2e=100,
        annual_egp_saving=1,
    )
    # On carbon, `a` is both cheaper and better, so `b` can be discarded.
    assert {c.key for c in prune_dominated([a, b], "lca_carbon")} == {"a"}

    # On raw kWh the same pair is a genuine trade-off - `b` costs more but saves
    # nine times the energy - so both must survive. Pruning at expansion time,
    # before the objective is known, would silently discard `b` here.
    assert {c.key for c in prune_dominated([a, b], "raw_kwh")} == {"a", "b"}


def test_pruning_removes_most_of_the_real_candidate_set(portfolio):
    _buildings, candidates = portfolio
    kept = prune_dominated(candidates, "lca_carbon")
    assert len(kept) < len(candidates) * 0.5


@pytest.mark.parametrize("solver", sorted(SOLVERS))
@pytest.mark.parametrize("objective", sorted(OBJECTIVES))
@pytest.mark.parametrize("budget", [2_000_000, 10_000_000, 40_000_000])
def test_pruning_does_not_change_any_solver_result(portfolio, solver, objective, budget):
    """The invariance the optimisation rests on, checked across the whole matrix."""
    buildings, candidates = portfolio

    pruned = solve(candidates, buildings, budget, solver=solver,
                   objective=objective, prune=True)
    full = solve(candidates, buildings, budget, solver=solver,
                 objective=objective, prune=False)

    assert pruned.total_benefit_kgco2e == pytest.approx(full.total_benefit_kgco2e)
    assert pruned.total_kwh_saving == pytest.approx(full.total_kwh_saving)
    assert pruned.buildings_funded == full.buildings_funded


def test_pruning_holds_under_the_district_cap(portfolio):
    """The cap constrains how many buildings are funded, not which option each
    takes, so dominance is unaffected by it."""
    buildings, candidates = portfolio
    pruned = compare(candidates, buildings, 10_000_000, max_funded_per_district=2)
    full = compare(candidates, buildings, 10_000_000, max_funded_per_district=2, prune=False)

    for name in SOLVERS:
        assert pruned[name].total_benefit_kgco2e == pytest.approx(
            full[name].total_benefit_kgco2e
        )


def test_solver_never_reports_infeasible():
    """Funding nothing is always feasible, so INFEASIBLE would signal a modelling bug.

    Worth pinning: a future side constraint written as an equality rather than an
    inequality would make the model infeasible at some budgets, and the failure would
    otherwise surface as a silently empty allocation on stage.
    """
    buildings = [make_building(id="b1", code="B1")]
    candidates = [candidate("b1", "only", 1_000_000, 5_000)]

    for budget in (0, 1, 999_999, 1_000_000, 10_000_000):
        alloc = solve(candidates, buildings, budget, solver="cpsat")
        assert alloc.status in {"OPTIMAL", "FEASIBLE"}
