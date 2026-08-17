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
| 4 — evaluation harness, hardening, CI | done |
| 5 — rehearsal, documentation | **next** |

278 tests plus 15 integration tests that skip themselves without the stack, 83%
coverage, lint clean. Six containers healthy.

Integration tests need the stack and their own environment:

```bash
GEMP_DB_HOST=127.0.0.1 GEMP_DB_PORT=5433 GEMP_MQTT_HOST=127.0.0.1 pytest -m integration
```

They never write to the demonstration database. The PostgreSQL ones roll back; the
MQTT ones use a throwaway SQLite file and a test-only topic prefix the running
ingester does not subscribe to.

## Run it

```bash
docker compose up -d
python -m gemp.seed --months 6                    # ~6 min, 863k signed readings
python -m gemp.optimize.cli --budget 10000000 --compare
python -m gemp.ml.jobs                            # refit + anomalies, ~45 s
python -m gemp.ml.evaluate --k 4 6 8 10           # tune the anomaly threshold

python scripts/check.py                           # every CI gate, locally
python -m gemp.evaluate                           # re-measure every claim, ~12 s
python -m gemp.evaluate --with-db                 # adds forecasting and anomalies
pytest -m integration                             # needs the stack; skips without it
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
- **A container that writes a file needs a mount for it, and nothing says so when it
  does not.** The `sim` service wrote `anchor/live_anomalies.jsonl` — the ground truth
  for every fault it injects — inside the container, because only `core` mounted
  `./anchor`. It ran for months of data time with no error anywhere, and the loss only
  became visible as an anomaly precision of 0.26 that should have been 0.52.
- **Never score a detector past the end of its ground truth.** Data keeps arriving at
  720×; the truth file does not. Every correctly detected fault beyond it counts as a
  false alarm. Coverage is tracked **per source** (`seed`, `live`) and episodes outside
  a covered window are excluded rather than judged — a single min-to-max span would
  swallow the gap where nothing was being recorded and reintroduce the same error.
- **Code that is written but never called looks exactly like code that works.**
  `verify_against_checkpoint` was implemented and unit-tested in Phase 1 and called by
  nothing until Phase 4: the endpoint took checkpoint arguments and no caller passed
  them, so `checkpoint_ok` was `null` on every response the demonstration ever served.
  Tamper detection for *modified* rows was real; for *deleted* ones it was absent, and
  deletion is the easier attack. Unit tests do not notice an unwired capability —
  only a claim measured against the running system does.
- **`INTERVAL '31 days'` in `warm_from_db` meant the ingester could not run against
  SQLite at all**, despite `Ingester` taking an injectable session factory precisely
  so it could. The contradiction sat unnoticed because nothing ran the real consumer
  end to end until Phase 4. Same rule as everywhere else: compute cutoffs in Python,
  pass them as parameters.
- **MQTT client ids are exclusive.** A second client connecting as `gemp-ingest`
  disconnects the production ingester and stops ingestion for as long as it runs.
  Anything that consumes from a live broker needs its own id and `clean_session=True`,
  or it also leaves a durable subscription queueing messages after it exits.
- **`pytest` imports every test module before running any of them**, so
  `tests/test_api.py` setting `GEMP_HMAC_KEY` in the process environment applies to the
  whole session. Environment beats `.env` in pydantic-settings, so host-side code that
  calls `get_settings()` inside a test run gets the *test* key. It presented as an
  intact production chain reporting "modified at seq 0".
- **A continuous aggregate's refresh policy is scheduled in WALL time** while the data
  it materialises advances in data time. At 720× the 5-minute schedule is 60 hours of
  data, so the aggregate tail is routinely a day or two behind the hypertable. That is
  the design working; comparing the newest buckets against the raw table measures
  refresh timing rather than correctness.

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
- **The API is unauthenticated except for `/candidates/recompute`**, which is gated
  only when `GEMP_ADMIN_TOKEN` is set. The demonstration network is air-gapped and a
  token that must be set correctly on the day is a way to lose the demonstration.
  Anything exposed beyond loopback must set that variable, and the API logs a warning
  on every call while it is unset.
- **Concurrent solves are capped and excess requests get 429, not a queue.** During a
  demonstration the request that matters is the slider drag happening now.

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
5. **The old "CP-SAT beats greedy by ~10 % unconstrained, ~134 % under a cap" was
   measured against a saturating baseline and must not be quoted.** Plain greedy never
   revisits a funded building, so once every building holds its cheapest dense option —
   about 16 M EGP on this portfolio — it stops spending entirely. Its benefit is flat
   from there while CP-SAT keeps climbing, the "gap" grows to +127 % at 40 M, and above
   30 M even equal split beats it. The number was measuring greedy's ceiling.

   `greedy_upgrade` is the repair: first fit, then keep swapping a funded building up
   to a costlier better option while the money lasts. Against **that** baseline, over
   40 budget/objective instances: **CP-SAT wins by a median of 1.4 % unconstrained and
   20.1 % under a district cap of 2** — a 14.7× ratio. Both optimizers beat equal split
   by 163–234 %. That is the defensible version of the argument and it is the one the
   paper should make: exact optimization earns its place through side constraints no
   greedy variant can express, not through a large unconstrained margin.

6. **Two claims in the review were mis-specified rather than wrong.** "Every bundle
   saves less than the sum of its parts" fails for 509 of 1,114 bundles and should:
   lighting and HVAC controls act on different end uses and rooftop solar is
   generation, so there is no interaction term to lose. What holds is the narrower
   claim — bundles *sharing an end use* are strictly sub-additive (605/605), and no
   bundle exceeds the sum of its parts. Likewise the LCA finding is better stated as
   what the difference is worth (worst-case 0.02 % of carbon) than as set identity,
   which differs at 3 of 20 budgets purely through ties.

## Open items

- **`data/catalog.csv` is entirely uncited.** All six rows say `TODO(Nada): cite`.
  `python -m gemp.domain.catalog --validate --strict` fails until they are sourced.
  This is the one thing code cannot close, and every number the platform reports
  derives from it.
- **Anomaly precision is 0.55 at recall 0.81** (k=8, episode-level), and **the 0.6
  target is not reachable by tuning.** k only slides a point along one curve. Phase 4
  swept 64 combinations of k, minimum episode duration and minimum peak z: duration
  buys precision at the same exchange rate as k, and a peak-z floor makes precision
  *worse* (0.607 → 0.535 at k=8), because a frozen meter barely deviates while the
  largest residuals are legitimate load the forecaster missed. Precision reaches 0.601
  at k=10 but only by dropping recall to 0.756, which is the wrong trade for a fault
  detector. Closing this needs a better expected-load model — the fixed features in
  `ml/features.py` are the place to look, not `anomaly_k`.
- **Those two numbers are still drifting upward** as the live ground-truth file
  accumulates, because a correct detection of a fault with no record is excluded
  rather than credited. Precision at k=8 read 0.515 at 454 recorded events and 0.550
  at 618. Recall sits close to its gate (0.79–0.81 across runs), so a single run
  reporting 0.79 is drift, not a regression — re-run before treating it as one.
- The claims harness reports two known-open findings (`DATA`, `F9-b`). They print
  `FAIL*` and do not fail the run.

## The evaluation harness

`python -m gemp.evaluate` re-measures every claim the paper makes and exits 1 if one
has stopped holding. It is not a test suite: a test pins behaviour that must not
change, a claim re-measures a sentence already written down so the sentence can be
corrected when the measurement moves. It found the greedy saturation defect, the
mis-specified bundling claim, and the anomaly scoring error above.

Claims already documented as open — the uncited catalog, the anomaly precision target
— report `FAIL*` and do **not** fail the run. A permanently red gate teaches everyone
to ignore the gate.

`scripts/check.py` runs the four CI gates locally, which is what actually enforces
them: the repository has no remote, so `.github/workflows/ci.yml` has never fired.

## House style

Commit messages state what changed and **why it mattered**, including bugs found and
what they would have cost. Comments explain reasoning, not mechanics. When a
measurement contradicts an earlier claim, the claim gets corrected in writing rather
than quietly dropped.
