"""The CI workflow, checked as data.

The workflow has never executed: the repository has no remote to push to, so
`scripts/check.py` is what actually enforces the gates. That makes these two checks
the only thing standing between a YAML error and a workflow that is silently never
scheduled on the day a remote finally exists.

These lived in the Grafana provisioning tests, which had nothing to do with CI beyond
both being files nobody runs. Splitting them means the Grafana file can be read as
what it is - an archive - without dragging a live concern along with it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_the_ci_workflow_parses():
    assert WORKFLOW.exists(), "no CI workflow committed"
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    # `on:` is YAML 1.1's boolean true, which is why it round-trips as True rather
    # than as the string. Accepting either is the point: this test is here to catch a
    # syntax error, not to relitigate the YAML spec.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers, "workflow declares no triggers"
    assert workflow["jobs"], "workflow declares no jobs"


def test_ci_runs_the_same_gates_as_the_local_script():
    """The two must not drift into disagreeing about what "passing" means."""
    from scripts.check import GATES

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = " ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"]
    )

    for name, command, _why in GATES:
        # Compare on the module or tool being invoked, not on the exact argv: CI
        # legitimately adds things like a coverage flag.
        target = next((part for part in command if not part.startswith("-")
                       and part != command[0]), None)
        assert target and target in steps, f"CI does not run the {name!r} gate ({target})"
