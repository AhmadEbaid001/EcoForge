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

No database, no broker, no containers required for any of the above.

## What works today (Phase 0 complete)

| Command | What it does |
|---|---|
| `python -m gemp.domain.catalog --validate` | Check the data contract. Add `--strict` to fail on uncited catalog rows. |
| `python scripts/fetch_osm_buildings.py` | Build the portfolio from **real** OpenStreetMap footprints (New Cairo). Needs network. |
| `python scripts/gen_buildings.py` | Offline fallback: synthetic square footprints, same schema. |
| `python -m gemp.optimize.cli --budget 10000000` | Solve and print an allocation. |
| `python -m gemp.optimize.cli --budget 10000000 --compare` | CP-SAT vs greedy vs equal-split on the same instance. |
| `python -m gemp.optimize.cli --budget 10000000 --compare --district-cap 2` | The instance where the exact solver decisively beats greedy. |
| `python -m pytest` | 92 tests. |

### Current headline numbers

50 real OSM footprints, New Cairo, 32.7 GWh/yr, budget 10,000,000 EGP:

| Solver | Funded | Spent | Life-cycle kgCO₂e | vs equal split |
|---|---|---|---|---|
| equal_split | 30 | 5,117,794 | 12,526,901 | baseline |
| greedy | 34 | 9,966,764 | 29,923,997 | +139 % |
| cpsat | 24 | 9,999,207 | 32,916,590 | **+163 %** |

With a two-per-district cap the picture changes sharply — greedy spends only
3.55 M of the 10 M because it commits its district slots to cheap high-density
options and then cannot use the rest:

| Solver | Funded | Spent | Life-cycle kgCO₂e | vs equal split |
|---|---|---|---|---|
| equal_split | 9 | 1,644,764 | 5,261,983 | baseline |
| greedy | 10 | 3,553,479 | 13,411,548 | +155 % |
| cpsat | 10 | 9,986,292 | 31,368,000 | **+496 %** |

**CP-SAT over greedy: +10 % unconstrained, +134 % under the cap.** The unconstrained
gap is small because a density-ordered heuristic is near-optimal on a plain knapsack —
that is reported rather than hidden. The cap is where exact optimization earns its
place, and it is also the realistic case: no ministry funds nine buildings in one
district and none in the next.

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
  api/  web/   Phase 3: FastAPI + Leaflet
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

## Roadmap

| Phase | Dates | Status |
|---|---|---|
| 0 — foundation + optimizer | Aug 14–17 | **complete** |
| 1 — infrastructure, ingestion, integrity | Aug 18–20 | not started |
| 2 — forecasting + anomaly detection | Aug 21–23 | not started |
| 3 — map UI, controls, Grafana | Aug 24–26 | not started |
| 4 — evaluation harness + hardening | Aug 27–29 | not started |
| 5 — rehearsal + documentation | Aug 30–31 | not started |

Critical path: `catalog schema → savings model → candidates → CP-SAT → /optimize → map
→ rehearsal`. Everything else hangs off that spine and is severable.

Cut list, in order, if the schedule slips: SMTP alerting → WireGuard → Grafana →
anomaly detection → forecasting. The optimizer, the map and the evaluation harness are
never cut.
