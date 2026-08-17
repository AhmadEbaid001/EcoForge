"""The hardware seam, end to end: publish -> broker -> ingester -> signed row.

Everything else tests the write path by calling `flush()` directly. That covers the
signing and the deduplication and misses the part that actually breaks in the field:
credentials, topic shape, QoS, callback wiring, and whether a message published by
something that is not the simulator arrives at all. The proposal's claim is that an
ESP32 publishing the same shape to the same topic is indistinguishable from the
simulator - this is the test that makes that claim checkable.

    GEMP_MQTT_HOST=127.0.0.1 pytest -m integration

**Nothing here touches the production database or the production topic.** Rows land
in a temporary SQLite file, and the publish goes to a test-only topic prefix that the
running ingester does not subscribe to. Publishing on the real topic would inject a
fabricated reading into the demonstration data, signed and chained, which is the last
thing an integrity story needs.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gemp.db import Base, ReadingRow
from gemp.ingest.integrity import GENESIS, verify_chain
from gemp.repository import import_portfolio

pytestmark = pytest.mark.integration

CONNECT_TIMEOUT_S = 5
INGEST_SECONDS = 6.0

# The running ingester subscribes to `{settings.mqtt_topic_prefix}/+`, which is
# `gemp/reading/+`. Publishing anywhere under this prefix instead keeps the test
# invisible to it.
TEST_TOPIC_PREFIX = "gemp-test/reading"


@pytest.fixture(scope="module")
def settings():
    from gemp.config import get_settings

    try:
        return get_settings()
    except Exception as exc:  # noqa: BLE001 - missing key is a skip, not a bug
        pytest.skip(f"settings unavailable: {exc}")


@pytest.fixture(scope="module")
def broker(settings):
    """A connected publisher, or a skip. Never a failure."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"gemp-test-pub-{uuid.uuid4().hex[:8]}",
        clean_session=True,
    )
    client.username_pw_set(settings.mqtt_user, settings.mqtt_password.get_secret_value())
    try:
        client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=30)
    except OSError as exc:
        pytest.skip(f"no broker at {settings.mqtt_host}:{settings.mqtt_port} ({exc})")

    client.loop_start()
    yield client
    client.loop_stop()
    client.disconnect()


@pytest.fixture
def sqlite_scope(tmp_path):
    """A throwaway database with the portfolio imported, so foreign keys hold."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ingest.db'}")
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

    return scope


def run_ingester(settings, sqlite_scope, tmp_path, seconds: float = INGEST_SECONDS):
    from gemp.ingest.consumer import Ingester

    test_settings = settings.model_copy(update={"mqtt_topic_prefix": TEST_TOPIC_PREFIX})
    return Ingester(
        test_settings,
        batch_rows=10,
        batch_seconds=0.5,
        session_factory=sqlite_scope,
        anchor_path=tmp_path / "anchor.jsonl",
        # A second consumer on a live broker MUST NOT claim the production identity:
        # MQTT ids are exclusive, so connecting as `gemp-ingest` would disconnect the
        # running ingester and stop ingestion for as long as this test ran.
        client_id=f"gemp-test-sub-{uuid.uuid4().hex[:8]}",
        clean_session=True,
    )


def publish(broker, building_id: str, ts: datetime, kw: float) -> None:
    info = broker.publish(
        f"{TEST_TOPIC_PREFIX}/{building_id}",
        json.dumps({
            "building_id": building_id,
            "ts": ts.isoformat(),
            "kw": kw,
            "source": "test",
        }),
        qos=1,
    )
    info.wait_for_publish(timeout=CONNECT_TIMEOUT_S)


def test_a_published_reading_arrives_signed_and_chained(settings, broker, sqlite_scope,
                                                        tmp_path):
    """The whole seam. If this passes, an ESP32 speaking the same JSON would work."""
    ingester = run_ingester(settings, sqlite_scope, tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    import threading

    thread = threading.Thread(target=ingester.run, kwargs={"max_seconds": INGEST_SECONDS})
    thread.start()
    time.sleep(1.5)                       # let it connect and subscribe before publishing

    for i in range(5):
        publish(broker, "b001", base + timedelta(minutes=15 * i), 40.0 + i)

    thread.join(timeout=INGEST_SECONDS + 10)
    assert not thread.is_alive(), "ingester did not stop within its time limit"

    with sqlite_scope() as session:
        rows = session.execute(
            select(ReadingRow).where(ReadingRow.building_id == "b001")
            .order_by(ReadingRow.seq)
        ).scalars().all()

    assert len(rows) == 5, f"expected 5 readings, got {len(rows)}"
    assert [r.seq for r in rows] == [0, 1, 2, 3, 4]
    assert all(r.source == "test" for r in rows)

    # Signed by the ingester on arrival, not by the publisher: the node is not
    # trusted to sign, which is the point of doing it here.
    chain = [
        {"building_id": r.building_id, "ts": r.ts, "kw": r.kw,
         "source": r.source, "seq": r.seq, "sig": r.sig}
        for r in rows
    ]
    assert verify_chain(settings.key_bytes, chain, GENESIS, expect_first_seq=0).ok


def test_a_redelivered_reading_is_not_written_twice(settings, broker, sqlite_scope,
                                                    tmp_path):
    """QoS 1 is at-least-once, so redelivery is normal rather than exceptional.

    Counting a repeated reading twice would inflate measured consumption, which is
    the number every saving estimate is derived from.
    """
    ingester = run_ingester(settings, sqlite_scope, tmp_path)
    ts = datetime(2026, 2, 1, tzinfo=UTC)

    import threading

    thread = threading.Thread(target=ingester.run, kwargs={"max_seconds": INGEST_SECONDS})
    thread.start()
    time.sleep(1.5)

    for _ in range(3):
        publish(broker, "b002", ts, 55.0)

    thread.join(timeout=INGEST_SECONDS + 10)

    with sqlite_scope() as session:
        rows = session.execute(
            select(ReadingRow).where(ReadingRow.building_id == "b002")
        ).scalars().all()

    assert len(rows) == 1
    assert ingester.duplicates >= 2


def test_a_malformed_publish_is_counted_not_fatal(settings, broker, sqlite_scope, tmp_path):
    """Real hardware sends garbage occasionally. The correct response is to count it."""
    ingester = run_ingester(settings, sqlite_scope, tmp_path)

    import threading

    thread = threading.Thread(target=ingester.run, kwargs={"max_seconds": INGEST_SECONDS})
    thread.start()
    time.sleep(1.5)

    broker.publish(f"{TEST_TOPIC_PREFIX}/b003", "not json at all", qos=1)
    broker.publish(f"{TEST_TOPIC_PREFIX}/b003", json.dumps({"building_id": "b003"}), qos=1)
    time.sleep(0.5)
    publish(broker, "b003", datetime(2026, 3, 1, tzinfo=UTC), 33.0)

    thread.join(timeout=INGEST_SECONDS + 10)

    with sqlite_scope() as session:
        rows = session.execute(
            select(ReadingRow).where(ReadingRow.building_id == "b003")
        ).scalars().all()

    assert ingester.malformed == 2
    assert len(rows) == 1, "a good reading after two bad ones must still land"
