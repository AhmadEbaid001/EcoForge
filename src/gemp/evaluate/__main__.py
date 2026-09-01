"""    python -m gemp.evaluate [--with-db] [--quick] [--out DIR]

Exit status is the point. 0 means every claim the paper makes still holds; 1 means
one of them has stopped holding and a sentence somewhere needs rewriting. A claim
documented as open reports FAIL and is counted separately, because a gate that is
permanently red teaches everyone to ignore the gate. There are none open today.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from gemp.evaluate.claims import ClaimResult, Verdict, run_claims
from gemp.evaluate.sweep import (
    DEFAULT_BUDGETS,
    DEFAULT_CAPS,
    DEFAULT_OBJECTIVES,
    Instance,
    run_sweep,
    write_sweep_csv,
)

log = logging.getLogger("gemp.evaluate")

# Enough of the grid to exercise every claim, few enough solves to sit inside a CI
# run. The top of the range matters: the first version stopped at 20 M EGP, which is
# below the point where plain greedy saturates, so two claims reported failures that
# were artifacts of the grid rather than findings about the solvers.
# Four points spanning the portfolio's whole decision range: 25 M funds twelve
# of the fifty buildings, 100 M funds thirty-six, 175 M is past the point plain
# greedy stops spending. The old set topped out at 40 M, which on a portfolio of
# public buildings is still the steep part of the curve, so the saturation claim
# could only ever SKIP under --quick.
QUICK_BUDGETS = (25_000_000.0, 75_000_000.0, 125_000_000.0, 175_000_000.0)


def write_claims_csv(claims: list[ClaimResult], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "verdict", "known_open", "statement", "measured", "detail"])
        for claim in claims:
            writer.writerow([
                claim.id, claim.verdict.value, "yes" if claim.known_open else "",
                claim.statement, claim.measured, claim.detail,
            ])
    return path


def print_claims(claims: list[ClaimResult]) -> None:
    print()
    print(f"  {'ID':<7}{'VERDICT':<10}{'CLAIM':<58}MEASURED")
    print(f"  {'-' * 118}")
    for claim in claims:
        mark = claim.verdict.value
        if claim.verdict is Verdict.FAIL and claim.known_open:
            mark = "FAIL*"
        statement = claim.statement if len(claim.statement) <= 56 else claim.statement[:53] + "..."
        print(f"  {claim.id:<7}{mark:<10}{statement:<58}{claim.measured}")
        if claim.detail and claim.verdict is not Verdict.PASS:
            print(f"  {'':<17}{claim.detail[:110]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--with-db", action="store_true",
                        help="also measure forecasting and anomaly detection (needs the DB)")
    parser.add_argument("--quick", action="store_true",
                        help="three budgets instead of twenty; enough to check every claim")
    parser.add_argument("--out", type=Path, default=Path("out/evaluation"),
                        help="where the CSVs and figures are written")
    parser.add_argument("--no-figures", action="store_true")
    parser.add_argument("--max-bundle", type=int, default=None,
                        help="override params.yaml bundling.max_bundle_size")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")

    try:
        instance = Instance.load(args.max_bundle)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    budgets = QUICK_BUDGETS if args.quick else DEFAULT_BUDGETS
    rows = run_sweep(
        instance, budgets=budgets, objectives=DEFAULT_OBJECTIVES, caps=DEFAULT_CAPS
    )
    claims = run_claims(instance, rows, with_db=args.with_db)

    sweep_csv = write_sweep_csv(rows, args.out / "optimizer_sweep.csv")
    claims_csv = write_claims_csv(claims, args.out / "claims.csv")

    figures: list[Path] = []
    if not args.no_figures:
        from gemp.evaluate.figures import draw_all

        figures = draw_all(rows, args.out / "figures")

    print_claims(claims)

    blocking = [c for c in claims if c.blocking]
    known_open = [c for c in claims if c.verdict is Verdict.FAIL and c.known_open]
    skipped = [c for c in claims if c.verdict is Verdict.SKIP]

    print()
    print(f"  {len(rows)} solves, {len(claims)} claims: "
          f"{sum(1 for c in claims if c.verdict is Verdict.PASS)} pass, "
          f"{len(blocking)} failed, {len(known_open)} known-open, {len(skipped)} skipped")
    print(f"  wrote {sweep_csv}")
    print(f"  wrote {claims_csv}")
    for path in figures:
        print(f"  wrote {path}")

    if known_open:
        print("\n  * known-open, tracked, not blocking:")
        for claim in known_open:
            print(f"      {claim.id}  {claim.statement} - {claim.measured}")
    if not args.with_db:
        print("\n  forecasting and anomaly claims not measured; add --with-db")

    print()
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
