"""The security gate, and the exception register that keeps it usable.

The policy is block on anything. What these pin is not "does it block" - a gate
that blocks everything is easy - but the three ways such a gate goes wrong in
practice:

  * a scanner crashes and its truncated report reads as "nothing found"
  * an exception is added once and quietly becomes permanent
  * an exception outlives the finding and nobody removes it

The first is a gate that reports success for a month. The second is how "block on
anything" becomes "block on nothing" without anybody deciding to change it.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import security_gate  # noqa: E402


def sarif(rule: str, *, tool: str = "semgrep", level: str = "error",
          path: str = "src/gemp/api/main.py", line: int = 10) -> dict:
    return {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": tool, "rules": [{"id": rule}]}},
            "results": [{
                "ruleId": rule,
                "level": level,
                "message": {"text": f"{rule} triggered"},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": path},
                    "region": {"startLine": line},
                }}],
            }],
        }],
    }


@pytest.fixture
def reports(tmp_path):
    (tmp_path / "reports").mkdir()
    return tmp_path / "reports"


@pytest.fixture
def allowlist(tmp_path, monkeypatch):
    path = tmp_path / "allowlist.yml"
    monkeypatch.setattr(security_gate, "ALLOWLIST", path)

    def write(entries):
        path.write_text(yaml.safe_dump({"accepted": entries}), encoding="utf-8")

    return write


def test_no_findings_passes(reports):
    assert security_gate.main([str(reports)]) == 0


def test_any_finding_blocks(reports):
    """Not high, not critical. Any."""
    (reports / "semgrep.sarif").write_text(json.dumps(sarif("py.weak", level="note")))
    assert security_gate.main([str(reports)]) == 1


def test_a_truncated_report_is_a_failure_not_an_absence(reports):
    """A scanner that crashed half way through writes invalid JSON.

    Treating that as "no findings" is how a gate reports success for a month while
    scanning nothing, and it is the failure nobody notices because it looks exactly
    like everything being fine.
    """
    (reports / "semgrep.sarif").write_text('{"runs": [{"tool"')
    with pytest.raises(SystemExit):
        security_gate.main([str(reports)])


def test_an_accepted_finding_does_not_block(reports, allowlist):
    (reports / "semgrep.sarif").write_text(json.dumps(sarif("py.weak")))
    allowlist([{
        "id": "semgrep:py.weak",
        "reason": "the call is on a constant, not on request data",
        "owner": "ahmed",
        "expires": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
    }])
    assert security_gate.main([str(reports)]) == 0


def test_an_expired_exception_fails_even_with_no_findings(reports, allowlist):
    """The expiry is the whole mechanism.

    An exception that never runs out is a policy change made by whoever was on
    shift, and it outlives every memory of why it was made. So the date is enforced
    on its own: the build fails when acceptance lapses, whether or not the finding
    is still there, which is what forces the decision to be taken again by someone
    awake.
    """
    allowlist([{
        "id": "semgrep:py.weak",
        "reason": "accepted last quarter",
        "owner": "ahmed",
        "expires": (datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
    }])
    assert security_gate.main([str(reports)]) == 1


def test_an_exception_must_say_who_accepted_it_and_why(reports, allowlist):
    allowlist([{"id": "semgrep:py.weak", "expires": "2099-01-01"}])
    with pytest.raises(SystemExit, match="missing"):
        security_gate.main([str(reports)])


def test_an_exception_can_be_scoped_to_one_path(reports, allowlist):
    (reports / "a.sarif").write_text(json.dumps(sarif("py.weak", path="scripts/seed.py")))
    (reports / "b.sarif").write_text(json.dumps(sarif("py.weak", path="src/gemp/api/main.py")))
    allowlist([{
        "id": "semgrep:py.weak",
        "path": "scripts/",
        "reason": "seed script, not reachable from the API",
        "owner": "ahmed",
        "expires": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
    }])
    # The one in scripts/ is excused; the one in src/ is not, so this still blocks.
    assert security_gate.main([str(reports)]) == 1


def test_pip_audit_json_is_read(reports):
    """pip-audit has no SARIF output and is the authoritative source for Python
    advisories, so it gets an adapter rather than a substitute."""
    (reports / "pip-audit.json").write_text(json.dumps({
        "dependencies": [{
            "name": "somelib", "version": "1.0.0",
            "vulns": [{"id": "GHSA-xxxx", "description": "a flaw", "fix_versions": ["1.0.1"]}],
        }],
    }))
    assert security_gate.main([str(reports)]) == 1
