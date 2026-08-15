"""F5 - tamper-evident storage via a per-building HMAC hash chain.

The proposal signed each reading independently. That detects MODIFICATION of a
record but not REMOVAL of one: an adversary with database write access and no key
can delete inconvenient readings, and every remaining row still verifies perfectly.
In an energy-audit context - where the stated threat is that consumption figures are
quietly edited after collection to change a reported outcome - selective deletion
achieves the same result and is the easier attack.

Chaining each signature into the next closes that gap for the same cost per row:

    sig[n] = HMAC(key, sig[n-1] || canonical(row[n]))

Any modification, deletion or reordering inside the chain breaks every signature
after it. Carrying an explicit sequence number in the signed payload lets the
verifier say WHICH of those happened, rather than only that something did.

Two limits are stated deliberately rather than left implicit:

  * Truncation of the most recent rows still verifies, because there is nothing
    after the deleted tail to break. `integrity_checkpoint` closes that: the head
    signature and last sequence are anchored hourly to a file outside the database
    volume, so a truncated table contradicts the anchor.
  * This is not tamper-proofing against an adversary with host access, who can read
    the injected key and re-sign. Nor does it authenticate a reading at its point of
    origin - a compromised sensor publishing false values produces records that are
    correctly signed and factually wrong. Closing those needs per-device signing keys
    in hardware and replication to a separate machine, both outside this build.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# The chain's starting value. Any fixed constant works; a named one makes it obvious
# in a database dump that this row is a genesis row rather than a corrupted signature.
GENESIS = hashlib.sha256(b"gemp-chain-genesis-v1").digest()

# kW is serialised at fixed precision. Signing the repr of a float would make
# verification depend on platform float formatting, which fails silently and
# intermittently - the worst possible failure mode for an integrity layer.
KW_PRECISION = 4


class IntegrityError(ValueError):
    """Raised when a chain cannot be constructed, not when it fails to verify."""


@dataclass(frozen=True)
class Break:
    """Where and how a chain failed."""

    index: int
    ts: datetime
    seq: int
    reason: str          # "modified" | "deleted" | "reordered"

    def __str__(self) -> str:
        return f"{self.reason} at seq {self.seq} ({self.ts.isoformat()})"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    rows_checked: int
    first_break: Break | None = None

    def __bool__(self) -> bool:
        return self.ok


def canonical(row: dict[str, Any]) -> bytes:
    """Deterministic byte encoding of the signed fields of a reading.

    Only these five fields are covered. Anything added to the table later is
    explicitly NOT protected unless it is added here too - which is a decision to
    make consciously, so the field list is a literal rather than a loop over keys.
    """
    try:
        payload = {
            "building_id": str(row["building_id"]),
            "ts": epoch_seconds(row["ts"]),
            "kw": f"{float(row['kw']):.{KW_PRECISION}f}",
            "source": str(row["source"]),
            "seq": int(row["seq"]),
        }
    except KeyError as exc:
        raise IntegrityError(f"reading is missing signed field {exc.args[0]!r}") from exc

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def as_utc(ts: datetime | str) -> datetime:
    """Normalise to an aware UTC datetime. Naive values are assumed to be UTC.

    Everything the system stores is UTC, but not every driver returns it that way:
    SQLite drops the offset entirely, and a PostgreSQL session in a non-UTC timezone
    returns the same instant with a different offset.
    """
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def epoch_seconds(ts: datetime | str) -> int:
    """Timestamp as whole UTC seconds since the epoch.

    Signing an ISO string would make verification depend on how the driver chose to
    render the offset - the same failure mode as signing a float's repr, and just as
    silent. An integer instant has exactly one representation.
    """
    return int(as_utc(ts).timestamp())


def sign(key: bytes, row: dict[str, Any], prev_sig: bytes) -> bytes:
    """Signature for one row, bound to its predecessor."""
    return hmac.new(key, prev_sig + canonical(row), hashlib.sha256).digest()


def sign_chain(
    key: bytes, rows: Iterable[dict[str, Any]], start_sig: bytes = GENESIS
) -> list[bytes]:
    """Signatures for a run of rows. Returns one signature per row, in order."""
    signatures: list[bytes] = []
    prev = start_sig
    for row in rows:
        prev = sign(key, row, prev)
        signatures.append(prev)
    return signatures


def verify_chain(
    key: bytes,
    rows: Sequence[dict[str, Any]],
    start_sig: bytes = GENESIS,
    *,
    expect_first_seq: int | None = None,
) -> VerifyResult:
    """Walk a chain and report the first break, if any.

    `rows` must be ordered by sequence. Each row carries its stored `sig`.
    """
    prev = start_sig
    expected_seq = expect_first_seq

    for index, row in enumerate(rows):
        seq = int(row["seq"])

        if expected_seq is not None and seq != expected_seq:
            reason = "deleted" if seq > expected_seq else "reordered"
            return VerifyResult(
                ok=False,
                rows_checked=index,
                first_break=Break(index=index, ts=row["ts"], seq=seq, reason=reason),
            )

        expected = sign(key, row, prev)
        stored = row.get("sig")
        if stored is None:
            raise IntegrityError(f"row at seq {seq} has no stored signature")

        if not hmac.compare_digest(expected, bytes(stored)):
            return VerifyResult(
                ok=False,
                rows_checked=index,
                first_break=Break(index=index, ts=row["ts"], seq=seq, reason="modified"),
            )

        prev = bytes(stored)
        expected_seq = seq + 1

    return VerifyResult(ok=True, rows_checked=len(rows))


def verify_against_checkpoint(
    rows: Sequence[dict[str, Any]],
    checkpoint_seq: int,
    checkpoint_sig: bytes,
) -> VerifyResult:
    """Detect truncation of the chain's tail.

    A chain walk alone cannot see that the last thousand rows were deleted - there is
    nothing left to break. Comparing against an anchor written outside the database
    volume can: the anchor claims sequence N, the table stops at M < N.
    """
    if not rows:
        return VerifyResult(
            ok=checkpoint_seq < 0,
            rows_checked=0,
            first_break=None
            if checkpoint_seq < 0
            else Break(index=0, ts=datetime.min, seq=checkpoint_seq, reason="deleted"),
        )

    last = rows[-1]
    last_seq = int(last["seq"])

    if last_seq < checkpoint_seq:
        return VerifyResult(
            ok=False,
            rows_checked=len(rows),
            first_break=Break(
                index=len(rows) - 1, ts=last["ts"], seq=last_seq, reason="deleted"
            ),
        )

    if last_seq == checkpoint_seq and not hmac.compare_digest(
        bytes(last["sig"]), checkpoint_sig
    ):
        return VerifyResult(
            ok=False,
            rows_checked=len(rows),
            first_break=Break(
                index=len(rows) - 1, ts=last["ts"], seq=last_seq, reason="modified"
            ),
        )

    return VerifyResult(ok=True, rows_checked=len(rows))
