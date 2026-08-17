"""The UI's contract with the API, and the offline guarantee it depends on.

The map is the demonstration. Nothing here renders a browser - that would need a
headless driver this project does not have - but the two things that actually break
a demonstration are checkable without one: an endpoint changing shape underneath the
UI, and a stray external reference sneaking into the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"

# Anything that would make the browser reach off-origin. A CDN link added "just for
# this one icon" is exactly how an offline demonstration dies.
EXTERNAL_PATTERNS = [
    re.compile(r"""https?://(?!localhost|127\.0\.0\.1)""", re.I),
    re.compile(r"""//(?!localhost|127\.0\.0\.1)[a-z0-9-]+\.[a-z]{2,}""", re.I),
    re.compile(r"@import\s+url\(\s*['\"]?https?:", re.I),
]

# URLs inside comments are prose, not requests. The dataset source is cited in a
# comment in the replay module and the same courtesy applies here.
COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*|<!--|#)")


def source_files():
    return sorted(p for p in WEB.iterdir() if p.suffix in {".html", ".css", ".js"})


def test_the_ui_exists():
    names = {p.name for p in source_files()}
    assert {"index.html", "style.css", "app.js"} <= names


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_external_resource_references(path: Path):
    """F13, as a regression guard.

    The demonstration must survive an unplugged network cable. Verified live in the
    browser once - `performance.getEntriesByType('resource')` reported nothing
    off-origin - but a verification that ran once protects nothing. This is the check
    that keeps protecting it.
    """
    offenders = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if COMMENT_LINE.match(line):
            continue
        for pattern in EXTERNAL_PATTERNS:
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, "external references would break the offline demo:\n" + "\n".join(offenders)


def test_no_bundler_or_package_manifest():
    """No build step is a feature here: the files nginx serves are the files in the
    repository, so what is reviewed is what runs."""
    for forbidden in ("package.json", "node_modules", "webpack.config.js", "vite.config.js"):
        assert not (WEB / forbidden).exists()


def test_every_element_the_script_reaches_for_exists_in_the_page():
    """A typo in an id is silent in JavaScript: the handler simply never fires, the
    control looks fine and does nothing. Cheap to catch here."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")

    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    ids_wanted = set(re.findall(r"""\$\(['"]([^'"]+)['"]\)""", script))
    ids_wanted |= set(re.findall(r"""getElementById\(['"]([^'"]+)['"]\)""", script))

    missing = sorted(ids_wanted - ids_in_html)
    assert not missing, f"app.js references ids that do not exist: {missing}"


def test_api_paths_used_by_the_ui_are_the_ones_the_server_serves():
    """Keeps the UI and the routes from drifting apart silently."""
    from gemp.api.main import app

    script = (WEB / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r"`\$\{API\}(/[a-z0-9/{}_-]+)", script))
    served = {r.path.replace("/api/v1", "") for r in app.routes if hasattr(r, "path")}

    for path in used:
        # Template segments in the script become path parameters on the server.
        concrete = path.split("${")[0].rstrip("/")
        assert any(s.startswith(concrete) for s in served), f"UI calls unknown route {path}"
