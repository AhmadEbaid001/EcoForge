# GEMP — working context

Read this first. It carries the decisions and the traps, so neither has to be
rediscovered. RoboDam2026 competition entry, Team Ecoforge (Ahmed Ebaid — systems,
Nada Wagdy — environmental). Deadline **31 August 2026**.

## What this is

A **budget allocator**, not a monitoring dashboard. Given ~50 government buildings and
a fixed budget, decide which buildings get which retrofit to maximise life-cycle
benefit. Ingestion, forecasting, anomaly detection and the map exist to feed that
decision or display it.

## Status

| Phase | State |
|---|---|
| 0 — domain + optimizer | done |
| 1 — infra, ingestion, integrity | done |
| 2 — forecasting, anomaly detection | done |
| 3 — map UI, controls, Grafana | done (audited) |
| 4 — evaluation harness, hardening, CI | **next** |
| 5 — rehearsal, documentation | not started |

226 tests, lint clean. Six containers healthy.

## Run it

```bash
docker compose up -d
python -m gemp.seed --months 6                    # ~6 min, 863k signed readings
python -m gemp.optimize.cli --budget 10000000 --compare
python -m gemp.ml.jobs                            # refit + anomalies, ~45 s
python -m gemp.ml.evaluate --k 4 6 8 10           # tune the anomaly threshold
```

Map at `http://localhost:8080`, Grafana at `http://localhost:3000`, docs at `/docs`.

**Host-side scripts need `GEMP_DB_HOST=127.0.0.1 GEMP_DB_PORT=5433`** plus the
`GEMP_*` values from `.env`.

## Traps — every one of these cost real time

- **Never use `localhost` for the database from the host.** Docker binds IPv4;
  Windows resolves `localhost` to `::1` first and stalls on an IPv6 timeout.
  Measured: 130.3 s versus 0.2 s. Presents as "the database is slow", not an error.
  `Settings.dsn` rewrites it, but scripts that build their own URL must use
  `127.0.0.1`.
- **Container DB port is 5433**, because a native PostgreSQL install usually holds
  5432. Inside the compose network it is still `timescaledb:5432`.
- **Never read the hypertable per building with a bound parameter.** On 253 chunks the
  planner cannot exclude chunks for an unseen value: 130 s for 6,694 rows versus 1.7 s
  for all 334,702 unparameterised. Read the whole portfolio once — `ml/dataset.py`.
- **Every rolling window is in DATA time, never wall time.** The simulator runs at
  720×, so data time runs ahead and any `now()`-anchored window comes back empty.
  Same reason the continuous-aggregate refresh policy uses `NULL` offsets.
- **`rolling().apply()` with a Python lambda is fatal here** — ~8e8 interpreter
  operations, it hung the evaluation entirely. Keep the MAD vectorised.
- **SQLite ignores foreign keys** unless `PRAGMA foreign_keys=ON`. It is enabled in
  `db.py`; without it the tests accept what PostgreSQL rejects.
- **FastAPI resumes `yield` dependencies after the response is sent.** Anything whose
  id is handed to the client must be committed inside the service call, not left to
  the request-scoped session. A browser beats that race; curl does not.
- **`INTERVAL '7 days'` is PostgreSQL-only** and breaks the SQLite contract tests.
  Compute cutoffs in Python and pass them as parameters.
- `docker compose build core` does **not** rebuild `sim` — they are separate images
  from the same Dockerfile.

## Decisions worth not relitigating

- **Modular monolith.** The ingester and scheduler are threads inside the API process.
  The scheduler must be in-process: it rewrites `annual_kwh`, and only there can it
  invalidate the API's cached `OptimizerContext`.
- **No mapping library.** F13 needs the demo to survive an unplugged cable; Leaflet's
  default tiles fetch remotely. Fifty polygons is inline SVG. `tests/test_web.py`
  guards against any external reference creeping back in.
- **No PostGIS.** Footprints are GeoJSON in JSONB; the system never runs a spatial
  query.
- **Timescale objects live outside Alembic** — a continuous aggregate cannot be
  created inside a transaction.
- **Money is EGP integers everywhere.** The ÷1000 for "per thousand EGP" happens only
  at the display layer.
- `domain/` imports no database, broker or web framework. That is what lets the
  optimizer run from CSV fixtures with nothing else up.

## Findings that change the paper

1. **The optimizer maximises absolute benefit, not benefit density** (F1). Densities
   with different denominators do not sum. Density survives only as the greedy sort
   key. The proposal's §4.4 wording still needs fixing.
2. **The proposal's Table 1 reversal is an artifact** of crediting each measure over
   its own service life (F2). On a common 30-year horizon it disappears.
3. **The replacement narrative I proposed also fails.** Rooftop solar never has the
   best benefit density at any building — LED wins on 33, BMS controls on 16,
   insulation on 1. At 22,000 EGP/kWp generation cannot compete per EGP with cheap
   efficiency. That is the ordinary efficiency-before-generation loading order.
   **What is true and demonstrable:** the best measure is building-specific (HVAC age
   4 yr favours lighting, 10 yr favours controls), so no portfolio-wide priority list
   is right. `/api/v1/narrative/building-specific` evidences it.
4. **The LCA layer changes almost no decisions.** `lca_carbon` and `raw_kwh` fund the
   same set at every realistic budget, because embodied carbon is a median 8 % of
   gross avoided emissions. Pinned by
   `test_lca_adjustment_barely_changes_the_funded_set`. A time-of-use marginal
   emission factor would make it bite — and would make the metered time series reach
   the decision, which today it barely does.
5. **CP-SAT beats greedy by ~10 % unconstrained, ~134 % under a district cap.** Lead
   with the cap; report the unconstrained gap honestly.

## Open items

- **`data/catalog.csv` is entirely uncited.** All six rows say `TODO(Nada): cite`.
  `python -m gemp.domain.catalog --validate --strict` fails until they are sourced.
  This is the one thing code cannot close, and every number the platform reports
  derives from it.
- **Anomaly precision is 0.50 at recall 0.82** (k=8, episode-level). Recall meets the
  0.8 gate; precision does not meet 0.6. Roughly 1.5 false alerts per building per six
  months.
- Tests run against SQLite, not the live PostgreSQL. A Postgres-backed integration
  test belongs in Phase 4.
- Footprint rendering past zoom 3 is implemented and syntax-checked but was never
  visually confirmed — browser access was blocked before it could be re-checked.

## House style

Commit messages state what changed and **why it mattered**, including bugs found and
what they would have cost. Comments explain reasoning, not mechanics. When a
measurement contradicts an earlier claim, the claim gets corrected in writing rather
than quietly dropped.
