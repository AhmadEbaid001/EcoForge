# GEMP — master handoff

**This is the entry point.** It replaces the three separate chats the project was
built across, and it is the file a new session should be pointed at first.

Read in this order:

1. **This file** — where the project stands, what is left, how the pieces divide.
2. **`CLAUDE.md`** — the systems working context: phase status, decisions worth not
   relitigating, every trap that cost real time, and the findings that change the
   submitted paper.
3. **`HANDOFF-UI-UX.md`** — the interface: file layout, the constraints enforced by
   tests or by the browser, design tokens, the two appearances.

---

## 1. What this is

GEMP — RoboDam2026 competition entry, Smart Systems track. Team Ecoforge: Ahmed Ebaid
(systems), Nada Wagdy (environmental). **Deadline 31 August 2026.**

A **budget allocator**, not a monitoring dashboard. Given ~50 government buildings in
New Cairo and a fixed budget, it decides which buildings get which retrofit to
maximise life-cycle carbon benefit. Ingestion, forecasting, anomaly detection, the map
and the dashboards exist to feed that decision or to display it.

`C:\Users\Ahmad Ebaid\OneDrive\Desktop\gemp` — own git repo, **local only, no
remote**, branch `master`.

---

## 2. Where it stands

| Phase | State |
|---|---|
| 0 — domain + optimizer | done |
| 1 — infrastructure, ingestion, integrity | done |
| 2 — forecasting, anomaly detection | done |
| 3 — map UI, controls | done |
| 4 — evaluation harness, hardening, CI | done |
| — | identity, RBAC, in-app dashboards (added after Phase 4) |
| — | interface rebuild: sidebar shell, light/dark, accessibility |
| 5 — rehearsal, documentation | **not started** |

361 tests plus 15 integration tests that skip themselves without the stack, lint
clean, coverage floor 80%, all four gates passing.

Five containers: timescaledb, mosquitto, core, sim, nginx. Grafana was removed when
its dashboards moved into the application; its provisioning survives as an archive that
`tests/test_grafana_archive.py` checks and labels as one.

**After rebuilding and recreating `core`, restart nginx too.** It resolves the upstream
hostname once at startup, so a recreated API container leaves every request answering
502 while `docker ps` says healthy.

```bash
docker compose up -d
python scripts/check.py     # lint, tests+coverage, data contract, claims
```

Application at **http://localhost:8080**. There is no default account:

```bash
GEMP_DB_HOST=127.0.0.1 GEMP_DB_PORT=5433 python -m gemp.auth.bootstrap --username you --force
```

**Host-side scripts need `GEMP_DB_HOST=127.0.0.1 GEMP_DB_PORT=5433`.** Never
`localhost` — Windows resolves it to `::1` first and stalls 130 s per connection.

---

## 3. What is left

Ordered by what would cost most if it were still true on 31 August.

### Blocks submission

1. **The catalog is uncited.** `data/catalog.csv` — 0 of 6 rows carry a source;
   `data/params.yaml` — 6 more values marked `TODO(Nada): cite` (grid factor, tariff,
   end-use shares, condition multipliers, solar yield, solar price).
   `python -m gemp.domain.catalog --validate --strict` fails while any remain, and
   every number the platform reports derives from these. **Owner: Nada.** This is the
   one item code cannot close.

2. **Phase 5 has not started** — rehearsal and documentation, including the paper
   corrections listed in `CLAUDE.md` under "Findings that change the paper". The
   biggest of those: the old "+10% / +134% CP-SAT over greedy" headline must not be
   quoted; it measured a baseline that stops spending.

### Verification gaps

3. **CI has never executed.** No git remote, so `.github/workflows/ci.yml` has never
   run. `scripts/check.py` is what actually enforces the gates. Creating a remote and
   pushing once would validate the workflow. (Its two tests now live in
   `tests/test_ci_workflow.py` rather than inside the Grafana file.)

### Traceability

4. Nothing outstanding. F10 and F11 are labelled at the modules that resolve them -
   `gemp.ml.forecast` and `gemp.ml.features` for the Prophet rejection,
   `gemp.api.main`, `gemp.ingest.webhook` and `gemp.sim.node` for the monolith
   collapse, the dropped SMTP service and the WireGuard-as-designed-for argument. F11
   still carries the Phase 5 writing task: the paper has to make that last argument in
   prose, with the MQTT topic contract as its evidence.

### Recently closed, and why it is written down rather than deleted

The anomaly precision target, the Grafana ambiguity and the F10/F11 labels came off
this list on 18 August. One of them changed a paper claim rather than a line of code,
which is the part worth carrying forward.

**Anomaly precision is 0.827 at recall 0.875 (k=5)**, against gates of 0.6 and 0.8.
`F9-b` is a real gate in the claims harness now rather than a known-open `FAIL*`. The
threshold sweep's conclusion was right that tuning cannot reach 0.6 and wrong that 0.55
was a ceiling: the forecaster mispredicted hour 00, where Egyptian load steps into the
weekend or a public holiday while every lag feature says the building was busy an hour
ago. Predicting the ratio to a causal hour-of-week profile fixed it. Details in
`CLAUDE.md` under "Findings that change the paper", item 6.

**The three behaviours that had only ever been checked through the DOM have now been
seen.** In a displayed browser pane, signed in, on 18 August: the skip link appears
top-left on the first Tab and moves focus to `#view` when activated; real OpenStreetMap
footprints replace the proxy squares past `FOOTPRINT_ZOOM`, and they are visibly
building-shaped - an L, a cross - rather than boxes; and narrowing the chart's
container from 519 px to 320 px with no window `resize` event redrew the chart at the
new width, `viewBox` 519 to 320, axes and labels re-laid out rather than squashed. The
pane composites once it is DISPLAYED, which is the detail the earlier note was missing:
it is not that the pane cannot composite, it is that a hidden one does not.

**Two measurement defects fell out of that work and both were worse than the thing they
were hiding.** Forecasts could be negative, so the detector - which divides by the
expectation - scored an ordinary 1 kW reading at a robust z of 15,319. And the
simulator, whenever it could not reach the API at startup, silently rewound its data
clock by thirteen months and published nothing but duplicates while still recording the
faults it thought it was injecting: 1,762 of 2,212 ground-truth events described data
that was never stored. Anything written about F9 must come from a run after this.

---

## 4. How the work divided, and what that means now

Three chats built this, and they are all superseded by whichever session reads this:

- **`bb847b91-…jsonl`** (8.6 MB) — phases 0–3.
- **`65e8e371-…jsonl`** (7.0 MB) — phase 4, the hardening pass, the identity layer,
  and the UI defect fixes.
- **`e232bb60-…jsonl`** (7.5 MB) — the interface rebuild: the sidebar shell, light and
  dark, `ui.js`, the accessibility work.

They live in
`C:\Users\Ahmad Ebaid\.claude\projects\C--Users-Ahmad-Ebaid-OneDrive-Desktop-robodam-project\`.
23 MB in total — something to grep for a specific decision, never to load. Everything
that mattered from them is in this file, `CLAUDE.md` and `HANDOFF-UI-UX.md`, which is
why those three are kept current instead.

**Two sessions editing the same files at once caused real friction.** Uncommitted work
sat in the tree while another session committed around it, and one commit swept in
another session's changes under a message that did not describe them. If a second
session is ever run again, commit narrowly and say which files are yours.

---

## 5. The rules that will break the build

Stated in full in `HANDOFF-UI-UX.md` §4. The short version, because every one of them
has bitten:

- **Nothing loads from off-origin.** The CSP is `default-src 'self'`; an external
  script is *blocked by the browser*, and F13 requires the demo to survive an
  unplugged cable.
- **No inline script, no inline event handlers, and no `style="..."` attributes** —
  all three are discarded under the policy, silently.
- **No build step.** What is in `web/` is what nginx serves.
- Escape everything interpolated into HTML. Re-audited after the interface doubled in
  size; still clean.

---

## 6. House style

- **Comments explain reasoning, not mechanics** — why this way, what it cost, what
  breaks otherwise.
- **When a measurement contradicts an earlier claim, the claim gets corrected in
  writing** rather than quietly dropped. Several numbers in `CLAUDE.md` and the README
  are corrections of earlier ones, and they say so.
- **Commit messages state what changed and why it mattered**, including bugs found and
  what they would have cost.
- Verify against the running system, not against intent. Most of the defects found in
  Phase 4 were things that were implemented, tested, and never actually called.
