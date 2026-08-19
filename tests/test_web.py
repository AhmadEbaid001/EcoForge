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
    """Every file nginx serves, including the ES modules under web/js/."""
    return sorted(p for p in WEB.rglob("*") if p.suffix in {".html", ".css", ".js"})


def script_text() -> str:
    """All the JavaScript, concatenated.

    The UI became several modules when it grew a login screen and dashboards, and
    these checks are about what the shipped code as a whole references - not about
    which file happens to hold a given line.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in WEB.rglob("*.js"))


def test_the_ui_exists():
    names = {p.name for p in source_files()}
    assert {"index.html", "style.css", "app.js", "api.js", "map.js"} <= names


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
    # The shell renders its own markup, so the ids a script reaches for may be
    # declared in another module's template rather than in index.html.
    html = "\n".join(p.read_text(encoding="utf-8")
                     for p in [*WEB.rglob("*.html"), *WEB.rglob("*.js")])
    script = script_text()

    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    # Elements the shell builds at runtime - the stale strip and the result
    # strip are created with createElement when there is something to say -
    # carry their id from script rather than from a template.
    ids_in_html |= set(re.findall(r"""\.id\s*=\s*['"]([^'"]+)['"]""", html))
    ids_wanted = set(re.findall(r"""\$\(['"]([^'"]+)['"]\)""", script))
    ids_wanted |= set(re.findall(r"""getElementById\(['"]([^'"]+)['"]\)""", script))

    missing = sorted(ids_wanted - ids_in_html)
    assert not missing, f"the UI references ids that do not exist: {missing}"


def test_api_paths_used_by_the_ui_are_the_ones_the_server_serves():
    """Keeps the UI and the routes from drifting apart silently."""
    from gemp.api.main import app

    script = script_text()
    used = set(re.findall(r"`\$\{API\}(/[a-z0-9/{}_-]+)", script))
    # api.js states its paths as plain strings against a fixed base.
    used |= set(re.findall(r"""request\(['"][A-Z]+['"], ?[`'"](/[a-z0-9/{}_$-]+)""", script))
    # Walk into included routers. FastAPI 0.141 keeps one as a single wrapper entry
    # rather than flattening its children, so reading only the top level reports that
    # every auth and dashboard endpoint is missing.
    served = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        nested = getattr(route, "original_router", None)
        children = getattr(nested, "routes", None) or getattr(route, "routes", None)
        if children:
            stack.extend(children)
        elif hasattr(route, "path"):
            served.add(route.path.replace("/api/v1", ""))

    for path in used:
        # Template segments in the script become path parameters on the server.
        concrete = path.split("${")[0].rstrip("/")
        assert any(s.startswith(concrete) for s in served), f"UI calls unknown route {path}"


def test_the_zoom_ceiling_lets_a_footprint_be_read():
    """Measured in the browser, not assumed.

    Real footprints switch in at k = 3, where the median outline is 6.4 screen pixels
    across; at the old ceiling of 8 it was still only 10.4. Correct geometry nobody
    can read makes "real OpenStreetMap footprints" a claim rather than something a
    reviewer can see. The ceiling has to leave room for the outline to get big enough
    to recognise.
    """
    source = script_text()

    ceiling = re.search(r"const MAX_ZOOM = (\d+)", source)
    switch = re.search(r"const FOOTPRINT_ZOOM = (\d+)", source)
    assert ceiling and switch

    # At least a 4x range past the switch, or the outlines never become legible.
    assert int(ceiling.group(1)) >= int(switch.group(1)) * 4


def test_hidden_modals_are_actually_hidden():
    """`hidden` on a flex element does nothing without an explicit rule.

    `[hidden]` gets its `display: none` from the browser's own stylesheet, and any
    author rule that sets display beats a user-agent rule. `.modal { display: flex }`
    therefore left both modals on screen from the moment the page loaded: the map
    opened behind a full-screen overlay reading "Solving...", with nothing solving.
    """
    css = (WEB / "style.css").read_text(encoding="utf-8")

    sets_display = re.search(r"\.modal\s*\{[^}]*display\s*:", css, re.S)
    if not sets_display:
        return                                   # no display rule, no conflict to fix

    assert re.search(r"\.modal\[hidden\]\s*\{[^}]*display\s*:\s*none", css), (
        ".modal sets display, so .modal[hidden] must set display:none or the hidden "
        "attribute is silently ignored"
    )


def test_the_ui_does_not_claim_three_solvers():
    """There are four. Stale copy in front of judges reads as a system nobody checked."""
    for path in [*WEB.rglob("*.html"), *WEB.rglob("*.js")]:
        text = path.read_text(encoding="utf-8")
        assert "three methods" not in text
        assert "all three" not in text


def test_the_root_font_size_is_not_stated_in_rem():
    """A rem font-size on `html` compounds against itself, silently.

    `html, body { font: var(--fs-body) ... }` set the ROOT to .875rem, so the root
    became 14px and every rem token then resolved against 14 instead of 16 - one
    compounding step, applied to the entire type scale. Measured in the browser: body
    text 12.25px where the token table promised 14, captions 10.5px, navigation
    11.4px. The interface was 12.5% smaller than it was designed to be.

    It also overrides the reader: someone who has raised their browser's default font
    size gets it scaled back down.
    """
    css = (WEB / "style.css").read_text(encoding="utf-8")

    # The `html` rule may set font-size, but only in a unit that cannot compound.
    for match in re.finditer(r"(?:^|\})\s*html\s*(?:,\s*[^{]+)?\{([^}]*)\}", css, re.S):
        block = match.group(1)
        size = re.search(r"font-size\s*:\s*([^;]+)", block)
        shorthand = re.search(r"\bfont\s*:\s*([^;]+)", block)
        for declaration in (size, shorthand):
            if declaration and "rem" in declaration.group(1):
                raise AssertionError(
                    "html is sized in rem, which compounds the whole type scale: "
                    f"{declaration.group(0).strip()}"
                )


def test_the_map_does_not_solve_for_a_role_that_may_not(): 
    """A viewer's first sight of the product used to be a red error.

    `main()` called `solve()` on mount unconditionally, and `/optimize` is
    analyst-only - so a viewer got a red health indicator reading "this action
    requires the analyst role", five metrics showing em-dashes, and a budget slider
    that 403'd on every drag. Nothing was broken; they simply were not allowed, and
    the screen had no way to say so.
    """
    source = (WEB / "js" / "map.js").read_text(encoding="utf-8")

    assert "canSolve" in source, "the map must know whether the caller may solve"
    assert re.search(r"if \(canSolve\)\s*\{\s*await solve\(\)", source), (
        "solve() must be reached only when the caller holds the analyst role"
    )
    assert "showStoredRun" in source, (
        "a viewer needs the most recent stored allocation, not an empty map"
    )


def test_view_scoped_listeners_are_tied_to_a_lifetime():
    """Replacing `#view` drops the listeners inside it and nothing else.

    The map listens on `window` for resize, mouseup and mousemove. Without an abort
    signal those survive every navigation away and back: five visits to the Map meant
    five resize handlers, each rebuilding the projection for a screen nobody was
    looking at. Measured in the browser before the fix - the count climbed 1, 2, 3, 4;
    after it, it stays at 1.
    """
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "AbortController" in app, "the shell must own a lifetime per mounted view"
    assert "signal" in app, "the lifetime has to reach the view that needs it"

    # Counting rather than parsing: a listener's own body can contain `);`, so any
    # regex that tries to find where the call ends gets it wrong on the first
    # multi-statement handler. Every window-level registration has to carry a signal,
    # so the two counts simply have to match.
    map_source = (WEB / "js" / "map.js").read_text(encoding="utf-8")
    registrations = map_source.count("window.addEventListener(")
    signalled = len(re.findall(r"\{\s*signal\s*\}\s*\)", map_source))

    assert registrations and signalled >= registrations, (
        f"{registrations} window listeners in map.js but only {signalled} bound to a "
        "lifetime - the unbound ones survive every navigation away and back"
    )


def test_credentials_are_never_minted_through_a_browser_prompt():
    """`window.prompt` is clear text, has no confirmation field, validates nothing
    before the request, and cannot be filled from a password manager. The one flow
    that creates accounts is the last place to accept that."""
    for path in WEB.rglob("*.js"):
        source = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not COMMENT_LINE.match(line)
        )
        assert "window.prompt(" not in source, f"{path.name} mints input via window.prompt"


def test_charts_redraw_at_the_width_they_are_given():
    """A chart drawn once at 760px scales, but scaling is not redrawing.

    Tick placement, label spacing and gridline geometry were all decided for 760
    pixels, so a chart in a 980px column was stretched and one in a 360px column was
    cramped. The map has redrawn on resize since it was written; the charts never
    had. Measured after the fix: viewBox follows the container - 980, then 360, then
    980 again.
    """
    charts = (WEB / "js" / "charts.js").read_text(encoding="utf-8")

    assert "hydrateCharts" in charts, "charts must expose a way to connect them"
    assert "ResizeObserver" in charts, (
        "the sidebar collapsing changes a chart's width without changing the window's"
    )
    assert "window.addEventListener('resize'" in charts, (
        "ResizeObserver does not fire in every harness; a window resize is the common "
        "case and must work on its own"
    )

    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "hydrateCharts" in app, "the shell connects charts after a view renders"
    assert "MutationObserver" in app, (
        "a view that rebuilds itself through its own Refresh button replaces the "
        "markup the shell hydrated, and nothing would reconnect it"
    )


def test_a_screen_says_how_old_it_is():
    """Data time advances at 720x, so a stamp states a moment without admitting the
    moment has passed. Nothing is polled - re-fetching on a timer during a
    demonstration moves numbers under whoever is talking about them - so the stamp
    ages instead, and says so once it is worth pressing Re-read.

    The clock is ONE component in the shell header rather than a badge per panel,
    which is why it lives in ui.js: at 720x staleness is a property of the read,
    not of any single figure on the screen.
    """
    ui = (WEB / "js" / "ui.js").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")

    assert "s ago" in ui, "the reading's age has to be shown, not just its stamp"
    assert "STALE_AFTER_S" in ui, "there has to be a point at which it says so"
    assert ".clock.stale" in css, "and it has to look different once it is past it"

    # Stale is stated in DATA time, with the reason. Nine hours of simulated data
    # go past while the screen sits there for forty-five real seconds, and the age
    # that matters to a reader is the first number, not the second.
    assert "SIM_SPEED" in ui and "dataAge" in ui
    assert "stale-strip" in ui, "and the strip has to say what is consequently untrue"

    # One ticker, replaced rather than accumulated.
    assert "clearInterval" in ui, "a second clock would tick against the first"


def test_there_is_a_way_past_the_navigation():
    """Ten sidebar items sit between the top of the document and the content.

    Without a skip link a keyboard user tabs through the entire sidebar to reach the
    table they came for, and does it again on every view change. WCAG 2.4.1. The link
    must be focusable at all times - `display: none` would make it unreachable, which
    is the usual way this control gets broken - and the target needs tabindex="-1" or
    the fragment scrolls the page without moving focus.
    """
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")

    assert 'class="skip-link"' in app, "no skip link in the signed-in shell"
    assert 'href="#view"' in app
    assert 'id="view" tabindex="-1"' in app, (
        "the skip target must be focusable or the link only scrolls"
    )

    rule = re.search(r"\.skip-link\s*\{([^}]*)\}", css, re.S)
    assert rule, "the skip link needs a rule that positions it off-screen"
    assert "display: none" not in rule.group(1), (
        "a display:none element cannot receive focus, so the link would exist for nobody"
    )
    assert re.search(r"\.skip-link:focus\s*\{[^}]*left", css), (
        "it has to come back on screen when focused"
    )


def test_selection_checkboxes_say_what_they_select():
    """A hundred rows each announcing "Select this row" tells a screen-reader user
    that there is a checkbox and nothing about what ticking it would do. The name is
    visible in the row beside it, which is exactly the information the control has to
    carry itself."""
    ui = (WEB / "js" / "ui.js").read_text(encoding="utf-8")
    views = (WEB / "js" / "views.js").read_text(encoding="utf-8")

    assert "rowLabel" in ui, "dataTable must let the caller name each row"
    assert "rowLabel ? rowLabel(row)" in ui, "with a fallback when none is given"
    assert "rowLabel:" in views, (
        "the alert inbox is the selectable table; its rows must be named"
    )
