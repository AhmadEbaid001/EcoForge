"""Reading the external integrity anchor (F5).

The anchor is the only part of the integrity story that an adversary with write
access to the database cannot reach. `integrity_checkpoint` lives inside the
PostgreSQL volume: anyone able to truncate `reading` can truncate that table too,
and a chain walk over what remains still verifies - there is nothing after the
deleted tail to break. The file on a separate mount is what makes truncation
detectable at all.

Which means the file has to be READ, not merely written. It was written from Phase 1
and read by nothing until Phase 4: `/api/v1/integrity/verify` returned
`checkpoint_ok: null` on every call, so the demonstration proved tamper detection for
modified rows and quietly proved nothing at all for deleted ones.

    from gemp.ingest.anchor import latest_checkpoint
    latest_checkpoint("b001")
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("gemp.ingest.anchor")

# Matches `Ingester.anchor_path`. Overridable so a test never touches the real one.
DEFAULT_ANCHOR_PATH = Path("anchor") / "integrity_anchor.jsonl"

# Checkpoints are appended for every active chain on every round, so the newest entry
# for any one building is close to the end of the file. Reading a tail slice keeps a
# verify request cheap as the file grows through a long demonstration; a full scan is
# the fallback when the building is not in the slice.
TAIL_BYTES = 512 * 1024


@dataclass(frozen=True)
class Checkpoint:
    building_id: str
    ts: datetime
    last_seq: int
    head_sig: bytes


def anchor_path() -> Path:
    override = os.environ.get("GEMP_ANCHOR_PATH")
    return Path(override) if override else DEFAULT_ANCHOR_PATH


def _parse(line: str) -> Checkpoint | None:
    try:
        record = json.loads(line)
        return Checkpoint(
            building_id=record["building_id"],
            ts=datetime.fromisoformat(record["ts"]),
            last_seq=int(record["last_seq"]),
            head_sig=bytes.fromhex(record["head_sig"]),
        )
    except (ValueError, KeyError, TypeError):
        # A half-written final line is normal for an append-only log that is being
        # written while this reads it. Skipping it is correct; failing is not.
        return None


def _scan(lines: list[str], building_id: str) -> Checkpoint | None:
    best: Checkpoint | None = None
    for line in lines:
        line = line.strip()
        if not line or building_id not in line:
            continue
        checkpoint = _parse(line)
        if checkpoint is None or checkpoint.building_id != building_id:
            continue
        if best is None or checkpoint.last_seq > best.last_seq:
            best = checkpoint
    return best


def latest_checkpoint(building_id: str, path: Path | None = None) -> Checkpoint | None:
    """Newest anchored chain head for one building, or None if never anchored."""
    path = path or anchor_path()
    if not path.exists():
        log.warning("no integrity anchor at %s; truncation cannot be detected", path)
        return None

    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()          # discard the partial line the seek landed inside
        found = _scan(fh.readlines(), building_id)

    if found is not None or size <= TAIL_BYTES:
        return found

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return _scan(fh.readlines(), building_id)
