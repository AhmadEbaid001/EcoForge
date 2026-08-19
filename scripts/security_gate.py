"""One verdict from every security scanner, and the register of what was excused.

The policy is block on anything. Not "block on high", not "warn and carry on" -
any finding from any scanner stops the build and therefore stops the deploy.

A policy that strict only survives contact with reality if there is an honest way
to say "we know, and here is why it is acceptable". Without one, the first
unfixable finding in a transitive dependency turns into somebody adding
`|| true` to a workflow step at eleven at night, and from then on the gate is
decorative. So exceptions are a FILE, not a flag:

    .security/allowlist.yml

Every entry names the finding, who accepted it, why, and the date it stops being
accepted. An expired entry is itself a build failure - which is the whole point.
An exception that never expires is a policy change made by whoever was on shift,
and it will outlive their memory of why they made it.

Scanners disagree about output formats, so everything is normalised through SARIF
where the tool emits it and through a small adapter where it does not.

Usage:

    python scripts/security_gate.py reports/*.sarif reports/pip-audit.json
    python scripts/security_gate.py --summary "$GITHUB_STEP_SUMMARY" reports/
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / ".security" / "allowlist.yml"

# SARIF calls them levels; everything else calls them severities. Anything not in
# this map is treated as a finding at unknown severity, which still blocks - an
# unrecognised label is not a reason to let something through.
SARIF_LEVELS = {"error", "warning", "note", "none"}


@dataclass(frozen=True)
class Finding:
    """One thing a scanner objected to, in the only shape this script cares about."""

    tool: str
    rule: str
    severity: str
    path: str
    line: int
    title: str

    @property
    def key(self) -> str:
        """What an allowlist entry matches on."""
        return f"{self.tool}:{self.rule}"

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.path else "-"
        return f"[{self.severity}] {self.key} {where} — {self.title}"


@dataclass
class Exception_:
    """One accepted finding, with an expiry date and a name attached to it."""

    key: str
    reason: str
    owner: str
    expires: date
    path: str | None = None
    matched: list[Finding] = field(default_factory=list)

    def covers(self, finding: Finding) -> bool:
        if self.key != finding.key:
            return False
        # An entry may be scoped to a path, so the same rule can be accepted in a
        # seed script and still block in the API.
        return not (self.path and self.path not in finding.path)

    @property
    def expired(self) -> bool:
        return self.expires < datetime.now(UTC).date()


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------


def read_sarif(path: Path) -> list[Finding]:
    """SARIF 2.1.0, which semgrep, bandit, trivy, hadolint and gitleaks all emit."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A report that cannot be read is not an absence of findings. A scanner that
        # crashed halfway through writes truncated JSON, and treating that as "clean"
        # is how a broken gate reports success for a month.
        raise SystemExit(f"cannot read {path}: {exc}") from exc

    findings: list[Finding] = []
    for run in document.get("runs", []):
        driver = run.get("tool", {}).get("driver", {})
        tool = driver.get("name", path.stem)

        # Rule metadata lives apart from the results and carries the severity for
        # several of these tools.
        rules = {
            rule.get("id"): rule
            for rule in driver.get("rules", [])
            if rule.get("id")
        }

        for result in run.get("results", []):
            rule_id = result.get("ruleId") or "unknown"
            rule = rules.get(rule_id, {})
            severity = (
                result.get("level")
                or rule.get("defaultConfiguration", {}).get("level")
                or rule.get("properties", {}).get("security-severity")
                or "unknown"
            )
            if severity not in SARIF_LEVELS:
                severity = _severity_from_score(severity)

            location = (result.get("locations") or [{}])[0]
            physical = location.get("physicalLocation", {})
            artifact = physical.get("artifactLocation", {}).get("uri", "")
            line = physical.get("region", {}).get("startLine", 0)

            message = result.get("message", {}).get("text", "") or rule.get(
                "shortDescription", {}
            ).get("text", "")

            findings.append(Finding(
                tool=tool, rule=rule_id, severity=str(severity),
                path=artifact, line=int(line or 0),
                title=message.strip().splitlines()[0] if message else rule_id,
            ))
    return findings


def _severity_from_score(value: str) -> str:
    """SARIF carries CVSS as a string in properties; map it onto a word."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


def read_pip_audit(path: Path) -> list[Finding]:
    """pip-audit's own JSON. It has no SARIF output, and it is the authoritative
    source for Python advisories, so it gets an adapter rather than a substitute."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc

    findings = []
    for dependency in document.get("dependencies", []):
        name = dependency.get("name", "?")
        version = dependency.get("version", "?")
        for vuln in dependency.get("vulns", []):
            fix = ", ".join(vuln.get("fix_versions", [])) or "no fix published"
            findings.append(Finding(
                tool="pip-audit",
                rule=vuln.get("id", "UNKNOWN"),
                severity="unknown",
                path=f"{name}=={version}",
                line=0,
                title=f"{name} {version}: {vuln.get('description', '').strip().splitlines()[0] if vuln.get('description') else 'advisory'} (fixed in: {fix})",
            ))
    return findings


def collect(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path.is_dir():
            findings.extend(collect(sorted(path.iterdir())))
        elif path.suffix == ".sarif":
            findings.extend(read_sarif(path))
        elif path.name.startswith("pip-audit") and path.suffix == ".json":
            findings.extend(read_pip_audit(path))
    return findings


# --------------------------------------------------------------------------
# the register
# --------------------------------------------------------------------------


def load_exceptions() -> list[Exception_]:
    if not ALLOWLIST.exists():
        return []

    document = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8")) or {}
    exceptions = []
    for index, entry in enumerate(document.get("accepted", []) or []):
        missing = [k for k in ("id", "reason", "owner", "expires") if not entry.get(k)]
        if missing:
            raise SystemExit(
                f"{ALLOWLIST}: entry {index} is missing {', '.join(missing)}. "
                "Every exception names what it excuses, why, who accepted it and "
                "when that acceptance runs out."
            )
        expires = entry["expires"]
        if not isinstance(expires, date):
            expires = date.fromisoformat(str(expires))
        exceptions.append(Exception_(
            key=str(entry["id"]), reason=str(entry["reason"]),
            owner=str(entry["owner"]), expires=expires, path=entry.get("path"),
        ))
    return exceptions


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path,
                        help="SARIF files, pip-audit JSON, or directories of them")
    parser.add_argument("--summary", type=Path, default=None,
                        help="write a Markdown summary here (GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)

    findings = collect([Path(p) for p in args.reports])
    exceptions = load_exceptions()

    expired = [e for e in exceptions if e.expired]
    blocking: list[Finding] = []
    excused: list[tuple[Finding, Exception_]] = []

    for finding in findings:
        cover = next((e for e in exceptions if not e.expired and e.covers(finding)), None)
        if cover is None:
            blocking.append(finding)
        else:
            cover.matched.append(finding)
            excused.append((finding, cover))

    # An exception nobody needs any more is not harmless: it is a standing permission
    # to reintroduce the thing it excuses. Reported, but it does not fail the build -
    # the finding being gone is good news arriving in an inconvenient shape.
    unused = [e for e in exceptions if not e.expired and not e.matched]

    report = _render(blocking, excused, expired, unused)
    print(report)
    if args.summary:
        args.summary.write_text(report, encoding="utf-8")

    if expired:
        print(f"\nFAIL: {len(expired)} security exception(s) have expired.", file=sys.stderr)
    if blocking:
        print(f"FAIL: {len(blocking)} unaccepted finding(s).", file=sys.stderr)
    return 1 if (blocking or expired) else 0


def _render(blocking, excused, expired, unused) -> str:
    lines = ["## Security gate", ""]

    if not blocking and not expired:
        lines.append("**PASS** — no unaccepted findings.")
    else:
        lines.append(f"**FAIL** — {len(blocking)} unaccepted, {len(expired)} expired exception(s).")

    if blocking:
        lines += ["", "### Blocking", ""]
        by_tool: dict[str, list[Finding]] = {}
        for finding in blocking:
            by_tool.setdefault(finding.tool, []).append(finding)
        for tool, items in sorted(by_tool.items()):
            lines.append(f"**{tool}** — {len(items)}")
            lines += [f"- `{f.key}` {f.path}:{f.line} — {f.title}" for f in items[:25]]
            if len(items) > 25:
                lines.append(f"- …and {len(items) - 25} more")
            lines.append("")

    if expired:
        lines += ["", "### Expired exceptions", "",
                  "These were accepted with an end date and that date has passed. "
                  "Fix the finding, or re-accept it deliberately with a new date and "
                  "a reason that is still true.", ""]
        lines += [f"- `{e.key}` expired {e.expires} — {e.owner}: {e.reason}" for e in expired]

    if excused:
        lines += ["", "### Accepted", ""]
        seen = set()
        for _finding, cover in excused:
            if cover.key in seen:
                continue
            seen.add(cover.key)
            lines.append(f"- `{cover.key}` until {cover.expires} — {cover.owner}: {cover.reason}")

    if unused:
        lines += ["", "### Exceptions no longer needed", "",
                  "Nothing matched these. Delete them: an exception left behind is a "
                  "standing permission to reintroduce what it excused.", ""]
        lines += [f"- `{e.key}` — {e.owner}" for e in unused]

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
