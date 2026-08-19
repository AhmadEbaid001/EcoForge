"""API contract tests, against SQLite via FastAPI's TestClient.

Covers the routes the map and the demonstration depend on, without a database
server, a broker or a container runtime.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

KEY_HEX = "cd" * 32
TEST_PASSWORD = "contract-tests-passphrase"
os.environ.setdefault("GEMP_HMAC_KEY", KEY_HEX)
# The API starts the MQTT ingester in its lifespan; these tests have no broker.
os.environ["GEMP_INGEST_ENABLED"] = "0"

from gemp.api import main as api_main  # noqa: E402
from gemp.api.deps import get_session  # noqa: E402
from gemp.auth import service as auth_service  # noqa: E402
from gemp.auth.service import CSRF_HEADER  # noqa: E402
from gemp.db import Base, ReadingRow  # noqa: E402
from gemp.ingest.integrity import GENESIS, sign  # noqa: E402
from gemp.repository import import_portfolio, stored_candidate_count  # noqa: E402

T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = tmp_path_factory.mktemp("api") / "gemp.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    with scope() as session:
        import_portfolio(session)
        # These tests are about the API contract, not about who may call it - that is
        # tests/test_auth.py. They run as an administrator so every route is
        # reachable, which keeps a permissions change from showing up as fifty
        # unrelated failures here.
        auth_service.create_user(session, username="contract-tests",
                                 password=TEST_PASSWORD, role="admin")

    def override_session():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_main.app.dependency_overrides[get_session] = override_session
    api_main.reset_context()
    auth_service.reset_throttle()

    with TestClient(api_main.app) as test_client:
        test_client.session_scope = scope
        login = test_client.post("/api/v1/auth/login", json={
            "username": "contract-tests", "password": TEST_PASSWORD,
        })
        assert login.status_code == 200, login.text
        # A browser echoes the CSRF cookie in a header automatically; httpx does not.
        test_client.headers[CSRF_HEADER] = login.json()["csrf_token"]
        yield test_client

    api_main.app.dependency_overrides.clear()
    api_main.reset_context()


def seed_readings(client, building_id: str = "b001", count: int = 20):
    key = bytes.fromhex(KEY_HEX)
    prev = GENESIS
    with client.session_scope() as session:
        for i in range(count):
            row = {
                "building_id": building_id,
                "ts": T0 + timedelta(minutes=15 * i),
                "kw": 40.0 + i,
                "source": "sim",
                "seq": i,
            }
            prev = sign(key, row, prev)
            session.add(ReadingRow(**row, sig=prev))


# --- health and metadata ----------------------------------------------------


def test_health_reports_each_dependency(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["api"] == "ok"
    assert body["database"] == "ok"


def test_meta_exposes_the_data_contract_state(client):
    body = client.get("/api/v1/meta").json()
    assert body["buildings"] == 50
    assert body["candidates"] > 50
    assert set(body["objectives"]) == {"lca_carbon", "raw_kwh", "egp_saved"}
    assert set(body["solvers"]) == {"cpsat", "greedy", "greedy_upgrade", "equal_split"}
    # Placeholder catalog rows are surfaced, not hidden - they gate submission.
    assert body["uncited_catalog_rows"]


# --- portfolio --------------------------------------------------------------


def test_buildings_are_listed(client):
    body = client.get("/api/v1/buildings").json()
    assert len(body) == 50
    assert {"id", "code", "district", "annual_kwh"} <= set(body[0])


def test_map_geojson_is_valid_and_unannotated_without_a_run(client):
    body = client.get("/api/v1/map/geojson").json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 50
    assert body["features"][0]["geometry"]["type"] in {"Polygon", "Point"}
    assert all(not f["properties"]["funded"] for f in body["features"])


# --- the core deliverable ---------------------------------------------------


def test_optimize_returns_an_allocation(client):
    response = client.post("/api/v1/optimize", json={"budget_egp": 10_000_000})
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "OPTIMAL"
    assert body["buildings_funded"] > 0
    assert body["total_cost_egp"] <= 10_000_000
    assert len(body["items"]) == body["buildings_funded"]
    assert body["run_id"]


def test_optimize_is_fast_enough_for_a_slider(client):
    """The budget slider re-solves on every drag; the round trip has to feel live."""
    response = client.post("/api/v1/optimize",
                           json={"budget_egp": 8_000_000, "persist": False})
    assert response.json()["solve_ms"] < 2000


def test_optimize_rejects_unknown_solver_and_objective(client):
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "solver": "magic"}).status_code == 422
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "objective": "vibes"}).status_code == 422
    assert client.post("/api/v1/optimize", json={"budget_egp": -5}).status_code == 422


def test_zero_budget_is_rejected_rather_than_silently_empty(client):
    assert client.post("/api/v1/optimize", json={"budget_egp": 0}).status_code == 422


@pytest.mark.parametrize("budget", [float("inf"), float("nan"), 1e308])
def test_a_nonsense_budget_is_a_422_not_a_500(client, budget):
    """Measured against the running stack before this was fixed: all three returned
    Internal Server Error. `int(round(inf))` raises inside the CP-SAT model build,
    which is nowhere near where a caller would look for the cause."""
    response = client.post(
        "/api/v1/optimize",
        content=json.dumps({"budget_egp": budget, "persist": False}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_the_solver_time_limit_is_bounded(client):
    """An unbounded limit is a request that never returns, held open by a stranger."""
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "max_seconds": 0}).status_code == 422
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "max_seconds": 600}).status_code == 422
    assert client.post("/api/v1/optimize", json={
        "budget_egp": 1e6, "max_seconds": 5, "persist": False,
    }).status_code == 200


def test_an_unhandled_error_names_its_type_and_leaks_nothing_else(client):
    """The body must be useful to whoever is fixing it and useless to anyone probing.

    A second client, because the shared one re-raises server exceptions so that a
    genuine bug in another test surfaces as that bug rather than as a 500. Here the
    500 IS the behaviour under test.
    """
    from gemp.api import main

    def explode(*_args, **_kwargs):
        raise RuntimeError("secret internal detail: connection string here")

    original = main.load_building_rows
    main.load_building_rows = explode
    try:
        with TestClient(main.app, raise_server_exceptions=False) as quiet:
            # Carry the session over: a fresh client is anonymous, and the middleware
            # would refuse the request at 401 before the route could fail at all.
            quiet.cookies.update(client.cookies)
            response = quiet.get("/api/v1/buildings")
    finally:
        main.load_building_rows = original

    assert response.status_code == 500
    body = response.json()
    assert body == {"error": "internal_error", "detail": "RuntimeError"}
    assert "secret internal detail" not in response.text


def test_compare_returns_every_solver_and_the_headline_delta(client):
    body = client.post("/api/v1/compare", json={"budget_egp": 10_000_000}).json()

    assert set(body["results"]) == {"cpsat", "greedy", "greedy_upgrade", "equal_split"}
    assert body["results"]["equal_split"]["improvement_vs_equal_split_pct"] is None
    assert body["results"]["cpsat"]["improvement_vs_equal_split_pct"] > 0
    for baseline in ("greedy", "greedy_upgrade", "equal_split"):
        assert body["results"]["cpsat"]["total_benefit_kgco2e"] >= (
            body["results"][baseline]["total_benefit_kgco2e"]
        )


def test_district_cap_makes_exact_optimization_pull_away(client):
    """F8, through the API: greedy cannot plan around the cap and strands budget."""
    capped = client.post("/api/v1/compare", json={
        "budget_egp": 10_000_000, "max_funded_per_district": 2,
    }).json()

    assert capped["cpsat_vs_greedy_pct"] > 0
    assert (capped["results"]["cpsat"]["total_cost_egp"]
            > capped["results"]["greedy"]["total_cost_egp"])


def test_a_stored_run_can_be_read_back_and_annotates_the_map(client):
    run_id = client.post("/api/v1/optimize", json={"budget_egp": 10_000_000}).json()["run_id"]

    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["run_id"] == run_id
    assert run["buildings_funded"] == len(run["items"])
    assert len(run["inputs_hash"]) == 16

    geojson = client.get("/api/v1/map/geojson", params={"run_id": run_id}).json()
    funded = [f for f in geojson["features"] if f["properties"]["funded"]]
    assert len(funded) == run["buildings_funded"]
    assert "label" in funded[0]["properties"]


def test_unknown_run_is_404(client):
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def test_building_candidates_list_every_option_considered(client):
    """The "why this building, why this measure" view. A recommendation nobody can
    interrogate is one they are asked to take on trust."""
    body = client.get("/api/v1/buildings/b001/candidates").json()

    assert body["building"]["id"] == "b001"
    assert body["options"], "a building with no options cannot be explained"
    # Ordered by benefit density, best first.
    scores = [o["score_per_kegp"] for o in body["options"]]
    assert scores == sorted(scores, reverse=True)


def test_building_candidates_mark_the_one_a_run_chose(client):
    run_id = client.post("/api/v1/optimize", json={"budget_egp": 10_000_000}).json()
    funded = run_id["items"][0]["building_id"]

    body = client.get(f"/api/v1/buildings/{funded}/candidates",
                      params={"run_id": run_id["run_id"]}).json()

    assert body["chosen_key"]
    assert sum(1 for o in body["options"] if o["chosen"]) == 1


def test_unknown_building_candidates_is_404(client):
    assert client.get("/api/v1/buildings/nope/candidates").status_code == 404


def test_forecast_metrics_report_which_model_is_in_use(client):
    body = client.get("/api/v1/metrics/forecast").json()
    assert "by_model" in body
    # F3: how many buildings are priced from measurement rather than the fixture.
    assert "annual_kwh_source" in body


def test_anomaly_metrics_expose_the_threshold_actually_in_use(client):
    """The threshold is tuned, so the dashboard must say which value produced the
    numbers on it - otherwise a re-tuning silently changes what the counts mean."""
    body = client.get("/api/v1/metrics/anomaly").json()
    assert body["threshold_k"] > 0
    assert body["window_days"] > 0
    assert "by_severity" in body
    assert isinstance(body["worst"], list)


def test_narrative_contrasts_two_buildings_with_different_best_measures(client):
    """Evidences the claim that replaced the proposal's Table 1 reversal: the best
    measure is building-specific, so no portfolio-wide priority list is right."""
    response = client.get("/api/v1/narrative/building-specific")
    assert response.status_code == 200

    body = response.json()
    assert len(body["buildings"]) == 2
    assert body["buildings"][0]["best"] != body["buildings"][1]["best"]
    assert sum(body["distribution"].values()) == 50
    for building in body["buildings"]:
        assert building["options"], "the contrast is only readable with the options shown"


def test_recompute_materialises_the_candidate_set(client):
    """Called after Nada edits data/. Writes the set a run was solved against, so a
    stored recommendation stays explainable once the catalog moves on."""
    body = client.post("/api/v1/candidates/recompute").json()

    assert body["buildings"] == 50
    assert body["candidates"] > 50
    assert len(body["inputs_hash"]) == 16
    assert body["uncited_catalog_rows"]

    with client.session_scope() as session:
        assert stored_candidate_count(session) == body["candidates"]


def test_recompute_is_idempotent_for_unchanged_inputs(client):
    """Re-running must not accumulate generations - "the current candidate set" would
    stop being a well-defined thing."""
    first = client.post("/api/v1/candidates/recompute").json()
    second = client.post("/api/v1/candidates/recompute").json()

    assert first["inputs_hash"] == second["inputs_hash"]
    with client.session_scope() as session:
        assert stored_candidate_count(session) == second["candidates"]


# --- time series and integrity ----------------------------------------------


def test_series_returns_stored_readings(client):
    seed_readings(client, "b001", 20)
    body = client.get("/api/v1/buildings/b001/series").json()
    assert len(body["points"]) == 20
    assert body["points"][0]["ts"] < body["points"][-1]["ts"]


def test_integrity_endpoint_passes_on_an_intact_chain(client):
    body = client.get("/api/v1/integrity/verify/b002").json()
    assert body["rows"] == 0
    assert body["chain_ok"]

    seed_readings(client, "b002", 15)
    body = client.get("/api/v1/integrity/verify/b002").json()
    assert body["rows"] == 15
    assert body["chain_ok"]
    assert body["break"] is None


def test_integrity_endpoint_names_the_tampered_row(client):
    """The demonstration: edit a row, call the endpoint, watch it point at the row."""
    seed_readings(client, "b003", 12)

    with client.session_scope() as session:
        row = session.get(ReadingRow, {"building_id": "b003",
                                       "ts": T0 + timedelta(minutes=15 * 4)})
        row.kw = 0.5

    body = client.get("/api/v1/integrity/verify/b003").json()
    assert not body["chain_ok"]
    assert body["break"]["reason"] == "modified"
    assert body["break"]["seq"] == 4


def test_a_tampered_row_carries_no_wrong_key_hint(client):
    """The hint must not appear where the data really was modified.

    Otherwise it trains whoever is on the demonstration to dismiss a genuine break as
    a configuration problem, which is the opposite of what the chain is for.
    """
    seed_readings(client, "b006", 12)

    with client.session_scope() as session:
        row = session.get(ReadingRow, {"building_id": "b006",
                                       "ts": T0 + timedelta(minutes=15 * 5)})
        row.kw = 0.5

    body = client.get("/api/v1/integrity/verify/b006").json()
    assert not body["chain_ok"]
    assert body["hint"] is None


def test_the_wrong_signing_key_is_not_reported_as_tampering(client):
    """A break at row zero is far more likely a key mismatch than an edit.

    An attacker with write access has no reason to start at the first row, and every
    later row fails too. The Phase 4 integration test hit exactly this: it picked up
    this suite's test key from the process environment and reported an intact
    production chain as "modified at seq 0".
    """
    from gemp.api import main
    from gemp.config import Settings

    seed_readings(client, "b005", 12)

    wrong = Settings(hmac_key="ab" * 32)
    original = main.get_settings
    main.get_settings = lambda: wrong
    try:
        body = client.get("/api/v1/integrity/verify/b005").json()
    finally:
        main.get_settings = original

    assert not body["chain_ok"]
    assert body["break"]["seq"] == 0
    assert "GEMP_HMAC_KEY" in body["hint"]


@pytest.fixture(autouse=True)
def isolated_anchor(tmp_path):
    """Every test in this module gets its own empty anchor file.

    Without this the integrity tests read the REAL `anchor/integrity_anchor.jsonl`
    from the checkout - the live stack's, six megabytes of it - and a building that
    is empty in the SQLite fixture but anchored at sequence 40,000 in production
    correctly reports as truncated. Two tests failed exactly that way.

    Same shape as the GEMP_HMAC_KEY problem: process-wide state that one module sets
    and every other module inherits. The fix is the same - never let a test read
    something the deployment wrote.
    """
    path = tmp_path / "integrity_anchor.jsonl"
    path.write_text("", encoding="utf-8")
    os.environ["GEMP_ANCHOR_PATH"] = str(path)
    yield path
    os.environ.pop("GEMP_ANCHOR_PATH", None)


def write_anchor(records) -> None:
    """Record anchored chain heads in the isolated anchor file."""
    path = Path(os.environ["GEMP_ANCHOR_PATH"])
    with path.open("w", encoding="utf-8") as fh:
        for building_id, last_seq, head_sig in records:
            fh.write(json.dumps({
                "building_id": building_id,
                "ts": T0.isoformat(),
                "last_seq": last_seq,
                "head_sig": head_sig.hex(),
            }) + "\n")


def test_a_truncated_tail_is_caught_by_the_external_anchor(client):
    """The one form of tampering a chain walk cannot see.

    Delete the last rows and what remains still verifies - there is nothing after
    them to break. Only the anchor written outside the database volume can catch it.
    Before Phase 4 nothing loaded that anchor, so `checkpoint_ok` was null on every
    response and this attack succeeded silently against the demonstration.
    """
    seed_readings(client, "b007", 12)

    with client.session_scope() as session:
        rows = session.query(ReadingRow).filter_by(building_id="b007").order_by(
            ReadingRow.seq).all()
        head_seq, head_sig = rows[-1].seq, rows[-1].sig

    write_anchor([("b007", head_seq, head_sig)])

    intact = client.get("/api/v1/integrity/verify/b007").json()
    assert intact["chain_ok"] and intact["checkpoint_ok"]
    assert intact["anchored"]

    with client.session_scope() as session:
        for row in session.query(ReadingRow).filter(
            ReadingRow.building_id == "b007", ReadingRow.seq >= 9
        ).all():
            session.delete(row)

    truncated = client.get("/api/v1/integrity/verify/b007").json()

    # The walk is still clean - that is the whole point of the attack.
    assert truncated["chain_ok"]
    assert truncated["checkpoint_ok"] is False
    assert "tail has been deleted" in truncated["hint"]


def test_deleting_every_row_is_not_reported_as_a_healthy_empty_chain(client):
    """The most complete deletion possible must not be the easiest to get away with."""
    seed_readings(client, "b008", 10)

    with client.session_scope() as session:
        rows = session.query(ReadingRow).filter_by(building_id="b008").order_by(
            ReadingRow.seq).all()
        head_seq, head_sig = rows[-1].seq, rows[-1].sig

    write_anchor([("b008", head_seq, head_sig)])

    with client.session_scope() as session:
        session.query(ReadingRow).filter_by(building_id="b008").delete()
    body = client.get("/api/v1/integrity/verify/b008").json()

    assert body["rows"] == 0
    assert body["chain_ok"] is False
    assert body["checkpoint_ok"] is False
    assert "truncated" in body["hint"]


def test_an_unanchored_building_says_so_rather_than_claiming_verification(client):
    """`checkpoint_ok: null` is honest only when it is accompanied by `anchored: false`.

    Otherwise a reader cannot tell "nothing was anchored" from "the anchor passed".
    """
    seed_readings(client, "b009", 8)
    write_anchor([("b001", 5, b"\x11" * 32)])          # a different building
    body = client.get("/api/v1/integrity/verify/b009").json()

    assert body["chain_ok"]
    assert body["anchored"] is False
    assert body["checkpoint_ok"] is None


def test_integrity_endpoint_detects_a_deleted_row(client):
    seed_readings(client, "b004", 12)

    with client.session_scope() as session:
        row = session.get(ReadingRow, {"building_id": "b004",
                                       "ts": T0 + timedelta(minutes=15 * 6)})
        session.delete(row)

    body = client.get("/api/v1/integrity/verify/b004").json()
    assert not body["chain_ok"]
    assert body["break"]["reason"] == "deleted"


# --- load shedding ----------------------------------------------------------


def test_a_saturated_solver_sheds_load_instead_of_queueing(client):
    """A 429 arriving now beats a correct answer arriving after the moment has passed.

    Solving is CPU-bound and CP-SAT already runs eight search workers, so a handful
    of concurrent requests would slow down the one that matters - the person dragging
    the budget slider in front of judges.
    """
    from gemp.api import main

    held = [main._solve_slots.acquire(blocking=False)
            for _ in range(main.MAX_CONCURRENT_SOLVES)]
    try:
        assert all(held)
        response = client.post("/api/v1/optimize",
                               json={"budget_egp": 1e6, "persist": False})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "2"
    finally:
        for acquired in held:
            if acquired:
                main._solve_slots.release()

    # Slots are returned, so the next request succeeds.
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "persist": False}).status_code == 200


def test_the_anchor_is_read_before_the_rows(client):
    """Ordering that prevents a false tamper alarm on a live system.

    Ingestion never stops. Read the rows first and the ingester can flush new
    readings and checkpoint past the head that snapshot contains, so the anchor claims
    a sequence the rows legitimately do not reach and an intact chain reports as
    truncated. The claims harness produced exactly that false positive against the
    live stack, on a building nobody had touched.

    Reading the anchor first makes the check one-sided in the safe direction: later
    writes only extend the chain, and a chain longer than its anchor is healthy.
    """
    from gemp import services

    order = []

    real_anchor = services.latest_checkpoint
    real_chain = services.read_chain

    def traced_anchor(*args, **kwargs):
        order.append("anchor")
        return real_anchor(*args, **kwargs)

    def traced_chain(*args, **kwargs):
        order.append("rows")
        return real_chain(*args, **kwargs)

    services.latest_checkpoint = traced_anchor
    services.read_chain = traced_chain
    try:
        client.get("/api/v1/integrity/verify/b001")
    finally:
        services.latest_checkpoint = real_anchor
        services.read_chain = real_chain

    assert order.index("anchor") < order.index("rows"), (
        f"anchor must be read before rows, got {order}"
    )


# --- acknowledging alerts ---------------------------------------------------


def seed_anomalies(client, building_id="b010", count=5, severity="critical"):
    from gemp.db import AnomalyRow

    with client.session_scope() as session:
        for i in range(count):
            session.add(AnomalyRow(
                building_id=building_id,
                ts=T0 + timedelta(hours=i),
                observed_kw=150.0, expected_kw=100.0, residual=0.5,
                robust_z=20.0, severity=severity, acknowledged=False,
            ))


def test_an_alert_can_be_acknowledged(client):
    """Phase 2 shipped the column and the filters; nothing could set it.

    The map counted open anomalies and offered no way to close one, so the count only
    ever grew - sixteen months of replay reached eleven thousand criticals. A number
    that cannot go down is a number nobody reads.
    """
    from gemp.db import AnomalyRow

    seed_anomalies(client, "b011", 3)
    with client.session_scope() as session:
        first = session.query(AnomalyRow).filter_by(building_id="b011").first().id

    body = client.post(f"/api/v1/anomalies/{first}/acknowledge").json()
    assert body["changed"] == 1

    # Already closed, so there is nothing left to do with it.
    assert client.post(f"/api/v1/anomalies/{first}/acknowledge").status_code == 404


def test_alerts_can_be_closed_in_bulk_and_reopened(client):
    """Reversibility is what makes a bulk close over thousands of rows safe."""
    seed_anomalies(client, "b012", 6)

    closed = client.post("/api/v1/anomalies/acknowledge",
                         json={"building_id": "b012"}).json()
    assert closed["changed"] == 6
    assert closed["open_remaining"] == 0

    reopened = client.post("/api/v1/anomalies/acknowledge",
                           json={"building_id": "b012", "acknowledged": False}).json()
    assert reopened["changed"] == 6
    assert reopened["open_remaining"] == 6


def test_an_unfiltered_acknowledge_is_refused(client):
    """Closing every alert in the portfolio must not be reachable by omission."""
    response = client.post("/api/v1/anomalies/acknowledge", json={})
    assert response.status_code == 422
    assert "at least one" in response.json()["detail"]


def test_acknowledging_by_age_uses_data_time(client):
    """Under 720x replay the wall clock is months behind the data."""
    seed_anomalies(client, "b013", 4)
    cutoff = (T0 + timedelta(hours=2)).isoformat()

    body = client.post("/api/v1/anomalies/acknowledge",
                       json={"building_id": "b013", "before": cutoff}).json()

    assert body["changed"] == 2            # hours 0 and 1, not 2 and 3
    assert body["open_remaining"] == 2


def test_the_count_only_reports_alerts_this_call_actually_closed(client):
    """Otherwise "changed" means "rows the WHERE clause matched", which is not news."""
    seed_anomalies(client, "b014", 3)
    client.post("/api/v1/anomalies/acknowledge", json={"building_id": "b014"})

    again = client.post("/api/v1/anomalies/acknowledge",
                        json={"building_id": "b014"}).json()
    assert again["changed"] == 0


def test_an_unknown_severity_is_rejected_rather_than_matching_nothing(client):
    response = client.post("/api/v1/anomalies/acknowledge",
                           json={"building_id": "b015", "severity": "catastrophic"})
    assert response.status_code == 422


def test_candidates_carry_what_the_evidence_panel_states(client):
    """The map's option table names an option's service life and the building's HVAC
    type, so both have to be in the payload rather than inferred on the client.

    Service life is the SHORTEST in a bundle, because that is the one deciding how
    often the option is bought again inside the 30-year horizon - and it is what makes
    a cheap ten-year option legible next to a dearer one that lasts thirty.
    """
    body = client.get("/api/v1/buildings/b001/candidates").json()

    assert body["building"]["hvac_type"], "the panel names the HVAC type beside its age"

    options = body["options"]
    assert options, "b001 should have applicable interventions"
    assert all("service_life_yr" in o for o in options)
    lives = [o["service_life_yr"] for o in options if o["service_life_yr"] is not None]
    assert lives, "at least one option should state a service life"
    assert all(1 <= life <= 60 for life in lives), lives
