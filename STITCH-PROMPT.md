# Google Stitch prompts for GEMP

Stitch does one screen well at a time. Paste **Block A** at the top of every prompt,
then append **one** screen block from Block C. Block B is optional — use it when the
first result drifts off-brief.

Real numbers from the running system are included on purpose: they stop Stitch from
filling the mockup with lorem ipsum and three-row tables, which is what makes generated
dashboards look convincing and useless.

---

## Block A — base context (paste every time)

```
Design a desktop web screen for GEMP (Green Energy Monitoring Platform), an internal
decision tool used by government energy analysts and reviewed by competition judges.

WHAT IT DOES — this framing matters more than anything else:
GEMP is a BUDGET ALLOCATOR, not a monitoring dashboard. Given about 50 government
buildings in New Cairo and a fixed budget in Egyptian pounds, it decides which
buildings should receive which energy retrofit (LED lighting, HVAC controls, roof
insulation, double glazing, HVAC replacement, rooftop solar) to maximise life-cycle
carbon benefit. Metering, forecasting and anomaly detection exist only to feed that
decision. Every screen should feel like it is serving one question: where should the
money go, and can the recommendation be trusted?

AUDIENCE AND CONTEXT:
Analysts using it daily on a 1440x900 desktop, and judges seeing it once on a
projector in a bright room. It must read at a glance from three metres away and still
reward close reading. No mobile design needed; make it degrade gracefully to 1024px.

VISUAL DIRECTION:
Swiss / functional minimalism. Dense, calm, information-first. Strong typographic
hierarchy doing the work instead of decoration. Generous alignment discipline, tight
vertical rhythm. Think Linear, Vercel dashboard, Bloomberg Terminal restraint — not
consumer SaaS marketing. No hero images, no illustrations, no mascots, no decorative
gradients, no glassmorphism, no rounded-everything. Corner radius 8-10px maximum.

COLOUR — use these exact values, they are contrast-tested:
Light: background #EEF1F6, surface #FFFFFF, inset #F3F5F9, hairline #D6DCE6,
  control border #7F8A9A, text #0F151C, secondary text #4D5A6B, tertiary #5F6D7E,
  accent #0B62C4, success #127A4E, warning #8A5A06, danger #C03A25.
Dark: background #0D1117, surface #151B23, elevated #1C232D, inset #10151B,
  hairline #2B3644, control border #627287, text #E8EEF5, secondary #A9B8C9,
  tertiary #8494A6, accent #58A6FF, success #43B581, warning #E0A94D, danger #F4795B.
Show the screen in BOTH light and dark. Dark is not an inversion: grounds get dimmer
while accents get brighter.

TYPOGRAPHY:
System UI sans only (SF Pro / Segoe UI / Inter as a stand-in). One family. Tabular
lining numerals for every figure in a table or a metric tile. Scale: 11px uppercase
micro-labels at 600 weight, 12px captions, 13px controls, 14px body, 15px panel
headings, 24px page titles, 24-28px metric figures at 650 weight with -2% tracking.

ICONS:
Stroke icons only, 1.6px, single colour, 16-20px. No emoji, no filled multicolour
icons, no icon fonts. Use them sparingly — this interface should be legible with every
icon removed.

CONSTRAINTS THAT ARE NOT NEGOTIABLE:
- Never convey meaning by colour alone. Severity needs a shape or a label too.
- Every interactive control at least 30px tall with a visible focus ring.
- Body text contrast at least 4.5:1; control borders at least 3:1.
- No element may depend on hover to be discoverable.
- Charts are hand-drawn SVG in the real product: line, stacked bar and single
  horizontal proportion bar only. No donuts, no pie charts, no 3D, no gauges, no
  gradient fills under lines.
- Tables must be shown at realistic density — at least 12 visible rows, not 3.
```

---

## Block B — corrective additions (use if the first pass drifts)

```
Corrections:
- This is an internal tool, not a landing page. Remove any hero section, marketing
  copy, testimonial, pricing, or call-to-action button that is not a real action.
- Reduce whitespace by about a third and increase information density.
- Remove card shadows except a 1px hairline border and a barely-visible drop shadow.
- Numbers are the loudest thing on the page. Labels are quiet.
- Do not invent metrics. Use only the fields listed for this screen.
```

---

## Block C — the screens

### 1. Allocation map (the primary screen — design this one first)

```
SCREEN: Allocation map. This is the screen the whole product is judged on.

Three-column layout, full viewport height below the app header.

LEFT COLUMN, 260px — allocation controls:
- "Budget" — a large tabular figure reading "10,000,000 EGP" above a range slider,
  min 1M, max 30M, with 1M / 15M / 30M tick labels beneath.
- "Rank by" select: Life-cycle carbon (kgCO2e) / First-year energy (kWh) /
  First-year money (EGP).
- "Allocation method" select: Exact optimization (CP-SAT) / Greedy + upgrade pass /
  Greedy heuristic (saturates) / Equal split — status quo. There are exactly FOUR
  methods; never write "three".
- "Max funded per district" select: No cap / 2 / 4 / 6.
- Two buttons: "Compare all four methods" (recommended action, accent-tinted) and
  "Why building-specific?" (quiet).
- A legend: green square "Funded by this allocation", grey square "Not funded",
  red ring "Open anomaly", and the note "Size reflects annual consumption".

CENTRE — the map:
A schematic SVG map of about 50 building footprints across 5 named districts of New
Cairo. Buildings are squares sized by annual consumption at low zoom and become real
building outlines when zoomed in. Funded buildings are green AND carry a lighter
outline; unfunded are muted district-tinted grey. Buildings with an open anomaly get a
thin red ring. District names as small uppercase tracked labels.
Overlaid across the top of the map, a row of five compact translucent metric chips:
"31 buildings funded", "94% of budget spent", "1.2M kWh/yr saved",
"8.4M kgCO2e lifetime", "142 ms solve time".
Bottom-right: a small vertical zoom control — plus, minus, and "Fit".
Bottom-left: a one-line hint about scroll to zoom, drag to pan, click for options.

RIGHT COLUMN, 320px — the selected building:
Building name, then code and district. A two-column grid of small facts: Use,
Insulation, HVAC age, Roof m2, Floor m2, Consumption. Then "OPTIONS CONSIDERED (6)"
as a micro-label, followed by six compact option cards, each with the measure name,
a row of cost EGP / kWh saved / benefit per thousand EGP, and one card marked
"chosen by this allocation" with a green border and tint. One card is dimmed and
annotated "net negative".

Show the empty state of the right column too: "Select a building", with one sentence
explaining that every option the optimizer considered will be listed there.
```

### 2. Alert inbox (the densest screen — design this second)

```
SCREEN: Alert inbox. Triage of about 31,000 open anomalies, 100 shown at a time.

Page header: title "Alert inbox", one-line description "Readings that deviate from
what the forecaster expected, worst first", and on the right a pill reading
"Data clock 2026-03-14 08:20" next to a "Refresh" button.

Filter toolbar: severity select, a type-to-filter building picker, an "Open only"
checkbox, and a quiet "Close a whole group…" button.

A selection action bar that appears ONLY when rows are checked: "4 selected", a
primary "Acknowledge 4 selected" button, and a quiet "Clear selection".

The table, at least 14 visible rows: checkbox column, Building (b012), When in data
time (monospace 2026-03-14 06:00), Observed kW, Expected kW, Deviation z, Severity,
and a per-row "Acknowledge" button. Numeric columns right-aligned with tabular
figures. Column headers are sort buttons with an ascending/descending arrow; the
active one is accented.

Severity is a pill with a leading dot AND a word: critical (red), high (amber),
medium (neutral grey). Never colour alone.

Under the table, two quiet lines: "Showing the 100 largest of 31.6k open alerts." and
"Sorting applies to the rows shown, not to the whole inbox."

Also design these three states of the same screen:
(a) a success banner at the top reading "Closed 34 alerts." with an "Undo" button and
    a dismiss X;
(b) the empty state — "Nothing open here", with a sentence explaining the inbox is
    clear;
(c) the loading state as grey skeleton rows the same height as real rows.
```

### 3. Portfolio overview

```
SCREEN: Portfolio overview — the landing screen after sign-in.

Page header "Portfolio overview", description "What the platform is measuring, and the
allocation it last recommended", data clock pill and Refresh on the right.

A row of four metric tiles, each with a large tabular figure, a quiet label, and a
2px accent hairline along the top edge:
"50" Buildings · "863.4k" Readings stored · "31.6k" Open alerts with the sub-line
"critical 11204 · high 9781 · medium 10581" · "50/50" Costed on measurement with the
sub-line "F3: buildings whose annual kWh comes from the forecast".

Then three panels stacked full width:
1. "Portfolio demand" — a single-series line chart of about 336 hourly points, y-axis
   in kW with round tick values, and a caption explaining that the simulator runs at
   720x so data time runs ahead of the wall clock.
2. "Alerts per day" — a stacked bar chart, 30 bars, three severities, with a text
   legend above it. The three fills must differ in lightness, not only hue.
3. "Most recent allocation" — four compact tiles: Budget 10M EGP, Funded 31 buildings,
   Spent 9.4M EGP, Lifetime benefit 8.4M kgCO2e, with a caption naming the solver,
   the objective and the timestamp.
```

### 4. Method comparison modal

```
SCREEN: A modal dialog over the map, titled "Same portfolio, same budget, four
methods". Width about 760px, elevated surface, dimmed and blurred backdrop, close X
at the top right.

A table with one row per method — Equal split (status quo), Greedy heuristic,
Greedy + upgrade pass, Exact optimization — and columns: Funded, Spent EGP,
Lifetime kgCO2e, vs status quo (as +163.4% etc., with "baseline" on the first row),
Time (ms). The winning row is tinted green with bolder figures.

Beneath it, a short explanatory paragraph in caption style — this product explains its
own numbers rather than asserting them. Design the paragraph as a real block of two to
four lines, not a single sentence.
```

### 5. Sign-in

```
SCREEN: Sign-in. A single centred card about 420px wide on a plain ground with a very
subtle radial wash behind it. Inside: a small 30px rounded-square gradient mark beside
"Green Energy Monitoring Platform" and the subtitle "Budget-constrained retrofit
prioritization". Username and password fields with visible labels above them, a
full-width primary "Sign in" button, and a quiet caption reading "Accounts are created
by an administrator. There is no self-service registration and no default account."
At the bottom of the card, a three-way appearance switch: Auto / Light / Dark as a
small segmented control of stroke icons.
Also design the error state, with a red-bordered message above the fields.
```

### 6. Administration

```
SCREEN: Administration, for admin accounts only.

Page header "Administration" with a primary "Add account" button on the right.

Panel 1 "Security posture" — a definition list: Session cookie Secure flag,
Idle timeout 8 hours, Absolute session lifetime 7 days, Minimum password length 12,
Accounts 4. The Secure flag row shows a red pill reading "OFF — required on any
networked deployment". Below it a small table of recent failed logins with timestamp,
username tried, and IP address.

Panel 2 "Accounts" — a table: Username, Name, Role as an inline select, Status as a
coloured dot plus the word active/disabled with an optional "must change password"
badge, Last login, and two quiet row buttons "Disable" and "Reset password".

Panel 3 "Audit log" — a six-column sortable table with 12 visible rows: When, User,
Action in monospace, Target, Outcome, Address. Rows with outcome "denied" show it as a
red pill.

Also design the confirmation dialog for a role change: title "Change the role of
nada?", a sentence explaining what the new role grants, and a red "Make nada admin"
button beside a quiet Cancel.
```

### 7. Reading integrity

```
SCREEN: Reading integrity — the tamper-evidence screen.

Page header "Reading integrity", description "Whether the stored measurements are the
ones that were signed on arrival."

One panel: a building picker and a "Verify again" button in the header, then a large
pill verdict reading "Intact" in green with a leading dot, then a definition list —
Rows checked 51.4k, Chain walk "verifies", External anchor "matches (sequence 51402)",
Anchor vs database copy "agree" — then a caption explaining that each reading is signed
and chained to the one before it.

Design the failure state as a second frame: a red "Broken" verdict, a red error block
reading "First break: hash mismatch at sequence 20418", and a neutral hint block
telling the reader that a break is evidence rather than an error to dismiss.
```

### 8. Stored allocations

```
SCREEN: Stored allocations. Page header with title, the description "Every
recommendation the optimizer has produced, and the inputs it used", and a Refresh
button.

One panel, "Runs", with "25 stored" as a quiet count in the header, and a sortable
table of at least 12 rows: When, Budget EGP, Objective, Method, Funded, Spent EGP,
Lifetime kgCO2e, and an "Inputs" column showing a truncated hash like "a3f9c21b04…"
with a dotted underline indicating the full value is available on hover.
Caption beneath explaining that each run records the hash of the catalog, parameters
and consumption that produced it, so a stored recommendation can be reproduced.

Also design the empty state: "No allocations stored yet", with a sentence pointing the
reader at the Map tab.
```

### 9. App shell (design once, reuse in every screen above)

```
SCREEN: The application header, sticky at the top, about 60px tall, on a translucent
blurred surface with a hairline bottom border.

Left: a 30px rounded-square gradient mark (green to blue) beside "Green Energy
Monitoring Platform" with "Team Ecoforge" as a quiet subtitle.
Centre: seven navigation tabs in ONE enclosed segmented control — Overview, Map,
Forecasts, Alerts, Allocations, Integrity, Administration. The active tab is a raised
surface with a border and heavier weight, not merely a different colour.
Right, in order: a status pill with a coloured dot reading "connected"; a three-way
appearance switch (Auto / Light / Dark) as stroke icons in a small segmented control;
a user button reading "ahmed" with a role badge "admin"; and a quiet "Sign out".

Show the header wrapping gracefully at 1024px width.
```

---

## Using the output

Stitch gives you a design and its own code. **Take the design, not the code** — the
real product has constraints Stitch does not model: no build step, no external
resources of any kind, and a Content-Security-Policy that blocks inline styles and any
off-origin script or font. Anything Stitch emits with a CDN link or a `style="..."`
attribute will be discarded by the browser, silently.

What is worth lifting from a Stitch result:
- spacing rhythm and the ratio between panel padding and gap
- the metric-tile and table-row proportions
- where it puts the primary action on each screen
- any place it found a clearer hierarchy than the current build

Bring those back as changes to `web/style.css` and the view templates.
