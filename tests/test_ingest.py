"""End-to-end ingestion: message in, signed row out, tampering detected.

Runs against SQLite so the whole write path is exercised with no database server and
no broker. That is not a compromise made for convenience - the container stack needs
hardware virtualisation, which is not available on every machine the team will build
on, and a pipeline that can only be tested on one laptop is a pipeline that stops
being tested.

What these tests do NOT cover, and which still needs the container stack: the
TimescaleDB hypertable and continuous aggregate, the Mosquitto transport itself, and
Compose orchestration.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gemp.config import Settings
from gemp.db import Base, BuildingRow, ReadingRow
from gemp.ingest.consumer import Ingester
from gemp.ingest.integrity import verify_against_checkpoint, verify_chain

T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
KEY_HEX = "ab" * 32


class FakeMessage:
    """Stands in for a paho MQTT message."""

    def __init__(self, payload: dict | bytes, topic: str = "gemp/reading/b001"):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


@pytest.fixture
def settings():
    return Settings(
        hmac_key=KEY_HEX,
        db_password="unused",
        mqtt_password="unused",
        ingest_batch_rows=1000,
        ingest_batch_seconds=0.01,
    )


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gemp.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    with factory() as session:
        session.add(BuildingRow(
            id="b001", code="TEST-001", name="Test", district="D1",
            lat=30.0, lon=31.7, floor_area_m2=1000.0, roof_area_m2=500.0,
            glazing_area_m2=100.0, roof_orientation="FLAT", hvac_type="chiller",
            hvac_age_yr=10, insulation_quality="fair", occupancy_pattern="office",
            annual_kwh=100_000.0, updated_at=T0,
        ))

    factory.engine = engine
    return factory


@pytest.fixture
def ingester(settings, session_factory, tmp_path):
    return Ingester(
        settings,
        session_factory=session_factory,
        anchor_path=tmp_path / "anchor" / "integrity_anchor.jsonl",
    )


def publish(ingester, count: int, start_index: int = 0, building: str = "b001"):
    for i in range(start_index, start_index + count):
        ingester._on_message(None, None, FakeMessage({
            "building_id": building,
            "ts": (T0 + timedelta(minutes=15 * i)).isoformat(),
            "kw": 40.0 + i,
            "source": "sim",
        }))


def stored_rows(session_factory, building: str = "b001") -> list[dict]:
    with session_factory() as session:
        rows = session.execute(
            select(ReadingRow).where(ReadingRow.building_id == building)
            .order_by(ReadingRow.seq)
        ).scalars().all()
        return [
            {"building_id": r.building_id, "ts": r.ts, "kw": r.kw,
             "source": r.source, "seq": r.seq, "sig": r.sig}
            for r in rows
        ]


# --- the happy path ---------------------------------------------------------


def test_published_readings_are_stored_and_signed(ingester, session_factory):
    publish(ingester, 10)
    assert ingester.flush() == 10

    rows = stored_rows(session_factory)
    assert len(rows) == 10
    assert [r["seq"] for r in rows] == list(range(10))
    assert all(len(r["sig"]) == 32 for r in rows)


def test_stored_chain_verifies_end_to_end(ingester, session_factory, settings):
    """The claim, exercised through the real write path rather than in isolation."""
    publish(ingester, 25)
    ingester.flush()

    result = verify_chain(settings.key_bytes, stored_rows(session_factory),
                          expect_first_seq=0)
    assert result.ok
    assert result.rows_checked == 25


def test_sequence_is_assigned_by_the_ingester_not_the_publisher(ingester, session_factory):
    """A device that reboots and restarts its counter must not corrupt the chain."""
    ingester._on_message(None, None, FakeMessage({
        "building_id": "b001", "ts": T0.isoformat(), "kw": 10.0,
        "source": "sim", "seq": 999999,           # publisher's claim, to be ignored
    }))
    ingester.flush()

    assert stored_rows(session_factory)[0]["seq"] == 0


# --- resilience -------------------------------------------------------------


def test_duplicate_delivery_is_dropped_without_burning_a_sequence(ingester, session_factory,
                                                                  settings):
    """MQTT QoS 1 is at-least-once, so redelivery is routine.

    The subtle failure this guards against: allocating a sequence to a duplicate and
    then losing the insert to a conflict would leave a gap that the verifier
    correctly - and misleadingly - reports as a deletion.
    """
    publish(ingester, 5)
    ingester.flush()
    publish(ingester, 5)                  # exact redelivery of the same five
    ingester.flush()

    rows = stored_rows(session_factory)
    assert len(rows) == 5
    assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4]
    assert ingester.duplicates == 5
    assert verify_chain(settings.key_bytes, rows, expect_first_seq=0).ok


def test_malformed_messages_are_counted_not_fatal(ingester, session_factory):
    ingester._on_message(None, None, FakeMessage(b"not json at all"))
    ingester._on_message(None, None, FakeMessage({"building_id": "b001"}))   # no ts/kw
    ingester._on_message(None, None, FakeMessage({
        "building_id": "b001", "ts": "not-a-timestamp", "kw": 1.0, "source": "sim",
    }))
    publish(ingester, 3)
    ingester.flush()

    assert ingester.malformed == 3
    assert len(stored_rows(session_factory)) == 3


def test_chain_resumes_across_a_restart(settings, session_factory, tmp_path):
    """The ingester is restarted by `restart: always` on any crash. The chain must
    continue rather than fork."""
    first = Ingester(settings, session_factory=session_factory,
                     anchor_path=tmp_path / "a.jsonl")
    publish(first, 8)
    first.flush()

    second = Ingester(settings, session_factory=session_factory,
                      anchor_path=tmp_path / "a.jsonl")
    second.load_chain_heads()
    publish(second, 7, start_index=8)
    second.flush()

    rows = stored_rows(session_factory)
    assert [r["seq"] for r in rows] == list(range(15))
    assert verify_chain(settings.key_bytes, rows, expect_first_seq=0).ok


def test_buildings_get_independent_chains(ingester, session_factory, settings):
    publish(ingester, 5, building="b001")
    ingester.flush()

    with session_factory() as session:
        session.add(BuildingRow(
            id="b002", code="TEST-002", name="Second", district="D1",
            lat=30.0, lon=31.7, floor_area_m2=1000.0, roof_area_m2=500.0,
            glazing_area_m2=100.0, roof_orientation="FLAT", hvac_type="split",
            hvac_age_yr=5, insulation_quality="good", occupancy_pattern="school",
            annual_kwh=50_000.0, updated_at=T0,
        ))
    publish(ingester, 5, building="b002")
    ingester.flush()

    for building in ("b001", "b002"):
        rows = stored_rows(session_factory, building)
        assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4]
        assert verify_chain(settings.key_bytes, rows, expect_first_seq=0).ok


# --- tamper detection, through the real storage layer -----------------------


def test_editing_a_stored_reading_is_detected(ingester, session_factory, settings):
    """Demo step: edit a row in the database, then verify."""
    publish(ingester, 12)
    ingester.flush()

    with session_factory() as session:
        row = session.get(ReadingRow, {"building_id": "b001",
                                       "ts": T0 + timedelta(minutes=15 * 5)})
        row.kw = 1.0

    result = verify_chain(settings.key_bytes, stored_rows(session_factory),
                          expect_first_seq=0)
    assert not result.ok
    assert result.first_break.reason == "modified"
    assert result.first_break.seq == 5


def test_deleting_a_stored_reading_is_detected(ingester, session_factory, settings):
    """The attack independent per-row signatures would have missed entirely."""
    publish(ingester, 12)
    ingester.flush()

    with session_factory() as session:
        row = session.get(ReadingRow, {"building_id": "b001",
                                       "ts": T0 + timedelta(minutes=15 * 5)})
        session.delete(row)

    result = verify_chain(settings.key_bytes, stored_rows(session_factory),
                          expect_first_seq=0)
    assert not result.ok
    assert result.first_break.reason == "deleted"


def test_truncating_the_tail_is_caught_by_the_checkpoint(ingester, session_factory,
                                                         settings):
    publish(ingester, 20)
    ingester.flush()
    ingester.write_checkpoint()

    head = ingester.chains["b001"]
    checkpoint_seq, checkpoint_sig = head.last_seq, head.last_sig

    with session_factory() as session:
        for row in session.execute(
            select(ReadingRow).where(ReadingRow.seq >= 15)
        ).scalars().all():
            session.delete(row)

    rows = stored_rows(session_factory)
    assert verify_chain(settings.key_bytes, rows, expect_first_seq=0).ok    # walk clean
    assert not verify_against_checkpoint(rows, checkpoint_seq, checkpoint_sig).ok


def test_checkpoint_is_mirrored_outside_the_database(ingester, session_factory):
    """The external anchor is the part that makes truncation detectable at all."""
    publish(ingester, 6)
    ingester.flush()
    ingester.write_checkpoint()

    assert ingester.anchor_path.exists()
    records = [json.loads(line) for line in
               ingester.anchor_path.read_text(encoding="utf-8").splitlines()]
    assert records
    assert records[-1]["building_id"] == "b001"
    assert records[-1]["last_seq"] == 5
    assert len(bytes.fromhex(records[-1]["head_sig"])) == 32
