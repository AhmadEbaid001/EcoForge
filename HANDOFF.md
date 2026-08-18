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

353 tests plus 15 integration tests that skip themselves without the stack, lint
clean, coverage floor 80%, all four gates passing.

Five containers: timescaledb, mosquitto, core, sim, nginx. Grafana was removed when
its dashboards moved into the application.

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

### Measured, not met

3. **Anomaly precision is 0.55 at recall 0.81**, against a 0.6 target — and tuning
   cannot close it. 64 combinations of threshold, episode duration and peak-z were
   swept; duration buys precision at the same exchange rate as the threshold, and a
   peak-z floor makes precision *worse*. Closing it needs a better expected-load
   model — `ml/features.py`, not `anomaly_k`. Documented as a negative result with
   the evidence, which is a legitimate thing to submit.

### Verification gaps

4. **CI has never executed.** No git remote, so `.github/workflows/ci.yml` has never
   run. `scripts/check.py` is what actually enforces the gates. Creating a remote and
   pushing once would validate the workflow.

5. **Several things are verified by DOM inspection, not by eye.** The browser pane
   used for automation does not composite, which means `requestAnimationFrame`,
   `ResizeObserver` and `:focus` never fire in it. Confirmed working by other means
   but never *seen*: map footprints past zoom 3, the skip link appearing on focus, and
   the ResizeObserver path of the chart redraw. Each is a few seconds to confirm in a
   real window.

### Traceability

6. **F10 and F11 are resolved but unlabelled.** Prophet was rejected for HGBR +
   seasonal-naive; WireGuard and SMTP were cut and the architecture collapsed to what
   the review prescribed. Every other finding is labelled at the module implementing
   it, so the paper has no evidence trail for these two. F11 also carries a Phase 5
   writing task: describe WireGuard and SMTP as *designed-for*, with the MQTT topic
   contract as the evidence.

7. **Grafana provisioning is retained but not deployed**, and `test_provisioning.py`
   still validates it. Either say plainly that it is kept as Phase 3 evidence, or drop
   it — a passing test currently implies something is running that is not.

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
