"""What SQLite cannot tell us.

The rest of the suite runs against SQLite, which is fast, needs no container, and is
wrong about several things that matter here: it has no hypertables, no continuous
aggregates, no JSONB, and a different planner. Every trap in CLAUDE.md that cost real
time lived in that gap.

These tests run against the live TimescaleDB and are skipped - not failed - when it
is unreachable, because a machine without hardware virtualisation still has to be
able to run the suite.

    python -m pytest -m integration

**They do not modify the seeded data.** The one test that writes does so inside a
transaction that is always rolled back, on a building id no seed produces. Running
this against the demonstration stack an hour before judging must be safe, or nobody
will run it at all.

Host-side runs need `GEMP_DB_HOST=127.0.0.1` and `GEMP_DB_PORT=5433`. `localhost`
resolves to `::1` first on Windows and costs 130 s per connection.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from gemp.db import BuildingRow, ReadingRow
from gemp.ingest.integrity import verify_chain
from gemp.ml.dataset import load_hourly_all
from gemp.repository import latest_reading_ts, load_buildings, read_chain

pytestmark = pytest.mark.integration


CONNECT_TIMEOUT_S = 5

# Must match `REFRESH_POLICY` in gemp/timescale.py. The aggregate's staleness bound
# is this interval multiplied by the replay speed, because the policy is scheduled in
# wall time while the data it materialises advances in data time.
REFRESH_SCHEDULE_MINUTES = 5


@pytest.fixture(scope="module")
def engine():
    """The live engine, or a skip. Never a failure."""
    from sqlalchemy import create_engine

    from gemp.config import get_settings

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - missing GEMP_HMAC_KEY is a skip, not a bug
        pytest.skip(f"settings unavailable: {exc}")

    engine = create_engine(
        settings.dsn,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - not running the stack is normal
        pytest.skip(f"no PostgreSQL at {settings.resolved_db_host}:{settings.db_port} ({exc})")

    if engine.dialect.name != "postgresql":
        pytest.skip(f"dialect is {engine.dialect.name}")

    return engine


@pytest.fixture(scope="module")
def deployment_key() -> bytes:
    """The key this deployment actually signed with, read from `.env` directly.

    `get_settings()` cannot be trusted here. `tests/test_api.py` sets GEMP_HMAC_KEY
    in the process environment at import time so the SQLite suite has a key, and
    pytest imports every test module before running any of them - so by the time
    this file executes, an environment variable is shadowing `.env`. Environment
    beats env_file in pydantic-settings, so the wrong key is picked up silently and
    every real signature then fails to verify.

    It reported as "modified at seq 0", which reads like tampering rather than like
    the wrong key. Reading the file directly is the only way this test measures the
    chain instead of measuring test-module import order.
    """
    from gemp.paths import data_dir

    env_path = data_dir().parent / ".env"
    if not env_path.exists():
        pytest.skip(f"{env_path} not found; cannot recover the deployment signing key")

    for line in env_path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "GEMP_HMAC_KEY":
            raw = value.strip()
            try:
                return bytes.fromhex(raw)
            except ValueError:
                return raw.encode("utf-8")

    pytest.skip("GEMP_HMAC_KEY not set in .env")


@pytest.fixture
def session(engine):
    """A session whose work is always rolled back.

    Binding the session to an outer transaction and rolling that back means a test
    can insert whatever it needs to exercise a constraint without leaving anything
    behind in a database that is also the demonstration.
    """
    from sqlalchemy.orm import Session

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="module")
def seeded(engine):
    from sqlalchemy.orm import Session

    with Session(bind=engine) as session:
        count = session.execute(select(func.count()).select_from(ReadingRow)).scalar_one()
    if not count:
        pytest.skip("no readings stored; run `python -m gemp.seed --months 6` first")
    return count


# --- Timescale objects ------------------------------------------------------


def test_reading_is_a_hypertable(engine):
    """Plain PostgreSQL would answer every query here identically, and slower."""
    with engine.connect() as connection:
        chunks = connection.execute(text(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'reading'"
        )).scalar_one()
    assert chunks > 0


def test_the_hourly_continuous_aggregate_exists(engine):
    with engine.connect() as connection:
        found = connection.execute(text(
            "SELECT count(*) FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = 'reading_hourly'"
        )).scalar_one()
    assert found == 1


def test_the_aggregate_has_materialised_the_newest_data(engine, seeded):
    """The NULL-offset refresh policy, checked where it actually applies.

    A conventional `end_offset => INTERVAL '1 hour'` is measured against the WALL
    clock. The simulator runs at 720x, so data time runs ahead and every reading
    looks like it is in the future: the aggregate would refuse to materialise the
    newest data and the dashboard would show a permanently empty tail during the one
    demonstration that matters. SQLite cannot express any part of this.

    The tolerance has to be stated in DATA time, and it is large. The refresh policy
    is scheduled every 5 wall-minutes, which at 720x is 60 hours of data - so a tail
    that is a day or two stale is the policy working normally, not a fault. An
    earlier version of this test asserted two hours, which is a bound the design
    cannot meet at any speed above about 24x.
    """
    from gemp.config import get_settings

    speed = get_settings().sim_speed
    allowed = timedelta(minutes=REFRESH_SCHEDULE_MINUTES * 2 * speed)

    with engine.connect() as connection:
        newest_raw = connection.execute(text("SELECT max(ts) FROM reading")).scalar_one()
        newest_agg = connection.execute(
            text("SELECT max(bucket) FROM reading_hourly")
        ).scalar_one()

    assert newest_agg is not None, "aggregate is empty; refresh policy is not running"
    lag = newest_raw - newest_agg
    assert lag <= allowed, (
        f"aggregate tail is {lag} behind the hypertable, more than two refresh "
        f"intervals ({allowed}) at {speed}x replay"
    )


def test_the_aggregate_agrees_with_the_raw_hypertable(engine, seeded):
    """If these disagree, every forecast is trained on something the meter never saw.

    Sampled well behind the tail on purpose. The newest buckets are still filling -
    the aggregate materialised them when only part of the hour had arrived - so a
    mismatch there measures refresh timing rather than correctness. Measured on this
    stack, a bucket five back from the head read 10.12 against a true 10.50.
    """
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT h.building_id, h.bucket, h.avg_kw,
                   (SELECT avg(kw) FROM reading r
                     WHERE r.building_id = h.building_id
                       AND r.ts >= h.bucket
                       AND r.ts <  h.bucket + INTERVAL '1 hour') AS raw_avg
            FROM reading_hourly h
            WHERE h.bucket < (SELECT max(bucket) FROM reading_hourly) - INTERVAL '30 days'
            ORDER BY h.bucket DESC
            LIMIT 1
        """)).one()

    assert row.raw_avg is not None
    assert row.avg_kw == pytest.approx(row.raw_avg, rel=1e-6)


# --- the query that cost 130 seconds ----------------------------------------


def test_the_whole_portfolio_reads_in_one_query_and_stays_fast(engine, seeded):
    """The chunk-exclusion trap, pinned against the real planner.

    Measured on this stack: a per-building query bound to a parameter took 130.3 s
    for 6,694 rows, while this unparameterised read returned 334,702 rows for all
    fifty buildings in 1.7 s. On a hypertable with 253 chunks the planner cannot
    exclude chunks for a parameter value it has not seen.

    The bound is deliberately loose - a tight timing assertion on shared hardware is
    a flaky test. It is there to catch a return to the 130 s behaviour, which is two
    orders of magnitude away, not to measure performance.
    """
    started = time.perf_counter()
    series = load_hourly_all(engine)
    elapsed = time.perf_counter() - started

    assert len(series) >= 2
    assert all(not frame.empty for frame in series.values())
    assert elapsed < 30.0, f"portfolio read took {elapsed:.1f}s; chunk exclusion regressed"


def test_windows_are_anchored_in_data_time(engine, seeded):
    """`latest_reading_ts` is the anchor every rolling window uses.

    Under 720x replay it runs well ahead of the wall clock, which is the whole reason
    nothing in the system anchors on `now()`.
    """
    from sqlalchemy.orm import Session

    with Session(bind=engine) as session:
        newest = latest_reading_ts(session)

    assert newest is not None
    assert newest.tzinfo is not None, "timestamps must be timezone-aware in PostgreSQL"


# --- integrity, on real signed rows -----------------------------------------


def test_the_signature_chain_verifies_on_stored_data(engine, seeded, deployment_key):
    """The chain is only meaningful over rows that survived a real round trip.

    SQLite stores the signature as a BLOB too, but it never exercised psycopg's
    bytea handling, and a signature that changes shape in transit verifies nowhere.
    """
    from sqlalchemy.orm import Session

    with Session(bind=engine) as session:
        building_id = session.execute(
            select(ReadingRow.building_id).limit(1)
        ).scalar_one()
        chain = read_chain(session, building_id)

    assert len(chain) > 10
    assert all(isinstance(row["sig"], bytes | memoryview) for row in chain)

    result = verify_chain(deployment_key, chain, expect_first_seq=chain[0]["seq"])
    assert bool(result), f"chain broken for {building_id}: {result.first_break}"
    assert result.rows_checked == len(chain)


# --- JSONB and the portfolio ------------------------------------------------


def test_footprints_round_trip_as_jsonb(engine):
    """JSONB, not JSON: the column type only exists on the PostgreSQL side."""
    from sqlalchemy.orm import Session

    with Session(bind=engine) as session:
        row = session.execute(
            select(BuildingRow).where(BuildingRow.footprint.is_not(None)).limit(1)
        ).scalars().first()

    if row is None:
        pytest.skip("no footprints imported")

    assert isinstance(row.footprint, dict)
    assert row.footprint.get("type") in {"Polygon", "MultiPolygon"}


def test_the_stored_portfolio_maps_onto_the_domain_model(engine):
    """The optimizer's input, read through the real database rather than a fixture."""
    from sqlalchemy.orm import Session

    with Session(bind=engine) as session:
        buildings = load_buildings(session)

    assert len(buildings) >= 2
    assert all(b.annual_kwh > 0 for b in buildings)
    assert len({b.district for b in buildings}) > 1


# --- constraints PostgreSQL enforces ----------------------------------------


def test_a_reading_for_an_unknown_building_is_rejected(session):
    """The foreign key, on the database that actually enforces it.

    SQLite ignores foreign keys unless `PRAGMA foreign_keys=ON`; that pragma is set
    in `db.py` precisely so the contract tests do not accept what PostgreSQL rejects.
    This is the other half of that check - proof the constraint really is on the
    server, not only in the pragma.

    Rolled back by the fixture, so nothing survives the test.
    """
    from sqlalchemy.exc import IntegrityError

    session.add(ReadingRow(
        building_id="no-such-building",          # building.id is VARCHAR(32)
        ts=datetime(2020, 1, 1, tzinfo=UTC),
        kw=1.0, source="test", seq=1, sig=b"\x00" * 32,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_readings_are_rejected_by_the_primary_key(session, seeded):
    """MQTT QoS 1 is at-least-once, so redelivery is normal rather than exceptional.

    The ingester relies on `ON CONFLICT DO NOTHING` for this. If the key stopped
    being (building_id, ts), redelivery would double-count consumption instead.
    """
    from sqlalchemy.exc import IntegrityError

    existing = session.execute(select(ReadingRow).limit(1)).scalars().one()
    session.add(ReadingRow(
        building_id=existing.building_id, ts=existing.ts,
        kw=existing.kw + 1.0, source="test", seq=existing.seq, sig=b"\x00" * 32,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_migrations_are_at_head(engine):
    """A schema drifted from the migrations is a deployment that cannot be rebuilt."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from gemp.paths import data_dir

    root = data_dir().parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()

    with engine.connect() as connection:
        applied = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()

    assert applied == head, f"database at {applied}, migrations at {head}"
