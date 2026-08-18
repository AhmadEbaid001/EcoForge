# GEMP — Green Energy Monitoring Platform

Budget-constrained retrofit decision support for public-sector building portfolios.
RoboDam2026, Smart Systems track. Team Ecoforge — Ahmed Ebaid, Nada Wagdy.

**This is not a monitoring dashboard.** It is a budget allocator. Given a portfolio of
government buildings and a fixed budget, it decides which buildings get which retrofit,
so as to maximise life-cycle benefit. Ingestion, forecasting, anomaly detection and the
map exist to feed that decision or to display it.

---

## Quick start

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

```bash
python scripts/fetch_osm_buildings.py
```

```bash
python -m gemp.optimize.cli --budget 10000000 --compare
```

No database, no broker, no containers required for any of the above — the optimizer
runs from CSV fixtures alone. For the full stack (map, ingestion, dashboards):

```bash
docker compose up -d && python -m gemp.seed --months 6
```

## What works today (Phases 0–3 complete)

| Command | What it does |
|---|---|
| `python -m gemp.domain.catalog --validate` | Check the data contract. Add `--strict` to fail on uncited catalog rows. |
| `python scripts/fetch_osm_buildings.py` | Build the portfolio from **real** OpenStreetMap footprints (New Cairo). Needs network. |
| `python scripts/gen_buildings.py` | Offline fallback: synthetic square footprints, same schema. It produces a *different portfolio* from the versioned one, so the published numbers will not reproduce against it. |
| `python -m gemp.optimize.cli --budget 10000000` | Solve and print an allocation. |
| `python -m gemp.optimize.cli --budget 10000000 --compare` | Every solver on the same instance. |
| `python -m gemp.optimize.cli --budget 10000000 --compare --district-cap 2` | The instance where the exact solver decisively beats every heuristic. |
| `python -m gemp.sim.replay --stats-only` | Report what the real UCI meter dataset looks like against a building. |
| `python -m gemp.sim.replay --building b001` | Replay that real dataset onto a building over MQTT. Needs the broker. |
| `python -m gemp.evaluate` | Re-measure every claim below. Exits 1 if one has stopped holding. |
| `python -m gemp.evaluate --with-db` | Adds the forecasting and anomaly claims. Needs the stack. |
| `python scripts/check.py` | Every CI gate, locally: lint, tests with coverage, data contract, claims. |
| `python -m gemp.auth.bootstrap --username you` | Create the first administrator. There is no default account. |
| `python -m pytest` | 344 tests; 15 integration tests skip themselves without the stack. |
| `curl localhost:8080/api/v1/integrity/verify/b001` | Walk one building's chain and check it against the external anchor. |

### Current headline numbers

50 real OSM footprints, New Cairo, 32.7 GWh/yr, budget 10,000,000 EGP:

| Solver | Funded | Spent | Life-cycle kgCO₂e | vs equal split |
|---|---|---|---|---|
| equal_split | 30 | 5,117,794 | 12,526,901 | baseline |
| greedy | 34 | 9,966,764 | 29,923,997 | +139 % |
| greedy_upgrade | 34 | 9,998,550 | 29,965,474 | +139 % |
| cpsat | 24 | 9,999,207 | 32,916,590 | **+163 %** |

With a two-per-district cap, plain greedy spends only 3.55 M of the 10 M: it commits
its district slots to cheap high-density options and then cannot use the rest.

| Solver | Funded | Spent | Life-cycle kgCO₂e | vs equal split |
|---|---|---|---|---|
| equal_split | 9 | 1,644,764 | 5,261,983 | baseline |
| greedy | 10 | 3,553,479 | 13,411,548 | +155 % |
| greedy_upgrade | 10 | 9,984,278 | 29,214,174 | +455 % |
| cpsat | 10 | 9,986,292 | 31,368,000 | **+496 %** |

**The headline is quoted against `greedy_upgrade`, never against plain greedy.** Plain
greedy never revisits a funded building, so once every building holds its cheapest
dense option — about 16 M EGP here — it stops spending altogether. Its benefit is then
flat while the budget grows, and a comparison against it measures that ceiling rather
than the value of exact optimization: the gap reaches +127 % at 40 M, by which point
even equal split is beating it. `greedy_upgrade` adds the obvious repair, spending
what is left on the best available swap, and is the baseline CP-SAT has to beat.

Over 40 budget/objective instances from 2 M to 40 M EGP, measured by
`python -m gemp.evaluate`: **CP-SAT wins by a median of 1.4 % unconstrained and 20.1 %
under a district cap of 2.** The unconstrained advantage is not flat — it peaks at
11.5 % around 14 M EGP, where the budget binds hardest and the choice of which
building to skip actually matters, then decays toward zero as the budget grows enough
to fund everything worth funding. The capped advantage does the opposite, climbing to
35 % at 40 M. The unconstrained gap is small because a good heuristic
is near-optimal on a plain knapsack — that is reported rather than hidden. The cap is
where exact optimization earns its place, and it is also the realistic case: no
ministry funds nine buildings in one district and none in the next.

Dominance pruning removes 60 % of the candidate set (1367 → 540) before solving.
Provably safe — any solution using a dominated option can be rewritten to use its
dominator — and `test_pruning_does_not_change_any_solver_result` checks that across
every solver × objective × budget combination.

---

## Architecture

Modular monolith. The module boundaries match the logical architecture of the
proposal's Figure 1; the deployment is a single process plus infrastructure
containers, which is roughly two days cheaper to build and operate than six
independently deployed services.

```
src/gemp/
  domain/      physics and economics. No database, no MQTT, no web framework.
    models.py      Building, Intervention, Candidate, Allocation
    catalog.py     loads + validates data/, enforces the team contract
    savings.py     F3: meter reading -> per-intervention kWh
    lifecycle.py   F2: common-horizon carbon and cost
    candidates.py  F4: bundle expansion, multiplicative composition
  optimize/    the core deliverable
    ilp.py         CP-SAT, absolute objective, district caps
    baselines.py   greedy and equal-split
    prune.py       dominance pruning, objective-specific, provably result-preserving
    runner.py      one interface, three solvers, one return type
  ingest/      Phase 1: MQTT -> hash-chained storage
  ml/          Phase 2: forecasting and anomaly detection
  api/  web/   Phase 3: FastAPI + inline-SVG map (no mapping library — see CLAUDE.md)
```

`domain/` importing nothing infrastructural is what lets the optimizer run against CSV
fixtures with no containers. That is deliberate: if the pipeline slips, the artifact
the project is judged on still runs.

## The data contract

Two files carry the entire interface between the two team members. **Nada edits values
and never code; Ahmed edits code and never values.** See [`data/README.md`](data/README.md).

| File | Owner |
|---|---|
| `data/catalog.csv` | Nada — cost, saving fraction, service life, embodied carbon, citations |
| `data/params.yaml` | Nada — grid factor, horizon, end-use shares, condition multipliers, solar physics |

```bash
python -m gemp.domain.catalog --validate --strict
```

Every value currently ships as a **placeholder** marked `TODO(Nada): cite`. `--strict`
fails while any remain, and gates submission.

---

## Design decisions worth knowing

These correct specific defects identified in the technical review of the proposal.
Each is documented at the top of the module that implements it.

**The optimizer maximises absolute benefit, not benefit density.** Summing
benefit-per-EGP under a budget constraint is degenerate — densities with different
denominators do not add, and the objective value carries no unit. Density survives as
the greedy heuristic's sort key. → `optimize/ilp.py`

**All interventions are compared over a common 30-year horizon.** Crediting each
option over its own service life makes a 12-year lamp and a 30-year insulation job
incomparable. Replacement embodied carbon is charged; replacement *capital* is not,
because a ministry allocating this year's money only pays for one installation now.
→ `domain/lifecycle.py`

**Savings on a shared end use compose multiplicatively.** Insulation cuts cooling load,
an HVAC swap raises COP, and both act on `E_hvac = Load / COP`. Naive addition
overstates a fabric-plus-plant bundle by 20–40 %. Compatible combinations are
pre-expanded into their own candidates, so the solver needs no interaction terms at
all. → `domain/candidates.py`

**A meter reading is not a saving estimate.** Getting from one aggregate number to
"this HVAC replacement saves X kWh" needs an end-use share table and a condition
multiplier, both of which live in `params.yaml`. → `domain/savings.py`

**All money is EGP integers everywhere.** The division by 1000 that produces "per
thousand EGP" happens only at the display layer, and a property test pins it.

**Part of the demonstration runs on real measured data, not only on a generator we
wrote.** `sim/replay.py` replays the UCI *Individual Household Electric Power
Consumption* dataset — 2,075,259 minute-resolution readings from one French household,
2006–2010 — onto a building over the same MQTT topic the simulator uses, so nothing
downstream can tell the two apart. Amplitude is rescaled to the target building's mean
load and shape is not: a household draws a few kW where a government building draws
hundreds, and replaying raw values would put consumption two orders of magnitude below
its profile and wreck every saving estimate derived from it. What the dataset is
actually here for — the texture of real demand, its spikes, plateaux and missing runs
— survives the rescaling. The 133 MB file is not versioned; `--stats-only` reports the
fit without publishing anything. → `sim/replay.py`

**Alerting is a row, a marker, and an optional webhook.** No SMTP service and no
pager: an anomaly is a row in the `anomaly` table, a red marker on the map, and — if
`GEMP_WEBHOOK_URL` is set — a JSON POST to that endpoint. The webhook posts from a
worker thread behind a bounded queue and drops rather than buffers when the endpoint
cannot keep up, because the ingester is a single-threaded loop and a slow notification
target must never be able to stall the write path. Alerts are acknowledgeable in bulk
and reversibly, which is what stops the open count from being a number that only ever
grows. → `ingest/webhook.py`

**Tamper evidence needs an anchor outside the database, and the anchor has to be
read.** Each reading is HMAC-signed and chained to its predecessor, so a modified row
breaks the walk at exactly that row. A *deleted tail* breaks nothing — there is
nothing after it left to check — so chain heads are also written to a file on a
separate mount, and verification compares the two. The database's own checkpoint table
is read as well, not as evidence but for comparison: anyone who can truncate `reading`
can truncate that table too, and a disagreement between file and table is itself a
signal. → `ingest/anchor.py`, `services.verify_building_chain`

---

## Open finding: the LCA layer currently changes no decisions

At every budget from 2 M to 40 M EGP, the `lca_carbon` and `raw_kwh` objectives fund
**the same buildings with the same interventions**. Embodied carbon is a median 8 % of
gross avoided emissions across the candidate set, and the options that actually get
funded are the ones where it is smallest. At an Egyptian grid factor of 0.45 kgCO₂e/kWh,
embodied carbon is simply too small to reverse a retrofit ranking.

The LCA layer does earn its place in one respect: it correctly refuses to fund at least
one glazing option whose manufacture emits more than it avoids over thirty years, which
a raw-kWh ranking would treat as a benefit.

This is pinned by `test_lca_adjustment_barely_changes_the_funded_set`. If a time-of-use
marginal emission factor is added — making an HVAC kWh saved at a summer afternoon peak
worth more carbon than a lighting kWh saved in the evening — that test should start
failing, and the failure is the signal that the LCA layer has begun to matter.

Stated more usefully as what the difference is worth: choosing funding by raw kWh
instead of life-cycle carbon costs at most **0.02 %** of the carbon a carbon-optimal
allocation would deliver, across 20 budgets.

## Closed finding: the anomaly precision target, and what it took to reach it

Episode-level detection reads **precision 0.83 at recall 0.88** (k = 5), against gates
of 0.6 and 0.8 in the technical review. Both are met. The route there is worth more
than the number, because for most of the project this section said the target was
unreachable, and the reasoning that led to that conclusion was half sound.

**The sound half.** `k` moves a point along a single precision/recall curve. To find
out whether the curve itself could be moved, 64 combinations of `k`, minimum episode
duration and minimum peak z were measured against injected ground truth. Requiring
longer episodes buys precision at about the same exchange rate as raising `k`.
Requiring a higher peak z makes precision *worse*, moving it from 0.607 to 0.535,
which is informative rather than merely disappointing: a frozen meter barely deviates
from its expectation at all, while the largest residuals are legitimate load that the
forecaster failed to anticipate. Residual magnitude does not separate real faults from
forecast misses. All of that still holds.

**The unsound half** was treating 0.55 as a property of the problem. It was a property
of the forecaster, and the forecaster had one systematic failure doing most of the
damage. Egyptian building load STEPS at midnight — into the Friday–Saturday weekend,
into a public holiday, out of one — and every lag feature says the building was busy an
hour ago. Relative residual dispersion measured 0.125 at hour 00 against 0.06 for the
rest of the day, and 640 of 1,379 false alarms began at hour 00, on Fridays and public
holidays. The detector scales residuals against a window pooled across all hours, so a
systematically worse hour breaches the threshold on ordinary days.

The fix is one idea: predict the **ratio** to a causal hour-of-week profile rather than
the load itself. A tree adds leaf values, so in kW a holiday is a per-building,
per-hour constant learned from a handful of examples; in ratio space it is "0.3×", one
split that holds everywhere. Held-out MAPE moved 3.83 % → 3.24 %, and episode precision
at recall 0.88 moved 0.55 → 0.83. The precision gain is far larger than the accuracy
gain because what it removed was not noise but a systematic error at one hour of the
day.

Two measurement defects were fixed in the same pass, and the second was the larger.
Forecasts are now floored at zero — the model had been predicting down to −1.32 kW in
the overnight trough, and the detector divides by the expected value, so an ordinary
1 kW reading became a robust z of 15,319. And ground truth from a simulator run that
rewound its data clock is now discarded: such a run republishes timestamps that already
exist, every reading is dropped as a duplicate, and the faults it records happened to
nothing. That had 1,762 of 2,212 recorded events describing data that was never stored,
and it read as recall 0.375 for a detector whose recall is 0.84.

## Roadmap

| Phase | Status |
|---|---|
| 0 — foundation + optimizer | **complete** |
| 1 — infrastructure, ingestion, integrity | **complete** |
| 2 — forecasting + anomaly detection | **complete** |
| 3 — map UI, controls, dashboards | **complete** |
| 4 — evaluation harness + hardening | **complete** |
| 5 — rehearsal + documentation | next |

See [CLAUDE.md](CLAUDE.md) for the working context: decisions, traps, and the findings
that change the paper.

Critical path: `catalog schema → savings model → candidates → CP-SAT → /optimize → map
→ rehearsal`. Everything else hangs off that spine and is severable.

Cut list, in order, if the schedule slips: SMTP alerting → WireGuard → Grafana →
anomaly detection → forecasting. The optimizer, the map and the evaluation harness are
never cut. The first three were cut, and F10 and F11 record why: SMTP became an
optional webhook (`gemp.ingest.webhook`), WireGuard is described rather than built with
the MQTT topic contract as the evidence (`gemp.sim.node`), and Grafana's panels became
views inside the application, leaving its provisioning as an archive
(`tests/test_grafana_archive.py`).
