"""The CI workflow, checked as data.

The workflow runs for real now, against github.com/AhmadEbaid001/EcoForge. These
checks still earn their place: a YAML error or a gate that drifted out of
`scripts/check.py` costs a push and a five-minute round trip to discover.

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


def test_the_deploy_scripts_are_executable():
    """`deploy.sh` is invoked by path - by the drill locally and over ssh on the
    host - so the executable bit is not cosmetic. Git stores it, Windows checkouts
    do not carry it, and a commit made from one silently drops it back to 100644.
    That is exactly how it was committed, and the drill failed with

        ./infra/deploy/deploy.sh: Permission denied

    which arrives at the deploy step rather than at the commit that caused it.
    """
    import subprocess  # nosec B404

    listing = subprocess.run(  # nosec B603
        ["git", "ls-files", "--stage", "--", "infra/deploy"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout

    not_executable = [
        line.split("	", 1)[1]
        for line in listing.splitlines()
        if line.strip() and line.split("	", 1)[1].endswith(".sh")
        and not line.startswith("100755")
    ]
    assert not not_executable, (
        "these are run by path and git has them as non-executable: "
        + ", ".join(not_executable)
        + " - fix with `git update-index --chmod=+x <path>`"
    )
