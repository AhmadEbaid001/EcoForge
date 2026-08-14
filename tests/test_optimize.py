"""F1 and F8 - the optimizer and its baselines."""

from __future__ import annotations

import pytest
from tests.conftest import make_building

from gemp.domain.models import Candidate
from gemp.optimize.objective import objective_fn
from gemp.optimize.runner import compare, solve


def candidate(bid: str, key: str, cost: float, benefit: float, district: str = "D1") -> Candidate:
    return Candidate(
        key=key,
        building_id=bid,
        district=district,
        intervention_ids=(key,),
        label=key,
        cost_egp=cost,
        annual_kwh_saving=benefit / 13.5,
        lifetime_benefit_kgco2e=benefit,
        annual_egp_saving=benefit / 10.0,
    )


@pytest.fixture
def tiny():
    """Three buildings, two options each, budget 100. Optimum solvable by hand.

        b1: A cost 50 benefit 60   |  B cost 30 benefit 30
        b2: C cost 50 benefit 55   |  D cost 20 benefit 18
        b3: E cost 50 benefit 50   |  F cost 20 benefit 15

    Best feasible under budget 100 picking at most one per building:
        A + C = cost 100, benefit 115.   <- optimum
    Density order is B (1.00), A (1.20)... so a density-greedy run takes A (1.20),
    C (1.10), then cannot afford anything else: also 115 here.
    """
    buildings = [
        make_building(id="b1", code="B1"),
        make_building(id="b2", code="B2"),
        make_building(id="b3", code="B3"),
    ]
    candidates = [
        candidate("b1", "A", 50, 60),
        candidate("b1", "B", 30, 30),
        candidate("b2", "C", 50, 55),
        candidate("b2", "D", 20, 18),
        candidate("b3", "E", 50, 50),
        candidate("b3", "F", 20, 15),
    ]
    return buildings, candidates


def test_ilp_finds_the_hand_computed_optimum(tiny):
    buildings, candidates = tiny
    alloc = solve(candidates, buildings, 100, solver="cpsat")

    assert alloc.status == "OPTIMAL"
    assert alloc.total_benefit_kgco2e == pytest.approx(115.0)
    assert {i.candidate_key for i in alloc.items} == {"A", "C"}


def test_ilp_never_exceeds_the_budget(tiny):
    buildings, candidates = tiny
    for budget in (0, 19, 20, 49, 50, 99, 100, 1000):
        alloc = solve(candidates, buildings, budget, solver="cpsat")
        assert alloc.total_cost_egp <= budget


def test_zero_budget_funds_nothing(tiny):
    buildings, candidates = tiny
    alloc = solve(candidates, buildings, 0, solver="cpsat")
    assert alloc.items == []
    assert alloc.total_benefit_kgco2e == 0


def test_unlimited_budget_funds_the_best_option_at_every_building(tiny):
    buildings, candidates = tiny
    alloc = solve(candidates, buildings, 10_000_000, solver="cpsat")
    assert alloc.buildings_funded == 3
    assert {i.candidate_key for i in alloc.items} == {"A", "C", "E"}


def test_at_most_one_option_per_building(tiny):
    buildings, candidates = tiny
    alloc = solve(candidates, buildings, 10_000_000, solver="cpsat")
    ids = [i.building_id for i in alloc.items]
    assert len(ids) == len(set(ids))


def test_ilp_is_never_worse_than_greedy(tiny):
    buildings, candidates = tiny
    for budget in (30, 50, 70, 100, 150, 200):
        results = compare(candidates, buildings, budget)
        assert (
            results["cpsat"].total_benefit_kgco2e
            >= results["greedy"].total_benefit_kgco2e - 1e-6
        ), f"greedy beat the exact solver at budget {budget}"


def test_ilp_strictly_beats_greedy_when_greedy_is_trapped():
    """The instance where a density heuristic provably loses.

    Two buildings, budget 100.
        b1: cheap  cost 10 benefit 15  (density 1.50)
            big    cost 90 benefit 95  (density 1.06)
        b2: mid    cost 95 benefit 99  (density 1.04)

    Greedy takes `cheap` first for its density, then cannot afford `mid` (95 > 90
    remaining) and cannot take `big` because b1 is already used - landing on 15.
    The optimum is `mid` alone, at 99.
    """
    buildings = [make_building(id="b1", code="B1"), make_building(id="b2", code="B2")]
    candidates = [
        candidate("b1", "cheap", 10, 15),
        candidate("b1", "big", 90, 95),
        candidate("b2", "mid", 95, 99),
    ]
    results = compare(candidates, buildings, 100)
    assert results["cpsat"].total_benefit_kgco2e > results["greedy"].total_benefit_kgco2e


def test_district_cap_is_respected_and_greedy_cannot_plan_around_it():
    """F8 - the constraint that justifies exact optimization.

    Four buildings in one district, cap of 2. Greedy commits its two slots to the
    highest-density options, which happen to be small; the exact solver spends the
    same budget on the two that actually deliver.
    """
    buildings = [make_building(id=f"b{i}", code=f"B{i}") for i in range(1, 5)]
    candidates = [
        candidate("b1", "small1", 10, 20),     # density 2.0
        candidate("b2", "small2", 10, 20),     # density 2.0
        candidate("b3", "large1", 400, 600),   # density 1.5
        candidate("b4", "large2", 400, 600),   # density 1.5
    ]
    results = compare(candidates, buildings, 1000, max_funded_per_district=2)

    for alloc in results.values():
        assert alloc.buildings_funded <= 2

    assert results["cpsat"].total_benefit_kgco2e == pytest.approx(1200.0)
    assert results["greedy"].total_benefit_kgco2e == pytest.approx(40.0)


def test_equal_split_wastes_budget_by_construction():
    """The baseline's defining weakness, asserted so it cannot be quietly 'fixed'.

    Remainders are not pooled: 1000 EGP across 4 buildings is 250 each, and an
    option costing 400 is unaffordable to every one of them even though the
    portfolio could easily fund two.
    """
    buildings = [make_building(id=f"b{i}", code=f"B{i}") for i in range(1, 5)]
    candidates = [candidate(f"b{i}", f"opt{i}", 400, 600) for i in range(1, 5)]

    alloc = solve(candidates, buildings, 1000, solver="equal_split")
    assert alloc.items == []

    exact = solve(candidates, buildings, 1000, solver="cpsat")
    assert exact.buildings_funded == 2


def test_objectives_can_disagree():
    """Carbon and money do not always rank the same option first.

    Which is the entire reason the platform exposes both rather than silently
    picking one.
    """
    buildings = [make_building(id="b1", code="B1")]
    carbon_heavy = Candidate(
        key="carbon", building_id="b1", district="D1", intervention_ids=("carbon",),
        label="carbon", cost_egp=100, annual_kwh_saving=100,
        lifetime_benefit_kgco2e=5000, annual_egp_saving=100,
    )
    money_heavy = Candidate(
        key="money", building_id="b1", district="D1", intervention_ids=("money",),
        label="money", cost_egp=100, annual_kwh_saving=90,
        lifetime_benefit_kgco2e=1000, annual_egp_saving=900,
    )
    candidates = [carbon_heavy, money_heavy]

    by_carbon = solve(candidates, buildings, 100, objective="lca_carbon")
    by_money = solve(candidates, buildings, 100, objective="egp_saved")

    assert by_carbon.items[0].candidate_key == "carbon"
    assert by_money.items[0].candidate_key == "money"


def test_all_solvers_return_the_same_shape(tiny):
    """What makes the evaluation harness a for-loop and the UI a dropdown."""
    buildings, candidates = tiny
    results = compare(candidates, buildings, 100)
    assert set(results) == {"cpsat", "greedy", "equal_split"}
    for name, alloc in results.items():
        assert alloc.solver == name
        assert alloc.budget_egp == 100
        assert alloc.total_cost_egp <= 100
        assert isinstance(alloc.benefit_per_egp, float)


def test_unknown_solver_and_objective_are_rejected(tiny):
    buildings, candidates = tiny
    with pytest.raises(ValueError, match="unknown solver"):
        solve(candidates, buildings, 100, solver="magic")
    with pytest.raises(ValueError, match="unknown objective"):
        solve(candidates, buildings, 100, objective="vibes")
    with pytest.raises(ValueError, match="negative"):
        solve(candidates, buildings, -1)


def test_density_is_not_the_ilp_objective():
    """F1, asserted directly.

    If the solver maximised the sum of densities it would prefer two cheap
    high-ratio options over one option delivering far more absolute benefit.
    """
    buildings = [make_building(id="b1", code="B1"), make_building(id="b2", code="B2")]
    candidates = [
        candidate("b1", "dense_small", 1, 10),      # density 10.0, benefit 10
        candidate("b2", "big", 100, 500),           # density  5.0, benefit 500
    ]
    alloc = solve(candidates, buildings, 101, solver="cpsat")

    # Both fit, so this alone does not discriminate - but the objective value must
    # be the absolute total, carrying a real unit, not a sum of ratios.
    value_of = objective_fn("lca_carbon")
    assert alloc.total_benefit_kgco2e == pytest.approx(510.0)
    assert value_of(candidates[1]) == 500.0
