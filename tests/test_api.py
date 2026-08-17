"""API contract tests, against SQLite via FastAPI's TestClient.

Covers the routes the map and the demonstration depend on, without a database
server, a broker or a container runtime.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

KEY_HEX = "cd" * 32
os.environ.setdefault("GEMP_HMAC_KEY", KEY_HEX)
# The API starts the MQTT ingester in its lifespan; these tests have no broker.
os.environ["GEMP_INGEST_ENABLED"] = "0"

from gemp.api import main as api_main  # noqa: E402
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

    api_main.app.dependency_overrides[api_main.get_session] = override_session
    api_main.reset_context()

    with TestClient(api_main.app) as test_client:
        test_client.session_scope = scope
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


def test_integrity_endpoint_detects_a_deleted_row(client):
    seed_readings(client, "b004", 12)

    with client.session_scope() as session:
        row = session.get(ReadingRow, {"building_id": "b004",
                                       "ts": T0 + timedelta(minutes=15 * 6)})
        session.delete(row)

    body = client.get("/api/v1/integrity/verify/b004").json()
    assert not body["chain_ok"]
    assert body["break"]["reason"] == "deleted"
