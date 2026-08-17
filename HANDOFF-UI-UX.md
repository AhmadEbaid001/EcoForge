# Handoff — UI/UX work on GEMP

**Read this first, then `CLAUDE.md`.** This file carries everything a fresh session
needs to work on the interface without re-deriving it or breaking what already works.
It is a distillation, not a transcript: the verbatim history is far too large for a
context window (see the last section for where it lives).

---

## 1. What the system is

GEMP — RoboDam2026 competition entry, Team Ecoforge (Ahmed Ebaid, systems; Nada
Wagdy, environmental). **Deadline 31 August 2026.**

A **budget allocator**, not a monitoring dashboard. Given ~50 government buildings in
New Cairo and a fixed budget, it decides which buildings get which retrofit to
maximise life-cycle carbon benefit. Ingestion, forecasting, anomaly detection and the
map exist to feed that decision or to display it.

Repository: `C:\Users\Ahmad Ebaid\OneDrive\Desktop\gemp` — own git repo, **local only,
no remote**. Branch `master`.

State: phases 0–4 complete, plus an identity layer (accounts, roles, in-app
dashboards) added afterwards. 344 tests + 15 integration tests, lint clean, all gates
passing, working tree clean.

---

## 2. Running it

The stack is five containers and is probably already running.

```bash
docker compose up -d
```

Application: **http://localhost:8080** — this is the whole product now. API docs at
`/docs`. Grafana was removed; its panels are views inside the application.

Login: there is one admin account, `ahmed`. **The password is not written down in the
repository on purpose** — ask Ahmed, or create your own account:

```bash
GEMP_DB_HOST=127.0.0.1 GEMP_DB_PORT=5433 python -m gemp.auth.bootstrap --username you --force
```

That prints a generated password once and forces a change at first login.

**Host-side scripts need `GEMP_DB_HOST=127.0.0.1 GEMP_DB_PORT=5433`.** Never
`localhost` — Windows resolves it to `::1` first and each connection stalls for 130
seconds. It presents as "the database is slow", not as an error.

---

## 3. The UI codebase

No build step, no bundler, no package.json. The files nginx serves are the files in
the repository, so what is reviewed is what runs.

```
web/
  index.html        Empty shell. Renders nothing until the app asks the server who
                    the caller is, so an unauthenticated browser gets no markup
                    describing data it may not see.
  style.css         ~940 lines, in 13 numbered sections. Tokens first (light, then
                    dark twice - see §5), then base, chrome, components, charts,
                    map, responsive. One component per section; no appended block.
  js/
    app.js          Shell: login form, password-change gate, tab navigation,
                    hash routing, session-expiry handling, and the appearance
                    switch (auto / light / dark). ~330 lines.
    api.js          The single way in and out of the API. Attaches the CSRF token,
                    turns 401 into a login screen, normalises errors. ~120 lines.
    charts.js       Hand-drawn inline SVG: lineChart, stackedBars, proportionBar,
                    statTile, the three appearance icons, escapeHtml, compact.
                    ~190 lines.
    views.js        Seven views: overview, forecasts, alerts, runs, integrity,
                    admin, account, plus openDialog() - the form that replaced
                    window.prompt for minting credentials. ~730 lines.
    map.js          The map, moved wholesale from the old single-page app. Owns its
                    own markup (MAP_HTML / MODAL_HTML template strings at the top).
                    ~600 lines. Treat as working code to be respected, not rewritten.
```

Navigation order: Overview, Map, Forecasts, Alerts, Allocations, Integrity,
Administration — plus an Account view reachable from the user button.

---

## 4. Constraints that will break the build if violated

These are not stylistic preferences. Each one is enforced by a test or by the browser.

1. **No external resources of any kind.** No CDN, no Google Fonts, no icon library,
   no chart library. F13 requires the demonstration to survive an unplugged network
   cable, and the server sends a Content-Security-Policy of `default-src 'self'` — an
   external `<script>` would be *blocked by the browser*, not merely unwise.
   `tests/test_web.py::test_no_external_resource_references` fails on any off-origin
   URL outside a comment.

2. **No inline scripts or inline event handlers.** The CSP has no `'unsafe-inline'`.
   `onclick="..."` in markup will silently not fire. Attach listeners in JavaScript.

2b. **No `style="..."` attributes either, for the same reason.** The policy is
   `style-src 'self'` with no `'unsafe-inline'`, and that governs style *attributes*
   as well as `<style>` elements. Three of them had been sitting in `map.js` since the
   dashboards were built, doing nothing: the narrative modal's cards never had the
   side-by-side layout their markup asked for. Nothing reports this — the attribute is
   simply discarded. Every visual decision has to be reachable from `style.css`
   through a class.

3. **No build step.** `tests/test_web.py::test_no_bundler_or_package_manifest` fails
   if `package.json`, `node_modules`, `webpack.config.js` or `vite.config.js` appears
   under `web/`.

4. **Every id a script reaches for must exist** in some HTML or JS template.
   Enforced by `test_every_element_the_script_reaches_for_exists_in_the_page`.

5. **Every API path the UI calls must be a route the server serves.** Enforced by
   `test_api_paths_used_by_the_ui_are_the_ones_the_server_serves`.

6. **`.modal[hidden] { display: none; }` must stay.** `.modal` sets `display: flex`,
   and an author rule beats the browser's own `[hidden]` rule — without the override
   both modals render on page load and the map opens behind a full-screen overlay
   reading "Solving…". This shipped once. Pinned by
   `test_hidden_modals_are_actually_hidden`.

7. **`MAX_ZOOM` must be at least 4× `FOOTPRINT_ZOOM`.** Real footprints switch in at
   zoom 3, where the median outline is 6.4 screen pixels; at the old ceiling of 8 it
   was still 10.4. Pinned by `test_the_zoom_ceiling_lets_a_footprint_be_read`.

8. **Never write "three methods" or "all three".** There are four solvers. Pinned by
   `test_the_ui_does_not_claim_three_solvers`.

9. **Escape everything interpolated into HTML.** `escapeHtml` from `charts.js`. The
   current code is clean — an audit found no unescaped interpolation anywhere — and it
   should stay that way.

---

## 5. Design tokens, and the two appearances

**This section replaces the dark-only token list that was here.** The interface now
supports light and dark, and the old names survive only as aliases.

Tokens are semantic — named for the job, not the colour — and defined in section 1 of
`style.css`:

```css
/* grounds */    --bg  --surface  --surface-2  --surface-3  --surface-hover
/* lines */      --line  --line-strong  --control-line
/* text */       --ink  --ink-2  --ink-3
/* meaning */    --accent --accent-hover --accent-soft --on-accent
                 --success --warn --danger, each with -soft and -ink variants
/* data */       --sev-critical --sev-high --sev-medium --series-0 --series-1
/* map */        --funded --funded-edge --unfunded --anomaly
                 --district-s --district-l --bldg-edge --map-ground
/* depth */      --shadow-1 --shadow-2 --shadow-3 --scrim --header-bg
/* type */       --fs-micro … --fs-figure-lg, in rem
/* space */      --s-1 … --s-10 on a 4px grid, --radius*, --ease, --dur
```

`--panel`, `--panel-2`, `--ink-dim` and `--funded-lo` are kept as **aliases** of the
new names. `map.js` draws `fill="var(--funded)"` into an SVG presentation attribute
and this document used to name the old set, so removing them would have broken the
screen that gets demonstrated most for no gain.

**Each colour token is written three times.** Once for light on bare `:root`, once
under `@media (prefers-color-scheme: dark)` guarded by `:not([data-theme="light"])`,
and once under `:root[data-theme="dark"]`. That is not redundancy to tidy up: a token
defined *only* inside a media query does not exist for anyone outside that query, and
without the third block an explicit dark choice on a light system does nothing.

Appearance is chosen in the header — **Auto / Light / Dark**, persisted in
`localStorage` under `gemp.appearance`. Auto means *no* `data-theme` attribute, which
leaves `prefers-color-scheme` in charge. That ordering is deliberate: the CSP forbids
inline script, so nothing can run before first paint, and a design whose correct
appearance depended on JavaScript would flash the wrong one on every load. Following
the system by default means the common case is right before `app.js` executes.

Every text pair was measured rather than eyeballed. On `--surface`: `--ink` is 18.4:1
light and 14.8:1 dark, `--ink-2` is 7.0 and 8.6, `--ink-3` is 5.3 and 5.6. Accent and
the three status colours clear 4.7:1 on both `--bg` and `--surface` in both
appearances. `--control-line` exists because a separator may be faint but the boundary
of an input may not — it measures 3.5:1 on white, which is what WCAG 1.4.11 asks of a
control edge.

Where colour carried meaning on its own, something else now carries it too: the second
chart series is **dashed** as well as amber, a funded building has a lighter **outline**
as well as a green fill, and each severity pill has a **dot**. Severity is ordinal, so
its three fills separate by lightness (adjacent stack segments differ by 3.08:1 and
1.55:1) rather than by hue alone.

---

## 6. Roles, and what each sees

Ladder: `viewer` < `analyst` < `admin`.

| Role | May |
|---|---|
| viewer | read the portfolio, map, stored runs, metrics, alerts, integrity |
| analyst | + run the optimizer, acknowledge alerts |
| admin | + manage users, recompute candidates, read the audit log and security posture |

Hiding a control is presentation only — the server refuses the call regardless, and a
hidden button has never stopped anyone who can open a terminal. But a control that is
*shown* and then fails is a bug (see issue 1 below).

Session behaviour worth knowing for UI work: 8-hour idle timeout, 7-day absolute
lifetime, and any request may discover the session has ended — `api.js` routes that to
the login screen.

---

## 7. The work: outstanding UI/UX issues

Audited live against the running stack. Ordered by severity. Items marked **CLOSED**
were fixed in the light/dark redesign pass; the rest are still open and still real.

### Closed by the redesign pass

- **Two class names were claimed by two components each.** `.controls` was both the
  map's left column and a panel's toolbar; `.legend` was both the map key and a chart
  key. The later block in the stylesheet won, so the map's sidebar and its legend were
  both being laid out as wrapping flex **rows** — on the most demonstrated screen in
  the system. `.facts` and `.caption` collided the same way with milder effects. Now
  `.map-controls` / `.toolbar` and `.map-legend` / `.chart-legend`, and `.facts` is
  scoped to `.detail` where the map needs its two-column form.
- **Three `style="..."` attributes in `map.js` were being discarded by the CSP** (see
  §4.2b). Replaced with `.section-label`, `.card` and `.card-row`.
- **No focus indicator anywhere except the login inputs**, which made every dashboard
  unusable from the keyboard. One `:focus-visible` ring now covers the application.
- **`window.prompt` for minting credentials** — issue 3 below. Replaced with a real
  dialog: labelled fields a password manager can fill, a confirmation field, length
  checked before the request leaves, Escape and scrim to dismiss, and a
  `crypto.getRandomValues` suggestion button.
- **The blocking `window.alert`** — issue 5 below. The bulk acknowledge now explains
  in place that it closes every alert of that severity, says how many that is, and
  needs a second click.
- **The alert list's silent cap** — issue 4. The footer now reads "showing the 100
  largest of 31.6k open alerts". Real pagination still needs an `offset` on
  `/dashboard/anomalies`, which the endpoint does not have.
- **Sign-out left the hash pointing at the last view** — issue 8. Cleared with
  `history.replaceState`.
- **`onPasswordChanged` was a no-op** — issue 10. It re-reads the session.
- **No `aria-label` on any control** — issue 9. Selects, alert filters, per-row admin
  buttons and the appearance switch are labelled; the active tab carries
  `aria-current="page"`, which is also what styles it, so the visual state and the
  announced state cannot drift.
- **Control sizes.** The small ghost buttons were 24px against a 28pt recommended
  desktop default. Everything interactive is now at least 28px, most 30px.
- **Text was sized in px throughout**, so the browser's own text-size setting could not
  scale it. The type scale is in rem.

### Serious

1. **A viewer opening the Map gets a broken screen.** Confirmed with a real viewer
   account: the health indicator turns red reading *"this action requires the analyst
   role"*, all five summary metrics show `—`, and the budget slider and Compare button
   are rendered but 403 on every use. Cause: `map.js` `main()` calls `solve()` on
   mount, which POSTs `/optimize` (analyst-only). The map should render read-only for
   a viewer — load the geojson, hide the solve controls, show the most recent stored
   allocation instead.

2. **Listener leak on every Map visit.** `wire()` and `attachPanZoom()` attach
   `window` listeners for resize, mousemove and mouseup, and `main()` runs on every
   mount. Five visits to the Map tab means five resize handlers, each rebuilding
   geometry. Views need a teardown hook, or those listeners belong in the shell.

3. ~~**Admin user creation uses `window.prompt` for passwords.**~~ **CLOSED** — see
   `openDialog()` in `views.js`.

### Moderate

4. **The alert list caps at 100** with ~31,000 open. The count is now stated, so the
   cap is no longer silent, but there is still no pagination — and there cannot be
   until `/dashboard/anomalies` takes an `offset`. Acknowledging rows refills the list
   from the pool.

5. ~~**"Acknowledge all shown" does not do what it says.**~~ **CLOSED** — it is now
   "Acknowledge by severity…", it states the scope and the count, and it confirms in
   place rather than through `window.alert`.

6. **Charts do not redraw on resize.** Written once as SVG strings with a fixed
   `viewBox="0 0 760 240"`. They scale, but tick density and label spacing are
   computed for 760 px, so labels are sparse on a wide monitor and crowded on a narrow
   one. Only the map redraws. Fixing this properly needs the teardown hook issue 2
   also wants, so the two belong in one pass.

7. **Nothing refreshes.** No polling anywhere. Data time advances at 720×, so the
   Overview a judge is looking at goes stale within a minute and nothing says so.
   Either poll the summary or timestamp the panels with "as of".

8. ~~**Sign-out leaves the hash pointing at the last view.**~~ **CLOSED.**

### Minor

9. ~~**No `aria-label` on any interactive control.**~~ **CLOSED.**

10. ~~**`onPasswordChanged` is a no-op.**~~ **CLOSED.**

---

## 8. What must not regress

The map is the most demonstrated screen in the system. Its projection, rendering,
pan/zoom and footprint switching work and have been verified in a browser. Improve
its surroundings freely; change its internals only with a reason.

Verified working, live, at the time of this handoff:

- signed out shows only the login form and leaks nothing about the portfolio
- 50 building polygons render; real OSM footprints appear past zoom 3
- both forecast charts, 100 alerts, 25 stored allocations
- integrity reads **Intact** over 51,400 rows with the external anchor matching
- acknowledging an alert closes it and it leaves the open list

---

## 9. House style

- **Comments explain reasoning, not mechanics.** Why this way, what it cost, what
  breaks otherwise. A comment that restates the code is noise.
- **When a measurement contradicts an earlier claim, the claim gets corrected in
  writing** rather than quietly dropped.
- **Commit messages state what changed and why it mattered**, including bugs found and
  what they would have cost. Not a changelog of files touched.
- Match the surrounding code: no framework, no classes where a function will do,
  template strings for markup, and `escapeHtml` on everything interpolated.

---

## 10. Verifying a change

```bash
python -m pytest tests/test_web.py -q     # the UI contract gates
python scripts/check.py                    # lint, all tests with coverage, data contract, claims
```

`scripts/check.py` is what actually enforces CI — the repository has no remote, so the
committed GitHub workflow has never run.

For live checking, the in-app browser can drive the page: sign in, navigate by setting
`location.hash` and dispatching a `hashchange` event, then read the DOM. Screenshots
require the Browser pane to be displayed on the user's side.

---

## 11. Where the rest of the context lives

- **`CLAUDE.md`** — the working context: phase status, decisions worth not
  relitigating, every trap that cost real time, and the findings that change the
  submitted paper. Read it.
- **`README.md`** — architecture, headline numbers, open findings.
- **`data/README.md`** — the data contract between the two team members.
- **Verbatim transcripts** —
  `C:\Users\Ahmad Ebaid\.claude\projects\C--Users-Ahmad-Ebaid-OneDrive-Desktop-robodam-project\`
  holds two session logs: `bb847b91-…jsonl` (phases 0–3) and `65e8e371-…jsonl`
  (phase 4, hardening, and the identity layer). They are 8.6 MB and 5.6 MB, so they
  are a reference to grep, not something to load.

This chat is intended to run **alongside** the main RoboDam session, not to replace
it: UI/UX work here, systems and paper work there.
