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

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

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

    The map listens on `window` for resize, pointermove, pointerup and pointercancel.
    Without an abort signal those survive every navigation away and back: five visits
    to the Map meant five resize handlers, each rebuilding the projection for a screen
    nobody was looking at. Measured in the browser before the fix - the count climbed
    1, 2, 3, 4; after it, it stays at 1.
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
    """A stamp states a moment without admitting the moment has passed. Nothing is
    polled - re-fetching on a timer during a demonstration moves numbers under
    whoever is talking about them - so the stamp ages instead, and says so once it
    is worth pressing Re-read.

    The clock is ONE component in the shell header rather than a badge per panel,
    which is why it lives in ui.js: staleness is a property of the read, not of
    any single figure on the screen.

    The age used to be reported in DATA time, multiplied by the simulator's 720x,
    because the clock ran away from the wall clock and forty-five real seconds
    genuinely hid nine hours of data. The clock is clamped now - it advances at
    real time once it has caught up - so that multiplier would overstate the drift
    by nearly three orders of magnitude, and the age is reported in real seconds.
    The words themselves come from the catalogue, so this asserts the mechanism
    rather than an English literal that only exists in one of two languages.
    """
    ui = (WEB / "js" / "ui.js").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")

    assert "readAge" in ui, "the reading's age has to be shown, not just its stamp"
    assert "clock.ago" in ui, "and the age has to be a translated string"
    assert "STALE_AFTER_S" in ui, "there has to be a point at which it says so"
    assert ".clock.stale" in css, "and it has to look different once it is past it"

    assert "SIM_SPEED" not in ui, (
        "the 720x multiplier was removed with the clamp; reinstating it would put an "
        "age on screen that is nearly three orders of magnitude too large"
    )
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
    # `inset-inline-start` rather than `left`: the link belongs at the edge where
    # reading starts, which is the right-hand one in Arabic.
    assert re.search(r"\.skip-link:focus\s*\{[^}]*(inset-inline-start|left)", css), (
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


def test_every_translation_key_the_ui_uses_exists_in_every_language():
    """A missing key does not fall back to English - it renders the key itself.

    `t()` returns the key when a string is absent, so an empty or partial table
    puts `nav.overview` in the navigation rail and `map.controls` on a heading.
    That is what shipped: an Arabic table and no English one, so the DEFAULT
    language rendered raw keys on every screen, and three keys map.js calls were
    in neither table. Nothing else notices - the ids exist, the paths resolve,
    the suite is green - because no other test reads what a control says.
    """
    source = (WEB / "js" / "i18n.js").read_text(encoding="utf-8")

    def table(lang: str) -> set[str]:
        block = re.search(lang + r":\s*\{(.*?)\n  \},", source, re.S)
        assert block, f"no {lang} table in i18n.js"
        return set(re.findall(r"'([^']+)':", block.group(1)))

    languages = {lang: table(lang) for lang in ("en", "ar")}

    used: set[str] = set()
    # Prefixes the UI builds a key from at run time - t(`sev.${severity}`) and
    # friends. Every catalogue key under one of these counts as used, because the
    # value that completes it comes from the API and cannot be read from source.
    dynamic: set[str] = set()

    for path in (WEB / "js").glob("*.js"):
        text = path.read_text(encoding="utf-8")
        # `t('key')` and `t('key', { ... })` alike. The namespace is deliberately
        # NOT filtered: this test used to know about four of them, which is why it
        # called every key of the other twenty "dead" the moment the interface
        # was translated past the demonstration path.
        used |= set(re.findall(r"[^a-zA-Z]t\('([^']+)'\s*[,)]", text))
        # `t(`sev.${k}`)` and `t(`range.d${days}`)` alike: the literal part
        # between the namespace and the hole is not always empty.
        dynamic |= set(re.findall(r"[^a-zA-Z]t\(`([a-zA-Z]+)\.[^`$]*\$\{", text))

    assert used, "no translation keys found - has t() been renamed?"

    for lang, keys in languages.items():
        missing = sorted(used - keys)
        assert not missing, (
            f"{lang} is missing {len(missing)} key(s) the UI calls, so they render "
            f"as raw keys: {missing}"
        )

    covered = used | {
        key for key in languages["en"]
        if key.split(".", 1)[0] in dynamic
    }
    dead = sorted(languages["en"] - covered)
    assert not dead, f"translated but never used: {dead}"


def test_a_line_breaks_on_missing_time_not_on_the_other_series():
    """Two series on one axis rarely share a sampling grid.

    The forecast panel draws readings the API has thinned to every 45 minutes
    against forecasts written every hour. The path builder used to walk the UNION of
    both series' timestamps and lift the pen wherever this series had no point -
    which is nearly every position when the grids differ. Measured on the live
    payload: the metered line came out as 225 subpaths, 113 of them single points,
    and the forecast line as 336 subpaths of ONE point each. A one-point subpath
    draws nothing, so the forecast was absent from a panel whose legend and data
    table both listed 336 values.

    A break has to mean missing time. The threshold is each series' own median step,
    so a series is drawn continuously at whatever rate it was sampled.
    """
    charts = (WEB / "js" / "charts.js").read_text(encoding="utf-8")

    assert "GAP_FACTOR" in charts, "no gap threshold: the line either joins holes or shatters"
    assert "own:" in charts, (
        "the path has to be built from each series' own points; walking the union is "
        "what shattered it"
    )
    body = charts[charts.index("const paths = resolved.map"):]
    body = body[:body.index("}).join('')")]
    assert "s.own" in body, "the path is still built from the union-indexed array"
    assert "point.t - previous.t > limit" in body, (
        "a break must be decided by elapsed time between this series' own points"
    )


def test_the_map_fetches_nothing_from_off_origin():
    """F13, and the one feature most likely to break it quietly.

    A satellite basemap is the obvious place for someone to reach for a tile
    server: one line, and the map looks better on the laptop of whoever wrote it.
    It would also be blocked by the Content-Security-Policy in front of a judge,
    and blank on a demonstration machine with the cable out.

    So the imagery is a file in this repository. Every URL the map asks for is
    relative, and the manifest names a local image.
    """
    import json
    import re

    source = (WEB / "js" / "map.js").read_text(encoding="utf-8")

    fetched = re.findall(r"""(?:fetch|href=)["'`]([^"'`$)]+)""", source)
    off_origin = [u for u in fetched if u.startswith(("http://", "https://", "//"))]
    assert not off_origin, f"the map reaches off-origin: {off_origin}"

    manifest_path = WEB / "data" / "basemap.json"
    if not manifest_path.exists():
        return  # a checkout that has not run scripts/fetch_basemap.py yet

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert not str(manifest["image"]).startswith(("http", "//")), (
        "the manifest points the map at a remote image"
    )
    assert (WEB / "data" / manifest["image"]).exists(), (
        f"the manifest names {manifest['image']}, which is not in web/data"
    )
    # CC BY 4.0 is a condition, not a suggestion: the credit has to be carried and
    # the map has to render it.
    assert manifest.get("attribution"), "imagery with no attribution recorded"
    assert manifest.get("licence"), "imagery with no licence recorded"
    assert "map-attribution" in source, "the map never renders the credit it owes"


def test_every_nginx_location_that_sets_a_header_sets_all_of_them():
    """nginx does not merge `add_header`: a location block that declares even one
    of its own discards the entire inherited set for that location.

    This file has already shipped the page carrying the whole application with no
    Content-Security-Policy at all, because a `location = /index.html` set nothing
    but Cache-Control. The basemap's caching exception is the second location to
    declare a header, and there will be a third.
    """
    import re

    conf = (ROOT / "infra" / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    required = {
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
    }

    for match in re.finditer(r"location[^{]*\{(.*?)\n    \}", conf, re.S):
        block = match.group(1)
        if "add_header" not in block:
            continue          # inherits the server-level set, which is correct
        missing = sorted(h for h in required if h not in block)
        assert not missing, (
            f"a location sets its own headers and so loses the inherited ones; "
            f"missing {missing} in: {match.group(0)[:80]}..."
        )


def test_only_the_basemap_escapes_no_store():
    """The whole application is served `no-store` on purpose - there is no build
    step and therefore no cache-busting, so a cached module is how somebody runs
    last week's front end against this week's API.

    Imagery is the one safe exception: a year-old photograph of Cairo has no
    interface with the API, and it is refreshed by hand rather than by a deploy.
    If a second exception appears, it should have to argue for itself here.
    """
    conf = (ROOT / "infra" / "nginx" / "nginx.conf").read_text(encoding="utf-8")

    cached = [line.strip() for line in conf.splitlines()
              if line.strip().startswith("add_header Cache-Control")
              and "no-store" not in line]
    assert len(cached) == 1, f"more than the basemap is cached now: {cached}"
    assert "max-age=86400" in cached[0], "the basemap cache window moved"


def test_a_pan_moves_the_ground_with_everything_on_it():
    """The map pans by moving one group's transform rather than redrawing 2,600
    paths on every pointer move. The satellite imagery is drawn OUTSIDE that group
    - it has to be, or the browser magnifies a raster it made once instead of
    sampling the file at the size it is shown at - so the fast path has to move it
    too.

    It did not, and the streets slid off the photograph underneath them on every
    drag. Both paths go through the same arithmetic now.
    """
    source = (WEB / "js" / "map.js").read_text(encoding="utf-8")

    assert "function groundRect()" in source, (
        "the placement arithmetic must live in one place, or the two callers drift"
    )

    transform = source[source.index("function applyTransform()"):]
    transform = transform[:transform.index("\n}") + 2]
    assert "placeGround()" in transform, (
        "a pan moves the group but not the imagery under it"
    )

    render = source[source.index("const ground = rect"):]
    render = render[:render.index(";")]
    assert "rect.x" in render and "rect.w" in render, (
        "render writes the image from something other than groundRect()"
    )


# ---------------------------------------------------------------------------
# The phone layout.
#
# Added on 2 September, when the product was carried to a phone for the first
# time and could not be used on one: the map answered a finger with a synthetic
# click and nothing else, the 5.25rem rail took a fifth of a 375px screen, and
# every control had been sized against a cursor. None of that is visible from a
# desktop, which is exactly why it survived to a week before submission - so
# each of these is the specific thing that was broken, written so it stays fixed.
# ---------------------------------------------------------------------------


def test_the_map_is_driven_by_pointers_rather_than_a_mouse():
    """`mousedown` reaches a phone as one synthetic click after the gesture is over.

    A map wired to it can be tapped and never dragged, which is what the map did:
    the screen this product is judged on was unusable on the device a judge is
    most likely to be holding. Pointer Events carry mouse, pen and touch through
    one set of handlers, so there is a single implementation of a pan rather than
    a mouse one and a touch one drifting apart.
    """
    source = (WEB / "js" / "map.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not COMMENT_LINE.match(line))

    for dead in ("'mousedown'", "'mousemove'", "'mouseup'"):
        assert dead not in code, f"the map still listens for {dead}"

    for live in ("'pointerdown'", "'pointermove'", "'pointerup'", "'pointercancel'"):
        assert live in code, f"the map does not handle {live}"


def test_a_cancelled_gesture_ends_the_drag():
    """`pointercancel` is not an edge case on a phone.

    An incoming call, the notification shade, or the browser deciding after a
    hundred milliseconds that the gesture was a scroll all end a touch this way
    and never send `pointerup`. Handled on its own, the map is left mid-drag with
    no finger behind it and the next tap teleports it across the city.
    """
    source = (WEB / "js" / "map.js").read_text(encoding="utf-8")
    attach = source[source.index("function attachPanZoom"):]
    attach = attach[:attach.index("\nfunction ", 10)]
    assert "pointercancel" in attach and "pointerup" in attach, (
        "the two have to be handled together, or a cancelled touch never ends the drag"
    )


def test_the_pinch_and_the_buttons_share_one_zoom_clamp():
    """Four ways to zoom - the buttons, the keys, the wheel, two fingers - and one
    ladder for all of them.

    The floor used to be a bare `0.4` inside zoomAbout while the ceiling was a
    named constant with a paragraph explaining it, so half the clamp was findable
    and half was not. A pinch that reached a different ceiling than the + key
    would be invisible until somebody in the room pinched to the end of it.
    """
    source = (WEB / "js" / "map.js").read_text(encoding="utf-8")

    assert "const MIN_ZOOM" in source and "const MAX_ZOOM" in source, (
        "both ends of the clamp have to be named, or the second caller writes its own"
    )
    assert "function scaleAbout(" in source, (
        "the pinch needs the arithmetic without the render, or it re-renders 2,600 "
        "paths per frame of a gesture a finger drives"
    )

    # zoomAbout is the version that renders; it must delegate rather than repeat.
    zoom = source[source.index("function zoomAbout("):]
    zoom = zoom[:zoom.index("\n}") + 2]
    assert "scaleAbout(" in zoom, "zoomAbout has its own copy of the arithmetic again"
    assert "Math.min" not in zoom, "zoomAbout is clamping separately from scaleAbout"


def test_the_map_claims_the_touch_gestures_it_needs():
    """Without `touch-action: none` the browser takes a drag on the map for a page
    scroll and sends `pointercancel` in the middle of it, so the pan handler sees
    the start of every gesture and the end of none."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    block = css[css.index("#map {"):]
    block = block[:block.index("}") + 1]
    assert "touch-action: none" in block, "#map does not claim its own gestures"


def test_the_map_stage_is_capped_on_a_phone():
    """The other half of `touch-action: none`, and the reason it is safe.

    An element that swallows every vertical swipe and fills the screen is a
    scroll trap: a reader who lands a finger on it cannot reach the rest of the
    page. The stage is capped to a fraction of the viewport on a phone precisely
    so there is always page around it to swipe from, and `svh` rather than `vh`
    because `vh` is measured with the browser's toolbars hidden.
    """
    css = (WEB / "style.css").read_text(encoding="utf-8")
    phone = css[css.index("@media (max-width: 768px)"):]
    assert "svh" in phone, (
        "the map stage is not capped against the viewport a phone actually shows"
    )


def test_every_hover_rule_is_gated_behind_a_hovering_pointer():
    """A touch browser leaves the last thing tapped in `:hover` until something
    else is tapped.

    None of these rules hides anything - they are a background, a colour, a
    border or an opacity - but left ungated they leave a table row lit as though
    it were selected when it is not, and the navigation item you came FROM
    looking like the one you are on.
    """
    css = (WEB / "style.css").read_text(encoding="utf-8")
    # Prose about hover is not a hover rule, and this file explains itself at
    # length. Comments are blanked rather than dropped so the line numbers in the
    # message below still point at the line somebody has to open.
    css = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)

    ungated = []
    guard_depth = None
    depth = 0
    for number, line in enumerate(css.splitlines(), 1):
        # `@media (hover: hover) { … }` on one line opens and closes together.
        gated_here = "hover: hover" in line
        if ":hover" in line and not gated_here and guard_depth is None:
            ungated.append(f"style.css:{number}: {line.strip()[:80]}")
        if gated_here and line.rstrip().endswith("{"):
            guard_depth = depth
        depth += line.count("{") - line.count("}")
        if guard_depth is not None and depth <= guard_depth:
            guard_depth = None

    assert not ungated, (
        "these hover states will stick to whatever a phone reader last tapped:\n"
        + "\n".join(ungated)
    )


def test_a_phone_reader_can_still_zoom_the_page():
    """`user-scalable=no` and a `maximum-scale` are the two ways a viewport meta
    takes page zoom away from somebody who needs it. WCAG 1.4.4, and the first
    thing a low-vision reader reaches for on a phone."""
    head = (WEB / "index.html").read_text(encoding="utf-8")
    viewport = [line for line in head.splitlines() if "name=\"viewport\"" in line]
    assert viewport, "the page declares no viewport at all"
    joined = " ".join(viewport)
    assert "user-scalable=no" not in joined, "the page forbids pinch-zooming itself"
    assert "maximum-scale" not in joined, "the page caps how far it can be zoomed"


def test_the_bottom_bar_leaves_room_for_the_page_under_it():
    """Below 768px the rail is a fixed bar across the foot of the screen, so it
    floats over the content. Without a matching pad on `main` it covers the last
    row of every table and the last control of every form - and the one screen
    where that is invisible is the one a developer is looking at."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    phone = css[css.index("@media (max-width: 768px)"):]
    phone = phone[:phone.index("@media (max-width: 480px)")]

    assert "--railbar-h" in phone, "the bar's height is not named, so nothing can reserve it"
    main_rule = phone[phone.index("main {"):]
    main_rule = main_rule[:main_rule.index("}")]
    assert "padding-bottom" in main_rule and "--railbar-h" in main_rule, (
        "the page does not reserve the height of the bar that sits over it"
    )
    assert "safe-area-inset-bottom" in main_rule, (
        "an edge-to-edge viewport puts the home indicator under the bar as well"
    )


def test_every_two_column_grid_collapses_on_a_narrow_window():
    """A variant that adds a class keeps its own columns however narrow the window.

    `.two-col` is (0,1,0) and `.two-col.aside-first` is (0,2,0), so a collapse
    rule naming only the first never reached the second - and a media query adds
    no weight to a selector. Measured on the running application at 390px: the
    grid computed to `358px 0px`, the second column was zero wide, and the
    Integrity and Account screens pushed their content off the right of the
    document. Neither shows it at a desktop width, which is why it survived.

    So the collapse rule has to name every variant that exists. This finds them
    from the stylesheet rather than from a list somebody has to remember.
    """
    css = (WEB / "style.css").read_text(encoding="utf-8")
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    # Every selector that gives a .two-col something more than one column.
    variants = set()
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css_no_comments):
        if "grid-template-columns" not in body:
            continue
        for part in selector.split(","):
            part = part.strip()
            if part.startswith(".two-col"):
                tracks = re.search(r"grid-template-columns:([^;]+)", body).group(1)
                # A single `1fr` or `minmax(0, 1fr)` IS the collapsed form.
                if tracks.count("fr") > 1 or "rem)" in tracks:
                    variants.add(part)

    assert variants, "no .two-col grid found; has the class been renamed?"

    narrow = css_no_comments[css_no_comments.index("@media (max-width: 1200px)"):]
    narrow = narrow[:narrow.index("}", narrow.index("}") + 1) + 1]

    missing = sorted(v for v in variants if v not in narrow)
    assert not missing, (
        "these two-column grids never collapse, so they overflow a phone:\n  "
        + "\n  ".join(missing)
    )


def test_every_table_the_ui_writes_can_scroll_inside_its_own_box():
    """A table is the one element that cannot be made narrower than its content.

    Six columns of figures come to about 560px. Dropped into a dialog with no
    scroller of its own, that width is handed to the dialog: on a 390px screen
    the Compare sheet scrolled sideways, carrying its own heading and its close
    button off the edge. Every other table in the product sits in a wrapper that
    scrolls; this checks that the next one does too.
    """
    wrappers = ("table-wrap", "opt-table-wrap", "card-table", "chart-table-wrap")

    bare = []
    for path in sorted(WEB.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<table[\s>]", source):
            # The wrapper is opened just before it, possibly across a line break.
            before = source[max(0, match.start() - 220):match.start()]
            if not any(w in before for w in wrappers):
                line = source.count("\n", 0, match.start()) + 1
                bare.append(f"{path.name}:{line}")

    assert not bare, (
        "these tables have no scroller, so their width becomes their container's:\n  "
        + "\n  ".join(bare)
    )

