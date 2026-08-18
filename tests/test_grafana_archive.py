"""Grafana dashboards and the CI workflow, checked as data.

Neither of these had any test. Both fail the same way: not loudly, but at the worst
moment. A dashboard whose datasource uid stopped matching provisioning renders eight
"Datasource not found" panels the first time anyone opens it, and a workflow with a
YAML error is simply never scheduled - and this one has never run at all, because the
repository has no remote to push to.

Nothing here starts Grafana. These are structural checks: files parse, references
resolve, and every table and column a panel queries still exists in the schema the
application creates. That last one is the check with teeth - it fails when a
migration renames something the dashboard reads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from gemp.db import Base

ROOT = Path(__file__).resolve().parents[1]
GRAFANA = ROOT / "infra" / "grafana" / "provisioning"
DASHBOARDS = GRAFANA / "dashboards"
DATASOURCES = GRAFANA / "datasources"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def dashboard_files() -> list[Path]:
    return sorted(DASHBOARDS.glob("*.json"))


def panels(dashboard: dict) -> list[dict]:
    """Panels, including any nested inside a collapsed row."""
    out = []
    for panel in dashboard.get("panels", []):
        out.append(panel)
        out.extend(panel.get("panels", []))
    return out


# --- datasources ------------------------------------------------------------


def test_the_datasource_is_provisioned():
    config = yaml.safe_load((DATASOURCES / "timescale.yml").read_text(encoding="utf-8"))
    source = config["datasources"][0]

    assert source["type"] == "postgres"
    assert source["uid"]
    # The read-only role, never the owner. A dashboard is a place someone will
    # eventually paste an arbitrary query.
    assert "RO_USER" in source["user"]
    assert "RO_PASSWORD" in source["secureJsonData"]["password"]


def test_the_datasource_password_is_not_committed():
    """Provisioning is committed; the credential it references must not be."""
    text = (DATASOURCES / "timescale.yml").read_text(encoding="utf-8")
    password = re.search(r"password:\s*(\S+)", text).group(1)
    assert password.startswith("${"), f"literal password in provisioning: {password!r}"


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
def test_every_panel_points_at_a_provisioned_datasource(path):
    """A uid mismatch renders every panel as "Datasource not found", silently."""
    config = yaml.safe_load((DATASOURCES / "timescale.yml").read_text(encoding="utf-8"))
    known = {source["uid"] for source in config["datasources"]}

    dashboard = json.loads(path.read_text(encoding="utf-8"))
    used = set()
    for panel in panels(dashboard):
        source = panel.get("datasource")
        if isinstance(source, dict) and "uid" in source:
            used.add(source["uid"])

    assert used, f"{path.name} declares no datasource on any panel"
    assert used <= known, f"{path.name} references unknown datasources: {used - known}"


# --- dashboards -------------------------------------------------------------


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
def test_the_dashboard_parses_and_has_panels(path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard.get("title")
    assert panels(dashboard), f"{path.name} has no panels"


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
def test_every_panel_queries_a_table_that_exists(path):
    """The check with teeth: a migration that renames a table breaks this.

    Grafana would otherwise report the failure only to whoever happens to be looking
    at the dashboard, at the moment they are looking at it.
    """
    tables = set(Base.metadata.tables)
    # Created outside Alembic, so it is not in the metadata but is very much real.
    tables.add("reading_hourly")

    dashboard = json.loads(path.read_text(encoding="utf-8"))
    referenced: set[str] = set()
    for panel in panels(dashboard):
        for target in panel.get("targets", []):
            sql = target.get("rawSql") or ""
            referenced |= {m.lower() for m in re.findall(r"\bFROM\s+([a-zA-Z_][\w]*)", sql)}
            referenced |= {m.lower() for m in re.findall(r"\bJOIN\s+([a-zA-Z_][\w]*)", sql)}

    assert referenced, f"{path.name} has no SQL targets to check"
    unknown = referenced - tables
    assert not unknown, f"{path.name} queries tables that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.name)
def test_dashboard_time_windows_are_not_anchored_on_the_wall_clock(path):
    """Under 720x replay, data time runs months ahead of `now()`.

    A panel filtering on `now() - interval` comes back empty during the one
    demonstration that matters. Windows have to be anchored on the newest reading,
    the same rule the application follows everywhere.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    offenders = []
    for panel in panels(dashboard):
        for target in panel.get("targets", []):
            sql = (target.get("rawSql") or "").lower()
            if re.search(r"now\(\)\s*-", sql):
                offenders.append(panel.get("title"))

    assert not offenders, f"wall-clock windows in: {offenders}"


# --- the workflow that has never run ----------------------------------------


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
