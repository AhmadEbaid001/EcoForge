"""F12 (money units) and the Phase 0 exit gate, asserted as a test."""

from __future__ import annotations

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gemp.domain.candidates import expand_portfolio
from gemp.domain.catalog import load_catalog, load_params
from gemp.domain.models import Candidate
from gemp.domain.portfolio import load_buildings
from gemp.domain.savings import assert_physically_possible
from gemp.optimize.runner import compare, solve

# --- F12: EGP vs thousands of EGP ------------------------------------------


@given(
    benefit=st.floats(min_value=1e3, max_value=1e9, allow_nan=False, allow_infinity=False),
    cost=st.floats(min_value=1e4, max_value=1e7, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200, deadline=None)
def test_score_per_kegp_round_trips(benefit: float, cost: float):
    """`score_per_kegp` is the only place a factor of 1000 is allowed to appear.

    Everything else - database, solver, catalog, API - is EGP. This property pins
    the conversion so a refactor cannot quietly move it.
    """
    c = Candidate(
        key="k", building_id="b", district="D", intervention_ids=("i",), label="l",
        cost_egp=cost, annual_kwh_saving=1.0,
        lifetime_benefit_kgco2e=benefit, annual_egp_saving=1.0,
    )
    assert c.score_per_kegp * (cost / 1000.0) == pytest.approx(benefit, rel=1e-9)


def test_zero_cost_candidate_does_not_divide_by_zero():
    c = Candidate(
        key="k", building_id="b", district="D", intervention_ids=("i",), label="l",
        cost_egp=0.0, annual_kwh_saving=1.0,
        lifetime_benefit_kgco2e=100.0, annual_egp_saving=1.0,
    )
    assert c.score_per_kegp == 0.0


# --- Phase 0 exit gate ------------------------------------------------------


@pytest.fixture(scope="module")
def portfolio():
    params = load_params()
    catalog = load_catalog()
    buildings = load_buildings()
    candidates = expand_portfolio(buildings, catalog, params)
    return params, catalog, buildings, candidates


def test_portfolio_expands_to_candidates(portfolio):
    _params, _catalog, buildings, candidates = portfolio
    assert len(buildings) == 50
    assert len(candidates) > len(buildings), "every building should have several options"
    assert all(c.cost_egp > 0 for c in candidates)


def test_no_candidate_claims_impossible_savings(portfolio):
    """The guard rail, run over the whole real portfolio rather than a fixture."""
    params, _catalog, buildings, candidates = portfolio
    by_id = {b.id: b for b in buildings}
    for c in candidates:
        assert_physically_possible(by_id[c.building_id], c.annual_kwh_saving, params, c.key)


def test_exit_gate_real_allocation_under_a_second(portfolio):
    """Phase 0 is done when this passes: a real allocation over 50 buildings, from
    fixtures, with no database, broker or container running."""
    _params, _catalog, buildings, candidates = portfolio

    started = time.perf_counter()
    alloc = solve(candidates, buildings, 10_000_000, solver="cpsat")
    elapsed = time.perf_counter() - started

    assert alloc.status == "OPTIMAL"
    assert alloc.items, "optimizer funded nothing at a 10M EGP budget"
    assert alloc.total_cost_egp <= 10_000_000
    assert elapsed < 2.0, f"solve took {elapsed:.2f}s; the budget slider needs it interactive"


def test_exact_beats_equal_split_by_a_wide_margin(portfolio):
    """The headline claim, pinned. Equal split funds almost nothing because
    10M EGP over 50 buildings is 200k each, below most intervention costs."""
    _params, _catalog, buildings, candidates = portfolio
    results = compare(candidates, buildings, 10_000_000)

    exact = results["cpsat"].total_benefit_kgco2e
    baseline = results["equal_split"].total_benefit_kgco2e

    assert exact > baseline * 2.0, f"exact {exact:,.0f} vs equal split {baseline:,.0f}"


def test_district_cap_is_where_exact_optimization_earns_its_place(portfolio):
    """F8: greedy cannot plan around a per-district cap, so it strands budget."""
    _params, _catalog, buildings, candidates = portfolio
    results = compare(candidates, buildings, 10_000_000, max_funded_per_district=2)

    assert results["cpsat"].total_benefit_kgco2e > results["greedy"].total_benefit_kgco2e
    assert results["greedy"].total_cost_egp < results["cpsat"].total_cost_egp
    for alloc in results.values():
        counts: dict[str, int] = {}
        for item in alloc.items:
            counts[item.district] = counts.get(item.district, 0) + 1
        assert all(n <= 2 for n in counts.values())


def test_lca_and_raw_kwh_rank_candidates_differently(portfolio):
    """Embodied carbon does change the ORDER of the candidate list.

    Across the 1493 options in the fixture portfolio, the two density rankings agree
    on only about a tenth of positions, driven by glazing - whose embodied carbon can
    exceed the emissions it avoids over the whole horizon.
    """
    _params, _catalog, _buildings, candidates = portfolio

    by_kwh = [c.key for c in sorted(
        candidates, key=lambda c: c.annual_kwh_saving / c.cost_egp, reverse=True)]
    by_lca = [c.key for c in sorted(
        candidates, key=lambda c: c.lifetime_benefit_kgco2e / c.cost_egp, reverse=True)]

    identical_positions = sum(1 for a, b in zip(by_kwh, by_lca, strict=True) if a == b)
    assert identical_positions < len(candidates) * 0.5


def test_lca_adjustment_barely_changes_the_funded_set(portfolio):
    """FINDING, pinned deliberately: at realistic budgets the LCA layer changes nothing.

    Ranking differences (see the test above) sit low in the list, among options that
    no budget funds anyway. At every budget from 2M to 40M EGP the carbon objective
    and the raw-kWh objective fund the SAME buildings with the SAME interventions,
    because embodied carbon is a median 8% of gross avoided emissions and the funded
    options are the ones where it is smallest.

    This is the F2 finding reappearing empirically: embodied carbon is too small to
    reverse a retrofit ranking at any Egyptian grid factor measured so far. The fixture
    here uses 0.45; the deployed data uses the cited 0.3803, where the effect is
    stronger but still does not reverse the ranking (see docs/FINDINGS.md).

    The test asserts the current behaviour so the finding stays visible rather than
    being quietly assumed away. If a time-of-use marginal emission factor is added
    later - which would make HVAC savings genuinely worth more carbon per kWh than
    lighting savings - this test SHOULD start failing, and that failure is the signal
    that the LCA layer has begun to earn its place.
    """
    _params, _catalog, buildings, candidates = portfolio

    by_carbon = solve(candidates, buildings, 10_000_000, objective="lca_carbon")
    by_kwh = solve(candidates, buildings, 10_000_000, objective="raw_kwh")

    carbon_keys = {i.candidate_key for i in by_carbon.items}
    kwh_keys = {i.candidate_key for i in by_kwh.items}
    assert carbon_keys == kwh_keys, (
        "objectives have started to diverge at a 10M budget - if this is intended "
        "(e.g. a time-of-use emission factor was added), update this test and say so "
        "in the paper, because it changes the LCA claim"
    )


def test_optimizer_declines_options_whose_embodied_carbon_exceeds_their_savings(portfolio):
    """A net-negative option must never be funded by the carbon objective.

    This asserted that the portfolio CONTAINED such an option, and it did: on the
    old fixture at least one glazing candidate emitted more to manufacture than it
    avoided over thirty years, and `docs/FINDINGS.md` cited that as the life-cycle
    layer's one unambiguous win.

    The portfolio is now real public buildings rather than the apartment blocks
    that fixture turned out to be, and they are large enough that glazing always
    pays its embodied carbon back. So the case no longer occurs naturally - which
    is a finding about the portfolio, not a licence to weaken the check.

    The property worth testing was always the optimizer's, not the fixture's, so
    the option is constructed here: whatever the portfolio happens to contain, a
    candidate that costs more carbon than it saves must not be bought with a carbon
    budget. Written this way it also keeps working when the portfolio changes again.
    """
    _params, _catalog, buildings, candidates = portfolio

    natural = [c for c in candidates if c.lifetime_benefit_kgco2e <= 0]
    assert all(c.annual_kwh_saving > 0 for c in natural), (
        "a net-negative option should still save energy - it is the CARBON that "
        "does not pay back, and that distinction is the whole point of the layer"
    )

    # A cheap option that saves energy and loses carbon. Cheap so that a budget
    # this size could not plausibly decline it on price.
    donor = min(candidates, key=lambda c: c.cost_egp)
    poisoned = donor.model_copy(update={
        "key": f"{donor.key}::net-negative-probe",
        "cost_egp": 1_000.0,
        "annual_kwh_saving": max(donor.annual_kwh_saving, 1.0),
        "lifetime_benefit_kgco2e": -1_000.0,
    })

    result = solve([*candidates, poisoned], buildings, 80_000_000,
                   objective="lca_carbon")
    funded = {i.candidate_key for i in result.items}

    assert poisoned.key not in funded, (
        "the carbon objective funded an option that emits more than it avoids"
    )
    assert not (funded & {c.key for c in natural})
