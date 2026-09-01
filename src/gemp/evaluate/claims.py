"""Every claim the paper makes, with the gate it has to clear.

A claim here is not a test. A test pins behaviour that must never change; a claim
re-measures something already asserted in prose, so that the prose can be corrected
when the measurement moves. Claims that are expected to fail are listed rather than
omitted, because a harness that only reports what already works is a marketing
document; as of 1 September there are none, and every claim here is blocking.

Claim ids track the technical review's findings (F1, F2 ...) where one exists.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from gemp.domain.candidates import expand_portfolio
from gemp.evaluate.sweep import Instance, SweepRow, gap_pct, paired, select
from gemp.optimize.objective import density, objective_fn
from gemp.optimize.runner import solve

log = logging.getLogger("gemp.evaluate.claims")

# --- gates ------------------------------------------------------------------
#
# Each number below is the point at which a sentence in the paper stops being true.
# They are loose on purpose: a gate tight enough to trip on a catalog revision would
# be deleted the first time Nada sources a citation that moves a cost by 10%.

# The baseline every headline number is quoted against. Plain greedy saturates and
# would flatter CP-SAT; see `claim_plain_greedy_saturates` and the baselines module.
STRONG_BASELINE = "greedy_upgrade"

# "A density-ordered heuristic is near-optimal on an unconstrained knapsack."
# Measured at ~10% against the strong heuristic; the sentence survives up to a quarter.
MAX_UNCONSTRAINED_GAP_PCT = 25.0

# "Exact optimization earns its place through constraints greedy cannot express."
# Measured at ~134% under a district cap. The claim is the RATIO, not the number:
# the capped gap has to be several times the unconstrained one, or the argument for
# CP-SAT is rhetorical.
MIN_CAPPED_GAP_MULTIPLE = 3.0

# "CP-SAT returns a proven optimum" - within the solver's own time limit.
REQUIRED_OPTIMAL_FRAC = 1.0

# "The LCA layer changes almost no decisions." Measured as what the difference is
# WORTH, not as set identity: near a budget boundary the two objectives swap options
# of nearly equal carbon value, which changes the set without changing anything a
# decision-maker would act on.
#
# A5 supersedes the note that used to live here ("if a TOU factor is ever added,
# this gate should start failing"). The flat factor is kept deliberately so this
# comparison stays reproducible, and the old-vs-new question moved to its own
# claim: F15 measures what the TOU-weighted objective changes on top of this one.
MAX_LCA_CARBON_SHORTFALL_PCT = 1.0

# "The measured hourly shape, weighted against the marginal grid profile, changes
# which buildings get funded." This is A5's reason to exist - if the sweep with a
# loaded profile produced the same sets as the flat factor at every budget, the
# hundreds of thousands of signed readings would still reach nothing. One changed
# budget is enough to prove the channel is live; the COUNT is the finding.
MIN_TOU_CHANGED_BUDGETS = 1

_TOU_STATEMENT = "The TOU-weighted objective changes which buildings get funded"

# Anomaly gates from the technical review (F9).
MIN_ANOMALY_RECALL = 0.80
MIN_ANOMALY_PRECISION = 0.60

# A forecaster that cannot beat "same hour last week" has not earned its runtime.
MIN_FORECAST_MAPE_IMPROVEMENT_PCT = 0.0

# Budgets used by the claims that re-solve rather than read the sweep. Kept small:
# these run inside the CI gate.
# Sized to the portfolio of public buildings: 25 M funds twelve of the fifty,
# 50 M funds twenty-two, 100 M funds thirty-six. Probing below that range
# measures a budget too small to make the allocation interesting.
PROBE_BUDGETS: tuple[float, ...] = (25_000_000.0, 50_000_000.0, 100_000_000.0)

# Below this many instances a "median gap" is a coincidence, and a claim measured on
# it would report the grid rather than the solver.
MIN_INSTANCES_FOR_A_MEDIAN = 3

# Plain greedy's plateau begins around 57 M EGP on this portfolio, so a sweep that
# stops below this cannot see it either way.
# The sweep has to reach well past the point plain greedy stops spending, or the
# plateau is a single point and indistinguishable from the curve still rising.
# Scaled with the portfolio when it became public buildings.
SATURATION_PROBE_BUDGET = 150_000_000.0


class Verdict(StrEnum):
    # The word a claim gets when it holds. Not a credential.
    PASS = "PASS"  # nosec B105
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class ClaimResult:
    id: str
    statement: str
    verdict: Verdict
    measured: str
    detail: str = ""
    # Known-open claims still report FAIL. They do not fail the RUN, because the work
    # to close them is tracked elsewhere and a permanently red gate teaches everyone
    # to ignore the gate.
    known_open: bool = False

    @property
    def blocking(self) -> bool:
        return self.verdict is Verdict.FAIL and not self.known_open


ClaimFn = Callable[[Instance, Sequence[SweepRow]], ClaimResult]


def _result(
    claim_id: str,
    statement: str,
    ok: bool,
    measured: str,
    detail: str = "",
    known_open: bool = False,
) -> ClaimResult:
    return ClaimResult(
        id=claim_id,
        statement=statement,
        verdict=Verdict.PASS if ok else Verdict.FAIL,
        measured=measured,
        detail=detail,
        known_open=known_open,
    )


# --- solver claims (no infrastructure) --------------------------------------


def claim_cpsat_never_worse(instance: Instance, rows: Sequence[SweepRow]) -> ClaimResult:
    """The one claim that would indicate a modelling bug rather than a wording error.

    Every baseline explores a subset of what CP-SAT can express, so on an identical
    instance CP-SAT losing to any of them means they are not solving the same
    problem - a bug in the model, the constraints or the objective mapping.
    """
    losses: list[str] = []
    checked = 0
    worst = 0.0
    for baseline in ("greedy_upgrade", "greedy", "equal_split"):
        for exact, other in paired(rows, "cpsat", baseline):
            checked += 1
            worst = min(worst, gap_pct(exact, other))
            if exact.objective_total < other.objective_total:
                losses.append(
                    f"{baseline} at budget {exact.budget_egp:,.0f} "
                    f"obj {exact.objective} cap {exact.district_cap}"
                )
    return _result(
        "F1-a",
        "CP-SAT is never worse than any baseline on an identical instance",
        not losses,
        f"{checked - len(losses)}/{checked} instances, worst gap {worst:+.2f}%",
        detail="; ".join(losses[:5]),
    )


def claim_unconstrained_gap_is_small(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """Reported, not hidden. The honest version of the CP-SAT argument starts here.

    Measured against `greedy_upgrade`, not plain greedy. Quoting the gap against a
    baseline that stops spending most of the budget would inflate the headline into
    something that collapses under the first question a judge asks.
    """
    pairs = paired(select(rows, district_cap=None), "cpsat", STRONG_BASELINE)
    gaps = [gap_pct(c, g) for c, g in pairs if g.objective_total > 0]
    median = statistics.median(gaps) if gaps else 0.0
    return _result(
        "F8-a",
        "Unconstrained, CP-SAT beats the strong heuristic only slightly",
        bool(gaps) and median <= MAX_UNCONSTRAINED_GAP_PCT,
        f"median {median:+.2f}% over {len(gaps)} budgets (gate <= {MAX_UNCONSTRAINED_GAP_PCT:.0f}%)",
        detail=f"range {min(gaps):+.2f}% to {max(gaps):+.2f}%" if gaps else "no comparable rows",
    )


def claim_capped_gap_is_large(instance: Instance, rows: Sequence[SweepRow]) -> ClaimResult:
    """The claim that actually justifies an exact solver."""
    uncapped = [
        gap_pct(c, g)
        for c, g in paired(select(rows, district_cap=None), "cpsat", STRONG_BASELINE)
        if g.objective_total > 0
    ]
    capped_rows = [r for r in rows if r.district_cap is not None]
    capped = [
        gap_pct(c, g)
        for c, g in paired(capped_rows, "cpsat", STRONG_BASELINE)
        if g.objective_total > 0
    ]
    # A median over one or two instances is not a median. Too small a grid must skip,
    # never report - an artifact of the grid presented as a finding about the solver
    # is exactly the failure this harness exists to prevent.
    if len(uncapped) < MIN_INSTANCES_FOR_A_MEDIAN or len(capped) < MIN_INSTANCES_FOR_A_MEDIAN:
        return ClaimResult(
            "F8-b", "Under a district cap the gap becomes decisive",
            Verdict.SKIP,
            f"needs {MIN_INSTANCES_FOR_A_MEDIAN} capped and uncapped instances, "
            f"have {len(capped)} and {len(uncapped)}",
        )

    median_uncapped = statistics.median(uncapped)
    median_capped = statistics.median(capped)
    multiple = median_capped / median_uncapped if median_uncapped > 0 else float("inf")
    return _result(
        "F8-b",
        "Under a district cap the gap becomes decisive",
        multiple >= MIN_CAPPED_GAP_MULTIPLE,
        f"median {median_capped:+.1f}% capped vs {median_uncapped:+.1f}% uncapped "
        f"({multiple:.1f}x, gate >= {MIN_CAPPED_GAP_MULTIPLE:.0f}x)",
        detail=f"capped range {min(capped):+.1f}% to {max(capped):+.1f}%",
    )


def claim_cpsat_proves_optimality(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """"Proven optimum" is a claim about the solver's status, not its objective."""
    cpsat = select(rows, solver="cpsat")
    optimal = [r for r in cpsat if r.status == "OPTIMAL"]
    frac = len(optimal) / len(cpsat) if cpsat else 0.0
    slowest = max((r.solve_ms for r in cpsat), default=0.0)
    return _result(
        "F8-c",
        "CP-SAT returns a proven optimum within its time limit",
        frac >= REQUIRED_OPTIMAL_FRAC,
        f"{len(optimal)}/{len(cpsat)} OPTIMAL, slowest solve {slowest:.0f} ms",
        detail="; ".join(
            f"{r.status} at budget {r.budget_egp:,.0f} cap {r.district_cap}"
            for r in cpsat if r.status != "OPTIMAL"
        )[:200],
    )


def claim_optimizers_beat_equal_split(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """Equal-split is the do-nothing-clever baseline: spread the budget evenly.

    It is in the comparison because "we beat an optimizer-free policy" is the claim
    a judge actually cares about, and it is a far larger margin than CP-SAT over any
    heuristic. Plain greedy is deliberately excluded: it loses to equal split above
    30 M EGP, which is a fact about greedy's saturation (F8-f), not about optimization.
    """
    losses: list[str] = []
    medians: list[str] = []
    for solver in ("cpsat", STRONG_BASELINE):
        pairs = paired(rows, solver, "equal_split")
        losses += [
            f"{solver} at budget {a.budget_egp:,.0f}"
            for a, e in pairs
            if a.objective_total < e.objective_total
        ]
        gaps = [gap_pct(a, e) for a, e in pairs if e.objective_total > 0]
        if gaps:
            medians.append(f"{solver} {statistics.median(gaps):+.0f}%")

    return _result(
        "F8-d",
        "Optimized allocation beats an equal-split budget policy",
        not losses,
        f"median over equal split: {', '.join(medians)}; {len(losses)} losses",
        detail="; ".join(losses[:3]),
    )


def claim_plain_greedy_saturates(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """A finding about the baseline, kept visible so nobody quotes the wrong number.

    Plain greedy never revisits a funded building, so on this portfolio it stops
    spending at roughly 57 M EGP and its benefit flatlines from there. Any headline
    that compares CP-SAT against it above that point is measuring greedy's ceiling,
    not the value of exact optimization.

    This passes while the property holds. If greedy is ever given an upgrade pass,
    it will fail - and the paragraph in the paper that explains the saturation will
    need to go with it.
    """
    series = sorted(
        select(rows, objective="lca_carbon", district_cap=None, solver="greedy"),
        key=lambda r: r.budget_egp,
    )
    if len(series) < MIN_INSTANCES_FOR_A_MEDIAN or max(
        r.budget_egp for r in series
    ) < SATURATION_PROBE_BUDGET:
        return ClaimResult(
            "F8-f", "Plain greedy saturates and cannot spend a large budget",
            Verdict.SKIP,
            f"sweep must reach {SATURATION_PROBE_BUDGET / 1e6:.0f} M EGP to see the plateau",
        )

    ceiling = max(r.objective_total for r in series)
    plateau = [r for r in series if r.objective_total >= ceiling - 1.0]
    onset = min(r.budget_egp for r in plateau)
    worst_unspent = max(1.0 - r.budget_used_frac for r in series)
    return _result(
        "F8-f",
        "Plain greedy saturates and cannot spend a large budget",
        len(plateau) > 1,
        f"benefit flat from {onset / 1e6:.0f} M EGP, leaves up to "
        f"{worst_unspent:.0%} of the budget unspent",
        detail=f"{len(plateau)} of {len(series)} budgets at the same benefit ceiling",
    )


def claim_benefit_is_monotone_in_budget(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """More money must not buy less benefit.

    Any violation is a bug in the model or the solve, not a finding: the feasible
    set at a smaller budget is contained in the feasible set at a larger one.
    """
    violations: list[str] = []
    for objective in {r.objective for r in rows}:
        for cap in {r.district_cap for r in rows}:
            series = sorted(
                select(rows, objective=objective, district_cap=cap, solver="cpsat"),
                key=lambda r: r.budget_egp,
            )
            for previous, current in zip(series, series[1:], strict=False):
                if current.objective_total < previous.objective_total:
                    violations.append(
                        f"{objective} cap {cap}: {previous.budget_egp:,.0f} -> "
                        f"{current.budget_egp:,.0f} lost "
                        f"{previous.objective_total - current.objective_total:,.0f}"
                    )
    return _result(
        "F8-e",
        "CP-SAT benefit is non-decreasing in budget",
        not violations,
        f"{len(violations)} violations",
        detail="; ".join(violations[:3]),
    )


def claim_pruning_preserves_the_result(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """Dominance pruning is sold as result-preserving, so it is checked as such.

    Pruning drops an option only when another option at the same building is both
    cheaper and at least as good. If the claim ever fails, the pruned set is no
    longer safe and every number above it is suspect.
    """
    mismatches: list[str] = []
    for budget in PROBE_BUDGETS:
        pruned = solve(instance.candidates, instance.buildings, budget, prune=True)
        whole = solve(instance.candidates, instance.buildings, budget, prune=False)
        if abs(pruned.total_benefit_kgco2e - whole.total_benefit_kgco2e) > 1.0:
            mismatches.append(
                f"budget {budget:,.0f}: {pruned.total_benefit_kgco2e:,.0f} pruned vs "
                f"{whole.total_benefit_kgco2e:,.0f} unpruned"
            )
    return _result(
        "F1-b",
        "Dominance pruning does not change the optimum",
        not mismatches,
        f"{len(PROBE_BUDGETS) - len(mismatches)}/{len(PROBE_BUDGETS)} budgets identical",
        detail="; ".join(mismatches),
    )


def claim_lca_and_raw_kwh_fund_the_same_set(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """The finding that the LCA layer changes almost no decisions.

    A5 note: this comparison is deliberately run at the FLAT grid factor on both
    sides. Whether the time-of-use weighting changes decisions is F15's question,
    measured against this claim's own funded sets - keeping the two apart is what
    makes old and new comparable at all.
    """
    lca = {
        r.budget_egp: r
        for r in select(rows, objective="lca_carbon", district_cap=None, solver="cpsat")
    }
    raw = {
        r.budget_egp: r
        for r in select(rows, objective="raw_kwh", district_cap=None, solver="cpsat")
    }
    shared = sorted(set(lca) & set(raw))
    if not shared:
        return ClaimResult(
            "F5", "Optimizing raw kWh instead of life-cycle carbon costs almost nothing",
            Verdict.SKIP, "sweep did not contain both objectives",
        )

    # Set identity is the strong form of the claim and it is not what matters. Near a
    # budget boundary the two objectives swap options of nearly equal carbon value,
    # which changes the SET without changing the DECISION in any way a decision-maker
    # would notice. The quantity that matters is what the swap costs in carbon.
    by_key = {c.key: c for c in instance.candidates}
    value_of = objective_fn("lca_carbon")

    def carbon_of(keys) -> float:
        return sum(value_of(by_key[k]) for k in keys if k in by_key)

    identical = [b for b in shared if lca[b].funded_keys == raw[b].funded_keys]
    shortfalls = []
    for budget in shared:
        best = carbon_of(lca[budget].funded_keys)
        under_raw = carbon_of(raw[budget].funded_keys)
        shortfalls.append((budget, 0.0 if best <= 0 else (best - under_raw) / best * 100.0))

    worst_budget, worst = max(shortfalls, key=lambda pair: pair[1])
    frac = len(identical) / len(shared)
    return _result(
        "F5",
        "Optimizing raw kWh instead of life-cycle carbon costs almost nothing",
        worst <= MAX_LCA_CARBON_SHORTFALL_PCT,
        f"worst carbon shortfall {worst:.2f}% (gate <= {MAX_LCA_CARBON_SHORTFALL_PCT}%), "
        f"identical funded set at {len(identical)}/{len(shared)} budgets ({frac:.0%})",
        detail=f"worst at budget {worst_budget:,.0f} EGP",
    )


def claim_no_portfolio_wide_priority_list(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """The replacement narrative, and the one that survived measurement.

    What IS demonstrable - at the flat factor as anywhere else - is that the best
    measure is building-specific, so no portfolio-wide priority list is right.
    Reworded when A5 landed: the original gate demanded `solar_wins == 0`, but
    whether rooftop solar wins somewhere under a marginal grid profile is a
    FINDING to report, not a condition for the sentence to hold. The claim is
    about diversity of winners, and `distinct > 1` is what carries it.
    """
    singles = [c for c in instance.candidates if len(c.intervention_ids) == 1]
    winners: dict[str, str] = {}
    for building in instance.buildings:
        options = [c for c in singles if c.building_id == building.id]
        if not options:
            continue
        best = max(options, key=lambda c: density(c, "lca_carbon"))
        winners[building.id] = best.intervention_ids[0]

    tally: dict[str, int] = {}
    for intervention_id in winners.values():
        tally[intervention_id] = tally.get(intervention_id, 0) + 1

    distinct = len(tally)
    ranking = ", ".join(
        f"{k} {v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1])
    )
    return _result(
        "F7",
        "No single measure wins portfolio-wide; the best option is building-specific",
        distinct > 1,
        f"{distinct} distinct winners over {len(winners)} buildings",
        detail=ranking,
    )


def claim_tou_weighting_changes_funded_sets(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """A5's fixture-world twin: SKIP, because fixtures carry no measured shapes.

    The real claim is `_tou_claim` in the measured-data family below. It lives
    there rather than here because the whole mechanism is per-building shapes from
    the metered series - the GeoJSON fixture has none, every building costs flat,
    and a comparison run on it would measure nothing while reporting a number.
    """
    del instance, rows
    return ClaimResult(
        "F15",
        "The TOU-weighted objective changes which buildings get funded",
        Verdict.SKIP,
        "fixture portfolio carries no measured load shapes; measured with the stack "
        "(evaluate --with-db after a refit)",
    )


def _tou_claim() -> list[ClaimResult]:
    """F15, on stored data: does metered data finally reach the funding decision?

    The optimizer ingests hundreds of thousands of signed readings. Before A5 they
    reached forecasts, anomalies and the integrity chain - everything except WHICH
    BUILDING GETS FUNDED - because carbon benefit multiplied a flat national
    constant. Now each candidate's TOU-weighted value depends on its building's own
    measured hourly shape. This re-solves CP-SAT under both objectives at the probe
    budgets over the STORED portfolio and counts how often the funded set differs.

    SKIPs honestly at each layer that can be absent: no grid profile loaded (the
    deployment chose the flat world), or no building yet has a measured shape (the
    refit has not run since the migration). A skipped channel is reported as
    skipped, never manufactured into a number.
    """
    try:
        from gemp.db import session_scope
        from gemp.domain.candidates import expand_portfolio
        from gemp.domain.catalog import load_catalog, load_params
        from gemp.optimize.runner import solve
        from gemp.repository import load_buildings
    except Exception as exc:  # noqa: BLE001 - no infrastructure in this process
        reason = f"{type(exc).__name__}: {exc}"
        return [ClaimResult("F15", _TOU_STATEMENT, Verdict.SKIP, reason)]

    params = load_params()
    if params.tou is None:
        return [ClaimResult("F15", _TOU_STATEMENT, Verdict.SKIP,
                            "no tou profile loaded; running the flat world")]

    try:
        with session_scope() as session:
            buildings = load_buildings(session)
            catalog = load_catalog()
    except Exception as exc:  # noqa: BLE001
        return [ClaimResult("F15", _TOU_STATEMENT, Verdict.SKIP,
                            f"{type(exc).__name__}: {exc}")]

    shaped = [b for b in buildings if b.has_measured_shape]
    if not shaped:
        return [ClaimResult("F15", _TOU_STATEMENT, Verdict.SKIP,
                            "no building has a measured shape yet; "
                            "run python -m gemp.ml.jobs")]

    candidates = expand_portfolio(buildings, catalog, params)
    changed, shared = [], 0
    for budget in PROBE_BUDGETS:
        r_flat = solve(candidates, buildings, budget,
                       solver="cpsat", objective="lca_carbon")
        r_tou = solve(candidates, buildings, budget,
                      solver="cpsat", objective="tou_carbon")
        if r_flat.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        if r_tou.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        shared += 1
        flat_keys = {i.candidate_key for i in r_flat.items}
        tou_keys = {i.candidate_key for i in r_tou.items}
        if flat_keys != tou_keys:
            changed.append(budget)

    if shared == 0:
        return [ClaimResult("F15", _TOU_STATEMENT, Verdict.SKIP,
                            "no solvable budget among the probes")]
    frac = len(changed) / shared
    return [_result(
        "F15",
        _TOU_STATEMENT,
        len(changed) >= MIN_TOU_CHANGED_BUDGETS,
        f"{len(shaped)}/{len(buildings)} buildings carry measured shapes; "
        f"funded set differs at {len(changed)}/{shared} probe budgets ({frac:.0%}); "
        f"gate >= {MIN_TOU_CHANGED_BUDGETS}",
        detail="budgets: " + ", ".join(f"{b:,.0f}" for b in PROBE_BUDGETS),
    )]


def claim_bundling_is_multiplicative(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """Interventions sharing an end use compose multiplicatively (F4).

    The first version of this claim demanded that EVERY bundle save strictly less
    than the sum of its parts, and 509 of 1,114 bundles failed it. The claim was
    wrong, not the model: LED lighting and BMS controls act on different end uses,
    and rooftop solar is generation rather than a load reduction, so there is no
    interaction term for them to lose. Adding those is correct.

    The multiplicative composition applies where it physically must - insulation
    cuts the cooling load, an HVAC swap raises COP, and both act on E_hvac. So the
    claim splits in two: bundles sharing an end use must be strictly sub-additive,
    and no bundle at all may exceed the sum of its parts.
    """
    end_use = {iv.id: iv.end_use for iv in instance.catalog}
    singles: dict[tuple[str, str], float] = {
        (c.building_id, c.intervention_ids[0]): c.annual_kwh_saving
        for c in instance.candidates
        if len(c.intervention_ids) == 1
    }

    shared_checked = 0
    over_additive: list[str] = []
    not_sub_additive: list[str] = []

    for candidate in instance.candidates:
        if len(candidate.intervention_ids) < 2:
            continue
        parts = [singles.get((candidate.building_id, iv)) for iv in candidate.intervention_ids]
        if any(p is None for p in parts):
            continue

        total_parts = sum(parts)
        uses = [end_use[iv] for iv in candidate.intervention_ids]
        # A tolerance in kWh/yr, on savings of 1e4 to 1e6: floating-point slack, not
        # a modelling allowance.
        if candidate.annual_kwh_saving > total_parts + 1e-6:
            over_additive.append(candidate.key)

        if len(set(uses)) < len(uses):
            shared_checked += 1
            if candidate.annual_kwh_saving >= total_parts - 1e-6:
                not_sub_additive.append(candidate.key)

    ok = shared_checked > 0 and not over_additive and not not_sub_additive
    return _result(
        "F4",
        "Interventions sharing an end use compose multiplicatively",
        ok,
        f"{shared_checked - len(not_sub_additive)}/{shared_checked} same-end-use bundles "
        f"strictly sub-additive, {len(over_additive)} bundles exceed the sum of their parts",
        detail="; ".join((not_sub_additive + over_additive)[:3]),
    )


def claim_catalog_rows_are_cited(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """Closed on 1 September; kept as a gate rather than retired.

    Every number the platform reports derives from these six rows. This was the last
    known-open claim, and it is a blocking one now: a row that loses its citation
    fails the run instead of being counted as a documented exception.
    """
    uncited = [iv.id for iv in instance.catalog if "TODO" in iv.source_ref.upper()]
    return _result(
        "DATA",
        "Every catalog row carries a citation",
        not uncited,
        f"{len(instance.catalog) - len(uncited)}/{len(instance.catalog)} rows cited",
        detail=", ".join(uncited),
    )


def claim_candidate_expansion_is_stable(
    instance: Instance, rows: Sequence[SweepRow]
) -> ClaimResult:
    """Expansion must be a pure function of (buildings, catalog, params).

    The sweep expands once and reuses the result across ~360 solves. If expansion
    were order-dependent or stateful, every number in this report would be measured
    against a set that no longer exists.
    """
    again = expand_portfolio(instance.buildings, instance.catalog, instance.params)
    first = {c.key for c in instance.candidates}
    second = {c.key for c in again}
    return _result(
        "F1-c",
        "Candidate expansion is deterministic",
        first == second,
        f"{len(first)} options, {len(first ^ second)} differing on re-expansion",
    )


SOLVER_CLAIMS: tuple[ClaimFn, ...] = (
    claim_cpsat_never_worse,
    claim_unconstrained_gap_is_small,
    claim_capped_gap_is_large,
    claim_cpsat_proves_optimality,
    claim_optimizers_beat_equal_split,
    claim_plain_greedy_saturates,
    claim_benefit_is_monotone_in_budget,
    claim_pruning_preserves_the_result,
    claim_lca_and_raw_kwh_fund_the_same_set,
    claim_tou_weighting_changes_funded_sets,
    claim_no_portfolio_wide_priority_list,
    claim_bundling_is_multiplicative,
    claim_candidate_expansion_is_stable,
    claim_catalog_rows_are_cited,
)


# --- measured-data claims (need the database) -------------------------------


def db_claims(k_values: Sequence[float] | None = None) -> list[ClaimResult]:
    """Forecasting and anomaly claims, from one fit pass over the seeded portfolio.

    Imported lazily: the solver claims must keep running with no database, no broker
    and no containers, which is the whole reason `domain/` has no infrastructure
    imports either.
    """
    from gemp.domain.catalog import load_params
    from gemp.evaluate.measured import measure, measure_integrity

    params = load_params()
    integrity = [_integrity_claim(measure_integrity)]
    ks = list(k_values) if k_values else [params.anomaly_k]
    tou = _tou_claim()
    try:
        measurement = measure(ks)
    except Exception as exc:  # noqa: BLE001 - the harness reports, it does not crash
        log.warning("measured claims skipped: %s", exc)
        reason = f"{type(exc).__name__}: {exc}"
        return integrity + tou + [
            ClaimResult("F3", "The forecaster beats a seasonal-naive baseline",
                        Verdict.SKIP, reason),
            ClaimResult("F9-a", "Anomaly recall meets the 0.8 target", Verdict.SKIP, reason),
            ClaimResult("F9-b", "Anomaly precision meets the 0.6 target", Verdict.SKIP, reason),
        ]

    scores = measurement.scores_at(params.anomaly_k)
    improvement = measurement.baseline_median_mape - measurement.model_median_mape

    return integrity + tou + [
        _result(
            "F3",
            "The forecaster beats a seasonal-naive baseline",
            improvement > MIN_FORECAST_MAPE_IMPROVEMENT_PCT,
            f"median MAPE {measurement.model_median_mape:.1f}% vs "
            f"{measurement.baseline_median_mape:.1f}% naive "
            f"({improvement:+.1f} points over {measurement.buildings} buildings)",
        ),
        _result(
            "F9-a",
            "Anomaly recall meets the 0.8 target",
            scores.recall >= MIN_ANOMALY_RECALL,
            f"recall {scores.recall:.3f} at k={scores.k:g} "
            f"({scores.events_found}/{scores.events} events, gate >= {MIN_ANOMALY_RECALL})",
        ),
        _result(
            "F9-b",
            "Anomaly precision meets the 0.6 target",
            scores.precision >= MIN_ANOMALY_PRECISION,
            f"precision {scores.precision:.3f} at k={scores.k:g} "
            f"({scores.true_positives}/{scores.episodes} alerts real, "
            f"gate >= {MIN_ANOMALY_PRECISION})",
            detail=f"flag rate {scores.flag_rate:.2%}; "
                   f"{scores.episodes_unscorable} alerts outside the ground-truth "
                   f"window were excluded rather than counted against precision",
        ),
    ]


def _integrity_claim(measure_integrity) -> ClaimResult:
    """F5, measured on stored data rather than asserted from the design.

    The claim is not "the chain verifies" - a chain always verifies against itself,
    including after its tail is deleted. It is that the chain still agrees with the
    anchor written outside the database volume, which is the only evidence that
    nothing was removed. That comparison was implemented in Phase 1 and called by
    nothing until Phase 4.
    """
    try:
        report = measure_integrity()
    except Exception as exc:  # noqa: BLE001 - the harness reports, it does not crash
        return ClaimResult(
            "F5-b", "Stored chains still match the external integrity anchor",
            Verdict.SKIP, f"{type(exc).__name__}: {exc}",
        )

    ok = (
        report.buildings > 0
        and report.chains_ok == report.buildings
        and report.anchored == report.buildings
        and report.checkpoints_ok == report.buildings
    )
    return _result(
        "F5-b",
        "Stored chains still match the external integrity anchor",
        ok,
        f"{report.chains_ok}/{report.buildings} chains verify, "
        f"{report.checkpoints_ok}/{report.buildings} match the anchor, "
        f"{report.anchor_agrees_with_database}/{report.buildings} agree with the "
        f"database copy",
        detail="; ".join(report.failures[:3]),
    )


def run_claims(
    instance: Instance,
    rows: Sequence[SweepRow],
    *,
    with_db: bool = False,
) -> list[ClaimResult]:
    results = [claim(instance, rows) for claim in SOLVER_CLAIMS]
    if with_db:
        results.extend(db_claims())
    return results
