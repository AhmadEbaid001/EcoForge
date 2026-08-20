"""Every gate CI runs, runnable now.

    python scripts/check.py            # lint, tests, data contract, claims
    python scripts/check.py --fast     # skip the evaluation harness

The repository has no remote yet, so `.github/workflows/ci.yml` never fires. This
script is what actually enforces the gates today, and CI runs the same four commands
so the two cannot drift into disagreeing about what "passing" means.

Exit status is 1 if any gate fails. Gates run to completion rather than stopping at
the first failure - finding out that three things are broken takes one run, not
three.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Measured at 83% without the container stack and 84% with it, over everything except
# the command-line entry points and the simulator (see the omit list in pyproject).
# The floor sits below both so that a normal run passes on a laptop with nothing
# running, and far enough below to survive a refactor that moves code around - it is
# there to catch a module losing its tests, not to be a target anyone optimises.
COVERAGE_FLOOR = 80

# (name, argv, why it is a gate). Kept as data because CI reads the same list, and a
# gate that exists in one place and not the other is worse than no gate.
GATES: list[tuple[str, list[str], str]] = [
    (
        "lint",
        [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
        "style and dead-import drift",
    ),
    (
        "tests",
        [sys.executable, "-m", "pytest", "-q", "--cov", f"--cov-fail-under={COVERAGE_FLOOR}"],
        "the contract suite; integration tests skip themselves without a database",
    ),
    (
        "data contract",
        [sys.executable, "-m", "gemp.domain.catalog", "--validate"],
        "catalog structure - NOT --strict, which fails on the uncited rows that are "
        "a known open item rather than a regression",
    ),
    (
        "claims",
        [sys.executable, "-m", "gemp.evaluate", "--quick", "--no-figures"],
        "every claim the paper makes, re-measured",
    ),
]


def run(name: str, argv: list[str]) -> tuple[bool, float]:
    print(f"\n=== {name} " + "=" * (66 - len(name)))
    started = time.perf_counter()
    # `argv` comes from GATES at the top of this file: four literal command lines.
    # No shell, and nothing here reads argv from anywhere else.
    result = subprocess.run(argv, cwd=ROOT)  # nosec B603
    elapsed = time.perf_counter() - started
    return result.returncode == 0, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fast", action="store_true",
                        help="skip the evaluation harness (~12 s of solving)")
    args = parser.parse_args(argv)

    gates = [g for g in GATES if not (args.fast and g[0] == "claims")]
    results: list[tuple[str, bool, float]] = []
    for name, command, _why in gates:
        ok, elapsed = run(name, command)
        results.append((name, ok, elapsed))

    print("\n" + "=" * 72)
    for name, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<16}{elapsed:>6.1f}s")

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"  {len(failed)} gate(s) failed: {', '.join(failed)}")
        return 1
    print("  all gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
