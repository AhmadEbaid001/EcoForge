"""MQTT to signed storage.

The ingester owns the hash chain, which is why it - not the publisher - assigns
sequence numbers. Three consequences follow, and all three are deliberate:

* A device cannot corrupt the chain by rebooting and restarting its counter.
* A duplicate delivery must be dropped BEFORE a sequence is allocated. MQTT QoS 1 is
  at-least-once, so redelivery is normal, and allocating a sequence to a row that
  then loses an ON CONFLICT race would leave a gap that the verifier correctly
  reports as a deletion. Per-building last-timestamp tracking prevents that.
* Sequence order is arrival order. Per building the stream is monotonic in time, so
  the two coincide; the primary key on (building_id, ts) is what actually enforces
  uniqueness.

    python -m gemp.ingest.consumer
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from sqlalchemy import text

from gemp.config import get_settings
from gemp.db import IntegrityCheckpointRow, ReadingRow, insert_ignore, session_scope
from gemp.ingest.integrity import GENESIS, as_utc, sign

log = logging.getLogger("gemp.ingest")

ANCHOR_PATH = Path("anchor") / "integrity_anchor.jsonl"
CHECKPOINT_EVERY_S = 60.0


@dataclass
class ChainState:
    """Head of one building's chain, kept in memory between batches."""

    last_seq: int = -1
    last_sig: bytes = GENESIS
    last_ts: datetime | None = None


class Ingester:
    def __init__(self, settings, batch_rows: int | None = None,
                 batch_seconds: float | None = None, session_factory=None,
                 anchor_path: Path | None = None):
        self.settings = settings
        self.key = settings.key_bytes
        self.batch_rows = batch_rows or settings.ingest_batch_rows
        self.batch_seconds = batch_seconds or settings.ingest_batch_seconds
        # Injectable so the write path can be integration-tested against SQLite with
        # no server and no broker running.
        self.session_factory = session_factory or session_scope
        self.anchor_path = anchor_path or ANCHOR_PATH

        self.queue: deque[dict[str, Any]] = deque()
        self.chains: dict[str, ChainState] = {}
        self.running = True
        self.written = 0
        self.duplicates = 0
        self.malformed = 0
        self._last_checkpoint = time.monotonic()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="gemp-ingest", clean_session=False
        )
        self.client.username_pw_set(
            settings.mqtt_user, settings.mqtt_password.get_secret_value()
        )
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # -- MQTT ----------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            topic = f"{self.settings.mqtt_topic_prefix}/+"
            client.subscribe(topic, qos=1)
            log.info("subscribed to %s", topic)
        else:
            log.error("broker refused connection: %s", reason_code)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload)
            self.queue.append({
                "building_id": str(payload["building_id"]),
                "ts": as_utc(payload["ts"]),
                "kw": float(payload["kw"]),
                "source": str(payload.get("source", "unknown")),
            })
        except (ValueError, KeyError, TypeError) as exc:
            # A malformed publish must not take the ingester down. Real hardware
            # sends garbage occasionally; the correct response is to count it.
            self.malformed += 1
            if self.malformed <= 5:
                log.warning("dropping malformed message on %s: %s", message.topic, exc)

    # -- chain state ---------------------------------------------------------

    def load_chain_heads(self) -> None:
        """Resume every building's chain from what is already stored."""
        # Deliberately not DISTINCT ON, which is PostgreSQL-only. The join form runs
        # on SQLite too, which is what lets the write path be integration-tested
        # without a server.
        with self.session_factory() as session:
            rows = session.execute(text("""
                SELECT r.building_id, r.seq, r.sig, r.ts
                FROM reading r
                JOIN (
                    SELECT building_id, MAX(seq) AS max_seq
                    FROM reading
                    GROUP BY building_id
                ) head
                  ON head.building_id = r.building_id AND head.max_seq = r.seq
            """)).all()

        for building_id, seq, sig, ts in rows:
            self.chains[building_id] = ChainState(
                last_seq=int(seq), last_sig=bytes(sig), last_ts=as_utc(ts)
            )
        log.info("resumed %d chains", len(self.chains))

    # -- write path ----------------------------------------------------------

    def flush(self) -> int:
        if not self.queue:
            return 0

        pending: list[dict[str, Any]] = []
        while self.queue and len(pending) < self.batch_rows:
            pending.append(self.queue.popleft())

        rows = []
        for reading in pending:
            state = self.chains.setdefault(reading["building_id"], ChainState())

            # Drop redelivered or out-of-order readings before allocating a sequence.
            if state.last_ts is not None and reading["ts"] <= state.last_ts:
                self.duplicates += 1
                continue

            seq = state.last_seq + 1
            signed = {**reading, "seq": seq}
            signature = sign(self.key, signed, state.last_sig)

            rows.append({**signed, "sig": signature})
            state.last_seq = seq
            state.last_sig = signature
            state.last_ts = reading["ts"]

        if not rows:
            return 0

        with self.session_factory() as session:
            session.execute(
                insert_ignore(ReadingRow, session.bind.dialect.name), rows
            )

        self.written += len(rows)
        return len(rows)

    def write_checkpoint(self) -> None:
        """Anchor every chain head, in the database AND outside its volume.

        The external copy is the part that matters: an adversary who truncates the
        reading table has to reach a second file on a different mount to stay
        consistent.
        """
        if not self.chains:
            return

        now = datetime.now(UTC).replace(microsecond=0)
        records = [
            {
                "building_id": building_id,
                "ts": now,
                "last_seq": state.last_seq,
                "head_sig": state.last_sig,
            }
            for building_id, state in self.chains.items()
            if state.last_seq >= 0
        ]
        if not records:
            return

        with self.session_factory() as session:
            session.execute(
                insert_ignore(IntegrityCheckpointRow, session.bind.dialect.name), records
            )

        self.anchor_path.parent.mkdir(parents=True, exist_ok=True)
        with self.anchor_path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps({
                    "building_id": record["building_id"],
                    "ts": record["ts"].isoformat(),
                    "last_seq": record["last_seq"],
                    "head_sig": record["head_sig"].hex(),
                }) + "\n")

        log.info("checkpointed %d chains", len(records))

    # -- lifecycle -----------------------------------------------------------

    def stop(self, *_args) -> None:
        self.running = False

    def run(self, max_seconds: float | None = None) -> int:
        self.load_chain_heads()
        self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self.client.loop_start()

        started = time.monotonic()
        last_flush = time.monotonic()

        while self.running:
            time.sleep(0.2)
            now = time.monotonic()

            if len(self.queue) >= self.batch_rows or (now - last_flush) >= self.batch_seconds:
                written = self.flush()
                last_flush = now
                if written:
                    log.debug("wrote %d rows (total %d)", written, self.written)

            if now - self._last_checkpoint >= CHECKPOINT_EVERY_S:
                self.write_checkpoint()
                self._last_checkpoint = now

            if max_seconds is not None and now - started >= max_seconds:
                self.running = False

        self.flush()
        self.write_checkpoint()
        self.client.loop_stop()
        self.client.disconnect()

        log.info("wrote %d rows, dropped %d duplicates, %d malformed",
                 self.written, self.duplicates, self.malformed)
        return self.written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s  %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=None,
                        help="run for this long then exit (for smoke tests)")
    args = parser.parse_args(argv)

    ingester = Ingester(get_settings())
    signal.signal(signal.SIGINT, ingester.stop)
    signal.signal(signal.SIGTERM, ingester.stop)

    try:
        ingester.run(args.seconds)
    except OSError as exc:
        log.error("ingester stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
