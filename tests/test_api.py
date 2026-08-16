"""API contract tests, against SQLite via FastAPI's TestClient.

Covers the routes the map and the demonstration depend on, without a database
server, a broker or a container runtime.
"""

from __future__ import annotations

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
    assert set(body["solvers"]) == {"cpsat", "greedy", "equal_split"}
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


def test_compare_returns_all_three_solvers_and_the_headline_delta(client):
    body = client.post("/api/v1/compare", json={"budget_egp": 10_000_000}).json()

    assert set(body["results"]) == {"cpsat", "greedy", "equal_split"}
    assert body["results"]["equal_split"]["improvement_vs_equal_split_pct"] is None
    assert body["results"]["cpsat"]["improvement_vs_equal_split_pct"] > 0
    assert body["results"]["cpsat"]["total_benefit_kgco2e"] >= (
        body["results"]["greedy"]["total_benefit_kgco2e"]
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


def test_integrity_endpoint_detects_a_deleted_row(client):
    seed_readings(client, "b004", 12)

    with client.session_scope() as session:
        row = session.get(ReadingRow, {"building_id": "b004",
                                       "ts": T0 + timedelta(minutes=15 * 6)})
        session.delete(row)

    body = client.get("/api/v1/integrity/verify/b004").json()
    assert not body["chain_ok"]
    assert body["break"]["reason"] == "deleted"
