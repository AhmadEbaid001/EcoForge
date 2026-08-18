"""The Grafana provisioning archive. GRAFANA IS NOT DEPLOYED.

Read that first, because a green tick on this file used to imply otherwise. The stack
is five containers - timescaledb, mosquitto, core, sim, nginx - and Grafana is not one
of them. Its panels became views inside the application when a second service turned
out to mean a second login, a second set of credentials, and SQL that could disagree
with the application about what a number meant.

The provisioning files are kept anyway, for two reasons that are worth separating.
The paper describes Phase 3 as including Grafana, so the files are the evidence that
the work was done rather than claimed. And `docker compose` could start it again from
exactly these files, which is the only reason it is still worth knowing whether they
would work.

So that is what these tests check, and all they check: that the archive would still
provision cleanly if anyone started it. Files parse, datasource uids resolve, and
every table a panel queries still exists in the schema the application creates. The
last one is the check with teeth - a migration that renames a table is the way this
archive rots, and rotting silently is how it would come to be quoted as working.

`test_no_grafana_service_is_declared` is the one that keeps the rest honest: it fails
if Grafana ever comes back to the compose file without this docstring being rewritten.
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
COMPOSE = ROOT / "docker-compose.yml"


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


# --- the decision this archive records --------------------------------------


def test_no_grafana_service_is_declared():
    """The archive must stay an archive.

    Every document in the project says five containers. If a sixth is ever added
    back, this fails before the docs, the README and the module docstring above
    quietly become wrong together - which is the failure mode that made a passing
    provisioning test misleading in the first place.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = set(compose["services"])

    assert "grafana" not in services, (
        "Grafana is back in the stack. Update tests/test_grafana_archive.py, the "
        "README and the team's working notes together, or take it out again."
    )
    assert len(services) == 5, f"expected five services, found {sorted(services)}"
