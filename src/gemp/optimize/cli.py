"""Phase 0 exit gate: a real allocation from the command line, with no infrastructure.

    python -m gemp.optimize.cli --budget 10000000
    python -m gemp.optimize.cli --budget 10000000 --compare
    python -m gemp.optimize.cli --budget 10000000 --objective raw_kwh --district-cap 4

No database, no MQTT broker, no containers. If the pipeline slips, this still runs
and still produces the artifact the project is judged on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gemp.domain.candidates import expand_portfolio
from gemp.domain.catalog import load_catalog, load_params
from gemp.domain.models import Allocation
from gemp.domain.portfolio import load_buildings
from gemp.optimize.objective import OBJECTIVE_UNITS, OBJECTIVES, objective_fn
from gemp.optimize.runner import SOLVERS, compare, improvement_pct, solve


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


def print_allocation(alloc: Allocation, top: int | None = None) -> None:
    unit = OBJECTIVE_UNITS[alloc.objective]
    value_of = objective_fn(alloc.objective)

    print()
    print(f"  solver     {alloc.solver}   status {alloc.status}   {alloc.solve_ms:.0f} ms")
    print(f"  objective  {alloc.objective} [{unit}]")
    print(f"  budget     {_fmt(alloc.budget_egp)} EGP")
    print(
        f"  spent      {_fmt(alloc.total_cost_egp)} EGP "
        f"({alloc.budget_used_frac:.1%} of budget)"
    )
    print(f"  funded     {alloc.buildings_funded} buildings")
    print(f"  saves      {_fmt(alloc.total_kwh_saving)} kWh/yr")
    print(f"             {_fmt(alloc.total_egp_saving)} EGP/yr at current tariff")
    print(f"             {_fmt(alloc.total_benefit_kgco2e)} kgCO2e over the horizon")
    print(f"  headline   {alloc.benefit_per_egp:.3f} kgCO2e per EGP of budget")

    if not alloc.items:
        print("\n  (nothing funded)")
        return

    rows = sorted(alloc.items, key=lambda i: value_of(_item_as_candidate(i)), reverse=True)
    shown = rows[:top] if top else rows

    print()
    print(f"  {'BUILDING':<18}{'DISTRICT':<17}{'INTERVENTION':<44}{'COST EGP':>12}{'kWh/yr':>11}")
    print(f"  {'-' * 100}")
    for i in shown:
        label = i.label if len(i.label) <= 42 else i.label[:39] + "..."
        print(
            f"  {i.building_code:<18}{i.district:<17}{label:<44}"
            f"{_fmt(i.cost_egp):>12}{_fmt(i.annual_kwh_saving):>11}"
        )
    if top and len(rows) > top:
        print(f"  ... and {len(rows) - top} more")


def _item_as_candidate(item):
    from gemp.optimize.runner import _as_candidate

    return _as_candidate(item)


def print_comparison(results: dict[str, Allocation], objective: str) -> None:
    unit = OBJECTIVE_UNITS[objective]
    value_of = objective_fn(objective)

    def total(a: Allocation) -> float:
        return sum(value_of(_item_as_candidate(i)) for i in a.items)

    baseline = results["equal_split"]
    print()
    print(f"  {'SOLVER':<16}{'STATUS':<12}{'FUNDED':>8}{'SPENT EGP':>15}"
          f"{'TOTAL ' + unit:>18}{'vs EQUAL SPLIT':>17}{'ms':>8}")
    print(f"  {'-' * 94}")
    for name in ("equal_split", "greedy", "greedy_upgrade", "cpsat"):
        a = results[name]
        delta = improvement_pct(a, baseline, objective)
        delta_str = "baseline" if name == "equal_split" else (
            "n/a" if delta == float("inf") else f"{delta:+.1f}%"
        )
        if name != "equal_split" and delta == float("inf"):
            delta_str = "baseline funded 0"
        print(
            f"  {name:<16}{a.status:<12}{a.buildings_funded:>8}{_fmt(a.total_cost_egp):>15}"
            f"{_fmt(total(a)):>18}{delta_str:>17}{a.solve_ms:>8.0f}"
        )

    strong = improvement_pct(results["cpsat"], results["greedy_upgrade"], objective)
    plain = improvement_pct(results["cpsat"], results["greedy"], objective)
    print()
    print(f"  CP-SAT over greedy_upgrade: {strong:+.2f}%   (over plain greedy: {plain:+.2f}%)")
    print(
        "  The first number is the honest one. Plain greedy never revisits a funded\n"
        "  building, so above roughly 16 M EGP it stops spending and the gap against it\n"
        "  measures its ceiling rather than the value of exact optimization. A small gap\n"
        "  against greedy_upgrade is expected: on an unconstrained knapsack a good\n"
        "  heuristic is near-optimal. Exact optimization earns its place through side\n"
        "  constraints no greedy can express (try --district-cap 4) and through\n"
        "  returning a proven optimum."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget", type=float, default=10_000_000, help="EGP")
    parser.add_argument("--solver", choices=sorted(SOLVERS), default="cpsat")
    parser.add_argument("--objective", choices=sorted(OBJECTIVES), default="lca_carbon")
    parser.add_argument("--district-cap", type=int, default=None,
                        help="max funded buildings per district (greedy cannot express this)")
    parser.add_argument("--max-bundle", type=int, default=None,
                        help="override params.yaml bundling.max_bundle_size")
    parser.add_argument("--compare", action="store_true",
                        help="run all three solvers on the same instance")
    parser.add_argument("--top", type=int, default=None, help="show only the top N rows")
    parser.add_argument("--json", type=Path, default=None, help="also write the result as JSON")
    args = parser.parse_args(argv)

    try:
        params = load_params()
        catalog = load_catalog()
        buildings = load_buildings()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    cap = args.district_cap
    if cap is None:
        cap = params.constraints.max_funded_per_district

    candidates = expand_portfolio(buildings, catalog, params, args.max_bundle)
    print(
        f"  portfolio  {len(buildings)} buildings, {len(catalog)} interventions, "
        f"{len(candidates)} candidate options"
        + (f", district cap {cap}" if cap is not None else "")
    )

    if args.compare:
        results = compare(candidates, buildings, args.budget,
                          objective=args.objective, max_funded_per_district=cap)
        print_comparison(results, args.objective)
        payload = {k: json.loads(v.model_dump_json()) for k, v in results.items()}
    else:
        alloc = solve(candidates, buildings, args.budget, solver=args.solver,
                      objective=args.objective, max_funded_per_district=cap)
        print_allocation(alloc, args.top)
        payload = json.loads(alloc.model_dump_json())

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\n  wrote {args.json}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
