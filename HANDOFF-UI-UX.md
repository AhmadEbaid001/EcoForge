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
  style.css         ~1100 lines, in 14 numbered sections. Tokens first (light, then
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
    ui.js           The furniture every view needs and each was doing
                    differently: pageHead, freshness, setStatus, the skeletons,
                    emptyState, dataTable + wireSort, picker, openDialog and
                    confirmAction. ~330 lines. Read this before adding a view.
    views.js        Seven views: overview, forecasts, alerts, runs, integrity,
                    admin, account. Each is now composition over ui.js rather
                    than its own table and its own spinner. ~990 lines.
    map.js          The map, moved wholesale from the old single-page app. Owns its
                    own markup (MAP_HTML / MODAL_HTML template strings at the top).
                    ~700 lines. Projection and rendering are working code to be
                    respected; its markup and its zoom controls are not sacred.
```

Navigation is a **fixed left sidebar**, not top tabs: Overview, Map, Forecasts, Alerts,
Allocations, Integrity, Administration, with Account, Sign out, the health indicator
and the appearance switch pinned to its foot. It collapses to a 60px icon rail — by
choice (the toggle in the workspace bar, remembered in `localStorage` under
`gemp.nav`), automatically below 1024px, and it becomes a horizontal strip below 700px.
The workspace bar above the content names the current view, so a collapsed rail never
leaves the screen unlabelled.

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

## 5a. The Stitch reference set, and what was taken from it

`STITCH-PROMPT.md` produced ten reference screens (archive on the Desktop). They are a
**source of ideas, not a target to match** — the ten screens are not one product:

- **Four different navigation systems** across ten screens: top tabs
  (Dashboard/Analytics/Scenarios/Reports), a 240px labelled sidebar (Allocation Map /
  Alert Inbox / Reports / Support / Logs), a 56px icon rail, and a hybrid.
- **Three product names**: GEMP, "EnergyInsight", "Grid Control · District Alpha".
- **The accent flips hue between modes** — blue in light, green in dark.
- **Invented domain data throughout**: ROI in years, EUI in kWh/m², anomaly *types*
  ("HVAC Surge", "Phantom Load"), severity "Low", the solver named "Mixed Integer
  Linear Program", buildings called "Engineering Block D". GEMP has none of these.
- **One map screen renders Google Maps tiles of Gurugram, India** — an off-origin
  fetch, in the wrong country, on the screen whose entire premise is that it works
  with the cable unplugged.
- **The login screen states security properties that are not true**: "AES-256-GCM",
  "FIPS 140-3", a node identifier, "AUDIT LOG ACTIVE", a version number, and a
  "Request Emergency Override Access" link. That was not implemented and must not be:
  a compliance claim in front of judges has to be one the system can support.

What **was** adopted, because it is genuinely better than what was here:

| Taken | Why |
|---|---|
| Sidebar navigation | Seven tabs already wrapped the header at 1180px, and left nowhere for a page title |
| Bordered fact grid in the building detail | Hairline-separated cells stay dense and still parse; the old two-column text block did not |
| Rank badge on every option card | Makes the ranking arguable — you can see when the chosen option is *not* rank 1, which is what a district cap does |
| Zebra striping on tables | Scanning a wide row without losing the line |
| Tonal layering over shadows | Depth from surface + hairline, which is what already worked in dark mode |

The rest of `DESIGN.md` in the archive is a reasonable restatement of the system
already in §5 — with the exception of its Inter-from-Google-Fonts requirement, which
constraint §4.1 forbids. The system font stack stays.

## 5c. Two rules that are load-bearing and look like nothing

**Every row in the sidebar shares one left edge and one icon column.** `.sidebar`
declares `--row-pad` and `--icon-col`; the brand, the seven destinations, the health
indicator and the account button all inherit them. Measured in the browser, every row
box starts at x=8, every glyph centres on x=28, and every piece of text starts at x=52
— one value per column, which is the whole point. Two things had broken that:

- `.nav-item` is a `<button>`, and the base `button` rule in §4 centres its contents.
  A nav row therefore centred its own icon and label inside its own width, so the icons
  landed at *five different x positions* depending on how long each label happened to
  be. `justify-content: flex-start` on `.nav-item` is what fixes it. Anything else in
  this file that is a button but should read as a row needs the same.
- A `border: 1px solid transparent` on `.nav-item` pushed every icon and label one
  pixel past the brand mark, which has none. The focus ring is an `outline`, so the
  border was doing nothing but that.

**Every control that opens a list goes through `selectWrap()`.** A bare `<select>` is
drawn by the operating system — its arrow, its height and its font are Windows's
decision, which is why they did not match the inputs beside them and why the arrow
stayed dark on a dark page. `appearance: none` takes the drawing back and the wrapper
supplies a chevron, because a pseudo-element cannot hang off a `<select>`.

The chevron is a rotated square, **not** a data-URI SVG. An inline SVG needs an
`xmlns="http://www.w3.org/2000/svg"` attribute, and `test_no_external_resource_references`
reads that as an off-origin reference — correctly, since it cannot tell a namespace
from a fetch. Base64-encoding it to dodge the test would have hidden the string from
the check that exists to catch exactly this.

The popup list itself cannot be styled by any page. What it *can* be told is which
appearance to draw, which is what `color-scheme` on the root does; without it the open
list stays white over a dark page.

## 5d. The map's context layer

The map used to draw fifty shapes on an empty field. The geometry was right and
nobody could read it: there was nothing around a building to place it against, which
is the first thing anyone asks of a map.

`scripts/fetch_osm_context.py` fetches the same New Cairo bounding box that
`fetch_osm_buildings.py` uses and keeps what that script throws away — the road
network and every other building footprint. It writes `web/data/context.geojson`
(~970 kB: 1,695 roads, 890 buildings, 14 water/parks), which nginx serves from this
origin like any other file under `web/`.

**Run it by hand, once.** Nothing fetches at runtime. A tile from a map provider would
be an off-origin request: blocked by the CSP and dead on a demonstration machine with
the cable out (F13). Baking the surroundings into the repository keeps the context and
the offline guarantee. If the file is missing the map degrades to exactly what it drew
before — `loadContext()` swallows the failure on purpose.

Three things about it are load-bearing:

- **It is projected with the portfolio's own `toScreen`**, built inside
  `buildGeometry()`. Two layers on two projections would drift apart at zoom.
- **Panning moves the group's `transform`; it does not re-render.** The context is
  ~2,600 paths, and rebuilding that markup on every pointer move made the map
  unusable to drag. `applyTransform()` is the whole pan update; zoom still goes
  through `render()`, because the zoom band decides what is drawn. Measured: 6 ms to
  pan, ~20 ms per zoom step.
- **Detail is gated by zoom band** (`#map[data-zoom]`, set in `render()`). At the
  opening view the minor streets are a grey wash that hides the roads anyone would
  navigate by, and the neighbouring footprints are specks — neither is drawn until it
  means something. Verified: far = 0 minor roads and 0 context buildings, mid and
  close = 1,384 and 890, portfolio switching from squares to real footprints at the
  same threshold.

Roads use `vector-effect: non-scaling-stroke`, so a street stays one pixel wide at
every zoom instead of growing into a slab as the group scales.

The scale bar is a fixed 80 px with a measured label, not a round number with a
stretched bar. The usual way round would mean writing a width from JavaScript, and an
inline style is discarded under `style-src 'self'`.

## 5b. How a view is built

Every view opens with the same three things, from `ui.js`. Follow the pattern rather
than inventing a fourth way to show a spinner.

```js
root.innerHTML = `${pageHead({ title, description, meta: freshness(clock) })}
                  ${skeletonRows(8)}`;      // shaped like what replaces it
…
setStatus(root, { kind: 'ok', message: 'Closed 34 alerts.', actionLabel: 'Undo', … });
```

- **`pageHead`** — an `h2` title and a sentence saying what the screen is for. `h1` is
  the product name in the header, so a panel heading is an `h3`. Skipping a level
  makes the heading outline lie to a screen reader.
- **`freshness(dataClock)`** — the data clock plus a Refresh button. Nothing polls (§7
  item 7), and at 720× a screen left open for a minute is showing half a day of stale
  numbers. Stating when the numbers were read is the honest version of that; a live
  badge over a stopped clock is not.
- **`setStatus`** — the result of an action, **inline at the top of the view**, not a
  floating toast. A fixed toast can cover the control that holds keyboard focus, and
  one that expires on a timer is unreadable for anyone who needs longer than it lasts.
  It stays until replaced or dismissed.
- **Skeletons, not a spinner line.** The old `<div class="loading">` was one line tall
  and everything below it moved when the data arrived.
- **`dataTable` + `wireSort`** for every table. Sorting is client-side over the rows
  already loaded and the footer says so — the API returns a capped slice, and sorting
  100 of 31,000 rows while calling them "the worst" would be a lie the interface tells
  on its own.
- **`openDialog` traps focus and returns it** to whatever opened it. `confirmAction` is
  the same dialog with no fields, for changes that are not obviously reversible.

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
- **Zoom was the mouse wheel and nothing else**, which left the map unreachable from a
  keyboard. There are now +/−/Fit buttons, and the map itself is focusable with arrow
  keys to pan and `+` `-` `0` to zoom.
- **Both map modals opened with focus left behind them**, so Tab walked the page under
  the scrim and Escape did nothing. Focus moves into the dialog and returns to the
  button that opened it. Escape is handled on the modal, not on `document`, so it adds
  no listener to leak.
- **A role change fired on `change` with no confirmation and no visible result.** It
  now says what the role means, asks, and reverts the select if the answer is no.
- **Every table and every "no data" case had its own markup.** One `dataTable`, one
  `emptyState`, all sortable, all with the row count stated.

A note for whoever picks this up: the `ui-ux-pro-max` skill's generated design system
recommends a Fira Code / Fira Sans pairing **loaded from Google Fonts**. That was
rejected, not overlooked — see §4.1. Its useful contributions were the density and
motion dials (dashboard density, subtle 200–250 ms transitions), the Swiss/minimal
direction, and the rule that telemetry may only be labelled live when it is backed by a
current source with an update time and a stale state, which is what `freshness()` is.

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

4. **The alert list caps at 100** with ~31,000 open. Mostly addressed: the count is
   stated, the list can be filtered by building as well as severity, rows can be
   multi-selected and closed in one call, and every close offers a real undo
   (`acknowledge` takes `acknowledged: false`). What is still missing is **paging** —
   and it cannot be added until `/dashboard/anomalies` takes an `offset`. That is a
   one-parameter API change and it is the last thing standing between this screen and
   a workable triage queue.

5. ~~**"Acknowledge all shown" does not do what it says.**~~ **CLOSED** — it is now
   "Acknowledge by severity…", it states the scope and the count, and it confirms in
   place rather than through `window.alert`.

6. **Charts do not redraw on resize.** Written once as SVG strings with a fixed
   `viewBox="0 0 760 240"`. They scale, but tick density and label spacing are
   computed for 760 px, so labels are sparse on a wide monitor and crowded on a narrow
   one. Only the map redraws. Fixing this properly needs the teardown hook issue 2
   also wants, so the two belong in one pass.

7. **Nothing refreshes on its own.** Still true, and still worth doing. Half-addressed:
   every view now states the data clock its numbers were read at and offers a Refresh
   button, so a stale screen says it is stale instead of pretending. Automatic polling
   of the summary is the remaining half.

8. ~~**Sign-out leaves the hash pointing at the last view.**~~ **CLOSED.**

### Minor

9. ~~**No `aria-label` on any interactive control.**~~ **CLOSED.**

10. ~~**`onPasswordChanged` is a no-op.**~~ **CLOSED.**

---

## 7a. The charts, after the interaction pass

`charts.js` no longer returns a picture. Every chart is a host with three parts and
only the first is ever redrawn:

```
.chart-host
  .chart-plot     the SVG - replaced on every resize
  .chart-tip      the readout - positioned from --tip-x, never rebuilt
  details.chart-table   the same numbers, collapsed
```

Listeners bind to the host once and read `host.__meta`, which the latest draw left
behind. Binding to the SVG would mean re-attaching after every resize, and a missed
re-attach is a chart that silently stops responding at one particular window width.

What a reader can now do, on every chart:

- **Point at it.** A crosshair snaps to the nearest reading and one readout lists
  every series at that moment - the pointer never has to land on a line.
- **Focus it and use the arrow keys.** Same values, same readout. Shift steps ten at
  a time, Home and End jump to the ends, Escape clears. The SVG carries the
  `tabindex`, so the chart itself is the stop rather than a wrapper around it.
- **Open the table.** A `<details>` under each chart with the same numbers, thinned
  to every nth row past sixty and labelled as thinned. No script, keyboard operable
  and announced for free.

The Overview carries **one filter row above everything it scopes** - 7 / 14 / 30 / 90
days - and both charts, the sparkline and the trend move together. A refetch dims the
frame it already has rather than dropping a skeleton in, so nothing jumps under the
reader who just asked the question. The window lives on the view object, so leaving
the screen and coming back does not reset it.

### The severity palette is not separable by hue, and the charts work around it

Run through a colourblindness check, in the light appearance:

| pair | deutan ΔE | normal ΔE |
|---|---|---|
| `--sev-critical` vs `--sev-high` | **0.6** | 26 |
| `--sev-high` vs `--sev-medium` | 11 | **13.0** |

A deutan ΔE of 0.6 means critical and high are, to roughly one man in twenty, **the
same colour** - and the alerts chart stacks them on each other. 13 in normal vision is
below the 15 at which two marks can be told apart at a glance by anyone.

The real repair is re-stepping the three tokens, and that is **not a chart's decision
to make**: those colours are also the alert badges, the map markers and the severity
chips, so they change everywhere or nowhere. Until someone does that, the charts stop
relying on hue alone - each band carries a hatch as well as a colour, at 45°, 135° and
90°, painted in `--surface` so it reads as the surface showing through the mark. The
legend keys carry the same textures, or the legend would be the one place a reader
still had to separate the bands by hue.

Two related things were fixed in passing. The proportion bar was painting a
categorical question - *which forecaster is in use* - in the severity ramp, so a
portfolio entirely on the current model drew a full-width bar in critical red with
"100%" on it: an alarm about good news. It draws from `--series-*` now. And the
readout puts the value first and heavier with the label after it, which is the
legend's hierarchy inverted, because at that point the reader has the series and wants
the number.

### Still open here

- **The three severity tokens should be re-stepped** so they pass without texture.
  Texture is the workaround, not the fix.
- **The forecast overlay is often a single line.** `/dashboard/forecast` returns
  actual and forecast for the last N hours, but the data clock advances at 720x while
  the refit runs on a wall-clock schedule, so within a few hours of wall time the
  window contains readings and no forecast. The chart is right; what it is drawing has
  aged out. Re-running `python -m gemp.ml.jobs` refills it.

---

## 8. What must not regress

The map is the most demonstrated screen in the system. Its projection, rendering,
pan/zoom and footprint switching work and have been verified in a browser. Improve
its surroundings freely; change its internals only with a reason.

Verified working, live, at the time of this handoff:

- signed out shows only the login form and leaks nothing about the portfolio
- 50 building polygons render; real OSM footprints appear past zoom 3
- both forecast charts, 100 alerts, 25 stored allocations
- every chart answers the pointer AND the arrow keys, and carries a table of the same
  numbers; the Overview's window control moves both charts and the sparkline together
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
