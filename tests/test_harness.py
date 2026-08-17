"""The evaluation harness is a deliverable, so it gets the same treatment.

A harness that silently compares a capped run against an uncapped one, or that
counts a known-open finding as a passing gate, produces numbers that are worse than
no numbers - they carry the authority of a measurement without the property.
"""

from __future__ import annotations

import csv

import pytest
from tests.conftest import PARAMS_DICT, make_building, make_intervention

from gemp.domain.catalog import Params
from gemp.domain.models import Candidate
from gemp.evaluate.claims import (
    ClaimResult,
    Verdict,
    claim_bundling_is_multiplicative,
    claim_catalog_rows_are_cited,
    claim_cpsat_never_worse,
    claim_lca_and_raw_kwh_fund_the_same_set,
    run_claims,
)
from gemp.evaluate.sweep import (
    ANY,
    Instance,
    SweepRow,
    gap_pct,
    paired,
    run_sweep,
    select,
    write_sweep_csv,
)


def row(budget=1e6, objective="lca_carbon", cap=None, solver="cpsat", total=100.0,
        keys=(), status="OPTIMAL") -> SweepRow:
    return SweepRow(
        budget_egp=budget, objective=objective, district_cap=cap, solver=solver,
        status=status, solve_ms=1.0, funded=1, spent_egp=budget * 0.9,
        objective_total=total, kwh_saving=total, benefit_kgco2e=total,
        egp_saving=total, funded_keys=frozenset(keys),
    )


# --- selection and pairing --------------------------------------------------


def test_none_selects_the_uncapped_runs_rather_than_everything():
    """The bug this pins: `district_cap=None` reading as "do not filter".

    None is a real value of that field - it IS the unconstrained instance - so a
    filter that treated it as "any" would quietly average the capped and uncapped
    gaps together and report a number belonging to neither.
    """
    rows = [row(cap=None), row(cap=2), row(cap=4)]

    assert len(select(rows, district_cap=None)) == 1
    assert len(select(rows, district_cap=ANY)) == 3
    assert len(select(rows)) == 3


def test_solvers_are_paired_on_the_same_instance():
    rows = [
        row(budget=1e6, cap=None, solver="cpsat", total=110),
        row(budget=1e6, cap=2, solver="cpsat", total=90),
        row(budget=1e6, cap=None, solver="greedy", total=100),
        row(budget=2e6, cap=None, solver="greedy", total=200),
    ]
    pairs = paired(rows, "cpsat", "greedy")

    assert len(pairs) == 1
    exact, baseline = pairs[0]
    assert exact.district_cap is None and baseline.district_cap is None
    assert gap_pct(exact, baseline) == pytest.approx(10.0)


def test_a_baseline_that_funded_nothing_is_infinite_not_a_crash():
    assert gap_pct(row(total=5.0), row(total=0.0)) == float("inf")
    assert gap_pct(row(total=0.0), row(total=0.0)) == 0.0


# --- verdict accounting -----------------------------------------------------


def test_a_known_open_failure_reports_but_does_not_block():
    """A permanently red gate teaches everyone to ignore the gate."""
    open_item = ClaimResult("DATA", "cited", Verdict.FAIL, "0/6", known_open=True)
    regression = ClaimResult("F5", "same set", Verdict.FAIL, "40%")

    assert open_item.verdict is Verdict.FAIL and not open_item.blocking
    assert regression.blocking


def test_a_skipped_claim_never_blocks():
    assert not ClaimResult("F3", "forecast", Verdict.SKIP, "no database").blocking


# --- claims -----------------------------------------------------------------


def test_cpsat_losing_to_a_baseline_is_caught():
    """If this ever fires on real data it is a modelling bug, not a finding."""
    rows = [
        row(solver="cpsat", total=90.0),
        row(solver="greedy_upgrade", total=100.0),
    ]
    result = claim_cpsat_never_worse(_instance(), rows)

    assert result.verdict is Verdict.FAIL
    assert "greedy_upgrade" in result.detail


def test_the_lca_claim_measures_carbon_at_stake_not_set_identity():
    """Two funded sets can differ while the decision does not.

    Near a budget boundary the objectives swap options of near-equal carbon value.
    Gating on set identity would report a finding where nothing is at stake.
    """
    instance = _instance()
    swapped = [
        row(objective="lca_carbon", solver="cpsat", keys=("k1",)),
        row(objective="raw_kwh", solver="cpsat", keys=("k2",)),
    ]
    result = claim_lca_and_raw_kwh_fund_the_same_set(instance, swapped)

    assert result.verdict is Verdict.PASS
    assert "identical funded set at 0/1" in result.measured


def test_uncited_catalog_rows_are_reported_as_known_open():
    result = claim_catalog_rows_are_cited(_instance(uncited=True), [])
    assert result.verdict is Verdict.FAIL
    assert result.known_open


def test_a_bundle_exceeding_the_sum_of_its_parts_is_caught():
    """Naive addition of same-end-use measures overstates by 20-40% (F4)."""
    instance = _instance(over_additive=True)
    result = claim_bundling_is_multiplicative(instance, [])
    assert result.verdict is Verdict.FAIL


# --- end to end -------------------------------------------------------------


def test_the_whole_harness_runs_on_a_hand_built_instance():
    instance = _instance()
    rows = run_sweep(instance, budgets=(1000.0,), objectives=("lca_carbon",), caps=(None,))

    assert {r.solver for r in rows} == {"cpsat", "greedy", "greedy_upgrade", "equal_split"}

    claims = run_claims(instance, rows)
    assert claims and all(isinstance(c, ClaimResult) for c in claims)
    # Ids are quoted in the paper, so duplicates would make a claim unciteable.
    assert len({c.id for c in claims}) == len(claims)


def test_the_sweep_csv_carries_every_row(tmp_path):
    instance = _instance()
    rows = run_sweep(instance, budgets=(500.0, 1000.0), objectives=("lca_carbon",),
                     caps=(None,))
    path = write_sweep_csv(rows, tmp_path / "sweep.csv")

    with path.open(encoding="utf-8") as fh:
        written = list(csv.DictReader(fh))

    assert len(written) == len(rows)
    assert written[0]["district_cap"] == ""          # unconstrained, not the string "None"


# --- fixtures ---------------------------------------------------------------


def _candidate(bid, key, cost, benefit, kwh, ivs=None) -> Candidate:
    return Candidate(
        key=key, building_id=bid, district="D1", intervention_ids=ivs or (key,),
        label=key, cost_egp=cost, annual_kwh_saving=kwh,
        lifetime_benefit_kgco2e=benefit, annual_egp_saving=benefit / 10.0,
    )


def _instance(uncited: bool = False, over_additive: bool = False) -> Instance:
    """A hand-built instance, so the harness tests never depend on data/.

    Same rule as the rest of the suite: those values are Nada's to change, and a
    test that breaks when a cost estimate is revised gets deleted under deadline.
    """
    buildings = [make_building(id="b1", code="B1")]
    catalog = [
        make_intervention(id="hvac_a", end_use="hvac",
                          source_ref="TODO(Nada): cite" if uncited else "ASHRAE 2021"),
        make_intervention(id="hvac_b", end_use="hvac", source_ref="ASHRAE 2021"),
        make_intervention(id="light_a", end_use="lighting", source_ref="ASHRAE 2021"),
    ]
    candidates = [
        _candidate("b1", "k1", 500, 1000, 100, ivs=("hvac_a",)),
        _candidate("b1", "k2", 500, 1000, 100, ivs=("hvac_b",)),
        _candidate("b1", "k3", 400, 700, 80, ivs=("light_a",)),
        # Same end use, so the bundle must save strictly less than 100 + 100.
        _candidate("b1", "k4", 900, 1500, 260 if over_additive else 160,
                   ivs=("hvac_a", "hvac_b")),
    ]
    return Instance(
        buildings=buildings,
        catalog=catalog,
        params=Params.model_validate(PARAMS_DICT),
        candidates=candidates,
    )
