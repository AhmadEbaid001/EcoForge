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
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from gemp import paths
from gemp.config import get_settings
from gemp.db import (
    AnomalyRow,
    BuildingRow,
    IntegrityCheckpointRow,
    ReadingRow,
    insert_ignore,
    session_scope,
)
from gemp.domain.catalog import load_params
from gemp.ingest.integrity import GENESIS, as_utc, sign
from gemp.ingest.live_anomaly import StreamingDetector
from gemp.ingest.webhook import Notification, Webhook

log = logging.getLogger("gemp.ingest")

CHECKPOINT_EVERY_S = 60.0

# How many readings may wait in memory for the database to come back.
#
# The queue used to be unbounded, which was survivable only because a failing write
# killed the ingester outright. Now that a write failure is retried, an outage means
# readings accumulate for its whole duration - fifty buildings at 720x replay is
# roughly 200 readings a second, so an hour down is three quarters of a million dicts.
#
# At the cap the OLDEST readings are discarded rather than the newest. Both lose data
# and there is no third option; keeping the newest means that when the database
# returns, the dashboard and the detector resume from the present rather than
# replaying an hour of history nobody is waiting for. Discards are counted, and the
# gap is visible in the stored timestamps either way.
MAX_QUEUED_READINGS = 100_000


@dataclass
class ChainState:
    """Head of one building's chain, kept in memory between batches."""

    last_seq: int = -1
    last_sig: bytes = GENESIS
    last_ts: datetime | None = None


class Ingester:
    def __init__(self, settings, batch_rows: int | None = None,
                 batch_seconds: float | None = None, session_factory=None,
                 anchor_path: Path | None = None,
                 client_id: str = "gemp-ingest", clean_session: bool = False,
                 webhook=None):
        """`client_id` and `clean_session` are parameters for one specific reason.

        MQTT identities are exclusive: a second client connecting with an id that is
        already in use disconnects the first. Anything that runs a second Ingester
        against a live broker - an integration test, a debugging session - would
        silently kick the production ingester off and stop ingestion for as long as it
        ran. A durable session (`clean_session=False`) also leaves a subscription
        queueing messages on the broker after it exits, so a throwaway client must not
        ask for one.

        The defaults are the production values; only a caller that knows it is a
        second consumer should change them.
        """
        self.settings = settings
        self.key = settings.key_bytes
        self.batch_rows = batch_rows or settings.ingest_batch_rows
        self.batch_seconds = batch_seconds or settings.ingest_batch_seconds
        # Injectable so the write path can be integration-tested against SQLite with
        # no server and no broker running. Default resolves through paths - the same
        # function the reader uses - so writer and reader cannot disagree about
        # where the anchor lives.
        self.session_factory = session_factory or session_scope
        self.anchor_path = anchor_path or paths.integrity_anchor_path()

        # Anomalies are scored as readings arrive, not only in the nightly batch:
        # a fault found the next morning has already burned a night of energy, and
        # during a demonstration nothing would appear on the map at all.
        self.detector = StreamingDetector(k=load_params().anomaly_k)
        self.webhook = webhook or Webhook(
            settings.webhook_url, settings.webhook_min_severity
        )

        self.queue: deque[dict[str, Any]] = deque(maxlen=MAX_QUEUED_READINGS)
        self.chains: dict[str, ChainState] = {}
        self.running = True
        self.written = 0
        self.anomalies = 0
        self.duplicates = 0
        self.malformed = 0
        # Consecutive failures of the write path. Reset by the first success, so a
        # non-zero value means ingestion is failing RIGHT NOW rather than that it
        # once did.
        self.write_failures = 0
        self.last_error = ""
        # Readings discarded because the queue was full while the database was away.
        self.overflowed = 0
        # Readings the database refused outright - an unknown building, most likely.
        self.rejected = 0
        self._last_checkpoint = time.monotonic()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=clean_session
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
            # A full deque evicts silently on append, so count the loss here - a
            # reading that vanished without a number attached to it is the kind of
            # gap that gets explained away as "the simulator must have paused".
            if len(self.queue) == MAX_QUEUED_READINGS:
                self.overflowed += 1
                if self.overflowed == 1 or self.overflowed % 10_000 == 0:
                    log.error("ingest queue full at %d readings; discarding the "
                              "oldest (%d so far)", MAX_QUEUED_READINGS, self.overflowed)
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

    def warm_detector(self) -> None:
        """Give the streaming detector its history before the first live reading."""
        with self.session_factory() as session:
            self.detector.warm_from_db(session)

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

        try:
            written, detected, duplicates = self._write(pending)
        except IntegrityError as exc:
            # A constraint violation is DETERMINISTIC: retrying it produces the same
            # violation forever. `reading.building_id` is a foreign key, so a single
            # reading for a building the portfolio does not contain - a stale
            # simulator, or the physical node F11 describes arriving before its
            # building row - would otherwise be requeued at the FRONT of the queue and
            # retried on every pass, blocking every valid reading behind it. Ingestion
            # would be permanently dead while the thread stayed alive.
            #
            # The offending readings are isolated rather than the batch discarded: at
            # 500 rows a batch, dropping all of them to be rid of one would throw away
            # 499 good readings, and would keep doing it for as long as whatever is
            # publishing the unknown id carries on.
            written, detected, duplicates = self._write_without_unknown(pending, exc)
            if written == 0 and not detected:
                return 0
        except Exception:
            # Anything else is treated as transient - a dropped connection, a
            # restarting database. The batch has already left the queue, so putting it
            # back is the whole difference between "retried on the next pass" and
            # "silently lost". Chain state was not touched - see `_write` - so the
            # retry re-signs from the same head and produces the same rows.
            self.queue.extendleft(reversed(pending))
            raise

        self.duplicates += duplicates

        # After the commit, never before. A notification for a fault that then failed
        # to store would send someone looking for a row that does not exist, and the
        # webhook is fire-and-forget so there is no taking it back.
        for anomaly in detected:
            self.webhook.notify(Notification(
                building_id=anomaly.building_id,
                ts=anomaly.ts,
                observed_kw=anomaly.observed_kw,
                expected_kw=anomaly.expected_kw,
                robust_z=anomaly.robust_z,
                severity=anomaly.severity,
                kind=anomaly.kind,
            ))

        self.written += written
        self.anomalies += len(detected)
        return written

    def _write_without_unknown(
        self, pending: list[dict[str, Any]], exc: IntegrityError
    ) -> tuple[int, list, int]:
        """Retry a refused batch with the readings the portfolio cannot accept removed.

        Only foreign-key failures can be isolated this way, because only they have an
        identifiable culprit: a building_id with no row in `building`. Asking which
        ids exist costs one query on a path that is already exceptional.

        Anything still failing after that is genuinely undiagnosable from here, so the
        batch is dropped rather than retried forever - which is the behaviour this
        whole branch exists to prevent.
        """
        try:
            with self.session_factory() as session:
                known = {
                    row[0] for row in session.execute(
                        select(BuildingRow.id).where(
                            BuildingRow.id.in_({r["building_id"] for r in pending})
                        )
                    ).all()
                }
        except Exception:  # noqa: BLE001 - fall back to dropping the batch
            known = set()

        usable = [r for r in pending if r["building_id"] in known]
        unknown_ids = sorted({r["building_id"] for r in pending} - known)
        refused = len(pending) - len(usable)

        if refused:
            self.rejected += refused
            log.error(
                "%d reading(s) refer to buildings the portfolio does not contain and "
                "have been dropped (%d total). Unknown ids: %s. Add the building rows; "
                "restarting will not help.",
                refused, self.rejected, ", ".join(unknown_ids[:5]),
            )

        if not usable:
            if not refused:
                # The violation was something other than an unknown building, so there
                # is nothing here to isolate.
                self.rejected += len(pending)
                log.error("database refused a batch of %d readings and it has been "
                          "dropped (%d total): %s",
                          len(pending), self.rejected, exc.orig or exc)
            return 0, [], 0

        try:
            return self._write(usable)
        except IntegrityError as second:
            self.rejected += len(usable)
            log.error("batch still refused after removing %d unknown-building "
                      "reading(s); dropped %d more (%d total): %s",
                      refused, len(usable), self.rejected, second.orig or second)
            return 0, [], 0

    def _write(self, pending: list[dict[str, Any]]) -> tuple[int, list, int]:
        """Sign and store one batch. Chain state advances only if the commit lands.

        Two things had to move for the chain to be safe, and both are about the gap
        between deciding a sequence number and the row actually existing.

        **Signing happens inside the write transaction, after asking what is stored.**
        The insert is ON CONFLICT DO NOTHING, so a row whose (building_id, ts) already
        exists is discarded by the database without complaint - while the in-memory
        chain had already consumed a sequence for it and signed the NEXT reading
        against its signature. One discarded row left a permanent hole: seq 10, 12,
        13, with 12 chained to a signature that was never stored, which `verify_chain`
        reports as tampering. That is the worst possible false alarm for the one
        feature whose entire purpose is to be believed. The last-timestamp check
        catches redelivery; only the database catches a row already sitting ahead of
        this ingester's head, left by a second writer or by a restart that read its
        heads mid-commit.

        **Chain state is advanced only after the commit returns.** It used to be
        mutated while building the batch, so a failed write left `last_seq` and
        `last_sig` describing rows that do not exist - and every later reading chained
        onto a phantom. Now the new heads are held aside and applied at the end, which
        is also what makes requeueing the batch in `flush` correct rather than a way
        to write it twice.
        """
        duplicates = 0
        heads: dict[str, tuple[int, bytes, datetime]] = {}
        rows: list[dict[str, Any]] = []

        # Scoring mutates the detector's rolling statistics, and it has to happen
        # before the commit because the anomaly rows go in the same transaction as
        # the readings. If the commit fails the batch is requeued and scored again,
        # so the first pass must leave no trace - the same rule the chain heads
        # below already follow.
        with self.detector.rollback_on_error(), self.session_factory() as session:
            dialect = session.bind.dialect.name
            stored = self._already_stored(session, pending)

            for reading in pending:
                building_id = reading["building_id"]
                state = self.chains.setdefault(building_id, ChainState())
                last_seq, last_sig, last_ts = heads.get(
                    building_id, (state.last_seq, state.last_sig, state.last_ts)
                )

                # Redelivered, out of order, or already on disk.
                if last_ts is not None and reading["ts"] <= last_ts:
                    duplicates += 1
                    continue
                if (building_id, reading["ts"]) in stored:
                    duplicates += 1
                    continue

                seq = last_seq + 1
                signed = {**reading, "seq": seq}
                signature = sign(self.key, signed, last_sig)

                rows.append({**signed, "sig": signature})
                heads[building_id] = (seq, signature, reading["ts"])

            if not rows:
                return 0, [], duplicates

            detected = [
                found
                for found in (
                    self.detector.score(r["building_id"], r["ts"], r["kw"]) for r in rows
                )
                if found is not None
            ]

            session.execute(insert_ignore(ReadingRow, dialect), rows)

            if detected:
                # insert_ignore because the nightly batch may already have flagged the
                # same (building, timestamp); one fault is one row either way.
                session.execute(insert_ignore(AnomalyRow, dialect), [
                    {
                        "building_id": a.building_id,
                        "ts": a.ts,
                        "observed_kw": a.observed_kw,
                        "expected_kw": a.expected_kw,
                        "residual": a.residual,
                        "robust_z": a.robust_z,
                        "severity": a.severity,
                        "acknowledged": False,
                    }
                    for a in detected
                ])

        # Committed. Only now is it true that these rows exist.
        for building_id, (seq, signature, ts) in heads.items():
            state = self.chains[building_id]
            state.last_seq, state.last_sig, state.last_ts = seq, signature, ts

        return len(rows), detected, duplicates

    def _already_stored(self, session, pending: list[dict[str, Any]]) -> set:
        """Which (building_id, ts) pairs in this batch the table already holds.

        Filtered by building and by timestamp separately rather than by tuple: row
        comparisons are supported unevenly across dialects, and this write path is
        integration-tested on SQLite as well as PostgreSQL. The batch is bounded by
        `ingest_batch_rows`, so the over-broad WHERE reads a handful of rows more
        than strictly needed and stays one round trip.
        """
        if not pending:
            return set()

        buildings = {r["building_id"] for r in pending}
        stamps = {r["ts"] for r in pending}
        rows = session.execute(
            select(ReadingRow.building_id, ReadingRow.ts).where(
                ReadingRow.building_id.in_(buildings),
                ReadingRow.ts.in_(stamps),
            )
        ).all()
        return {(building_id, as_utc(ts)) for building_id, ts in rows}

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
        self.warm_detector()
        self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self.client.loop_start()

        started = time.monotonic()
        last_flush = time.monotonic()

        while self.running:
            time.sleep(0.2)
            now = time.monotonic()

            # Every database touch below is wrapped, and the reason is worth stating
            # because the failure it prevents is invisible.
            #
            # This runs as a DAEMON THREAD inside the API process. An exception that
            # escapes here does not crash anything: it kills this thread and leaves
            # the API serving normally. /health goes on reporting `api: ok` and
            # `database: ok`, the container healthcheck goes on passing, and the only
            # symptom is that `readings` stops advancing - which also freezes the
            # simulator, since resume_point() reads that field. Ingestion would be
            # dead until somebody restarted the process, with nothing anywhere saying
            # so. One dropped connection while TimescaleDB restarts is enough.
            #
            # A failed flush leaves the batch in the queue, so the next pass retries
            # it. The chain is unharmed: `flush` advances chain state only for rows it
            # is about to write, inside the same call that writes them.
            try:
                if len(self.queue) >= self.batch_rows or (now - last_flush) >= self.batch_seconds:
                    written = self.flush()
                    last_flush = now
                    self.write_failures = 0
                    if written:
                        log.debug("wrote %d rows (total %d)", written, self.written)

                if now - self._last_checkpoint >= CHECKPOINT_EVERY_S:
                    self.write_checkpoint()
                    self._last_checkpoint = now
            except Exception as exc:  # noqa: BLE001 - a dead ingester is worse
                self.write_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                last_flush = now
                # Loud on the first failure and then every fiftieth, so a database
                # that stays down does not bury everything else in the log.
                if self.write_failures == 1 or self.write_failures % 50 == 0:
                    log.error("ingest write failed (%d in a row), %d readings queued: %s",
                              self.write_failures, len(self.queue), exc)

            if max_seconds is not None and now - started >= max_seconds:
                self.running = False

        # Shutdown: still best-effort, for the same reason. A failure here must not
        # stop the broker being disconnected or the webhook worker being joined.
        for final in (self.flush, self.write_checkpoint):
            try:
                final()
            except Exception as exc:  # noqa: BLE001
                log.error("%s failed during shutdown: %s", final.__name__, exc)
        self.client.loop_stop()
        self.client.disconnect()
        self.webhook.stop()

        log.info("wrote %d rows, flagged %d anomalies, dropped %d duplicates, %d malformed",
                 self.written, self.anomalies, self.duplicates, self.malformed)
        if self.webhook.enabled:
            log.info("webhook: %d sent, %d failed, %d dropped, %d below severity",
                     self.webhook.sent, self.webhook.failed,
                     self.webhook.dropped, self.webhook.suppressed)
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
