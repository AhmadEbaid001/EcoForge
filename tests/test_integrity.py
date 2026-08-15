"""F5 - the integrity chain must catch modification, deletion, reordering and truncation.

These are the assertions behind the claim made to judges. Every one of them
corresponds to something demonstrable live in psql.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gemp.ingest.integrity import (
    GENESIS,
    IntegrityError,
    canonical,
    sign,
    sign_chain,
    verify_against_checkpoint,
    verify_chain,
)

KEY = bytes.fromhex("00" * 32)
OTHER_KEY = bytes.fromhex("11" * 32)
T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def make_rows(count: int = 10, building: str = "b001") -> list[dict]:
    return [
        {
            "building_id": building,
            "ts": T0 + timedelta(minutes=15 * i),
            "kw": 40.0 + i,
            "source": "sim",
            "seq": i,
        }
        for i in range(count)
    ]


def signed(rows: list[dict], key: bytes = KEY) -> list[dict]:
    for row, signature in zip(rows, sign_chain(key, rows), strict=True):
        row["sig"] = signature
    return rows


# --- canonical encoding -----------------------------------------------------


def test_canonical_is_stable_across_float_representations():
    """Signing a float's repr would make verification depend on platform formatting -
    a silent, intermittent failure, which is the worst kind for an integrity layer."""
    a = {"building_id": "b1", "ts": T0, "kw": 0.1 + 0.2, "source": "sim", "seq": 1}
    b = {"building_id": "b1", "ts": T0, "kw": 0.3, "source": "sim", "seq": 1}
    assert canonical(a) == canonical(b)


def test_canonical_rejects_a_row_missing_a_signed_field():
    with pytest.raises(IntegrityError, match="missing signed field"):
        canonical({"building_id": "b1", "ts": T0, "kw": 1.0, "source": "sim"})


def test_canonical_is_order_independent_for_dict_keys():
    a = {"building_id": "b1", "ts": T0, "kw": 1.0, "source": "sim", "seq": 1}
    b = {"seq": 1, "source": "sim", "kw": 1.0, "ts": T0, "building_id": "b1"}
    assert canonical(a) == canonical(b)


# --- happy path -------------------------------------------------------------


def test_intact_chain_verifies():
    rows = signed(make_rows())
    result = verify_chain(KEY, rows, expect_first_seq=0)
    assert result.ok
    assert result.rows_checked == 10
    assert result.first_break is None


def test_each_signature_depends_on_its_predecessor():
    rows = make_rows(3)
    sigs = sign_chain(KEY, rows)
    # Same row, different predecessor -> different signature.
    assert sign(KEY, rows[1], GENESIS) != sigs[1]


def test_wrong_key_fails_immediately():
    rows = signed(make_rows())
    result = verify_chain(OTHER_KEY, rows, expect_first_seq=0)
    assert not result.ok
    assert result.first_break.index == 0


# --- the three attacks ------------------------------------------------------


def test_modified_reading_is_caught():
    """The attack the proposal's per-row signing already covered."""
    rows = signed(make_rows())
    rows[4]["kw"] = 1.0                      # quietly reduce a consumption figure

    result = verify_chain(KEY, rows, expect_first_seq=0)
    assert not result.ok
    assert result.first_break.reason == "modified"
    assert result.first_break.seq == 4


def test_deleted_reading_is_caught():
    """The attack per-row signing MISSES, and the reason for chaining.

    With independent signatures every surviving row still verifies and the deletion
    is invisible.
    """
    rows = signed(make_rows())
    del rows[4]

    result = verify_chain(KEY, rows, expect_first_seq=0)
    assert not result.ok
    assert result.first_break.reason == "deleted"
    assert result.first_break.seq == 5       # the gap is visible at the next row


def test_reordered_readings_are_caught():
    rows = signed(make_rows())
    rows[3], rows[6] = rows[6], rows[3]

    result = verify_chain(KEY, rows, expect_first_seq=0)
    assert not result.ok
    assert result.first_break.reason in {"deleted", "reordered"}


def test_break_is_reported_at_its_first_occurrence():
    rows = signed(make_rows())
    rows[7]["kw"] = 0.0
    rows[2]["kw"] = 0.0

    result = verify_chain(KEY, rows, expect_first_seq=0)
    assert result.first_break.seq == 2


def test_resigning_a_modified_row_still_breaks_the_chain():
    """An adversary with write access but no key cannot repair the chain.

    Even given the signing function, re-signing row 4 needs row 3's signature as
    input and changes row 4's signature, which invalidates row 5 onward - so the
    forgery has to run to the end of the table, and the checkpoint anchor catches
    that.
    """
    rows = signed(make_rows())
    rows[4]["kw"] = 1.0
    rows[4]["sig"] = sign(OTHER_KEY, rows[4], rows[3]["sig"])   # no key: wrong signature

    result = verify_chain(KEY, rows, expect_first_seq=0)
    assert not result.ok
    assert result.first_break.seq == 4


# --- truncation, via the external anchor ------------------------------------


def test_truncation_passes_a_chain_walk_but_fails_the_checkpoint():
    """Chaining alone cannot see a deleted tail - nothing is left to break."""
    rows = signed(make_rows(10))
    checkpoint_seq = rows[-1]["seq"]
    checkpoint_sig = rows[-1]["sig"]

    truncated = rows[:6]

    assert verify_chain(KEY, truncated, expect_first_seq=0).ok      # walk is clean
    result = verify_against_checkpoint(truncated, checkpoint_seq, checkpoint_sig)
    assert not result.ok
    assert result.first_break.reason == "deleted"


def test_checkpoint_accepts_a_chain_that_has_grown():
    rows = signed(make_rows(10))
    result = verify_against_checkpoint(rows, rows[5]["seq"], rows[5]["sig"])
    assert result.ok


def test_checkpoint_catches_a_modified_head():
    rows = signed(make_rows(10))
    checkpoint_sig = rows[-1]["sig"]
    rows[-1]["sig"] = b"\x00" * 32

    result = verify_against_checkpoint(rows, rows[-1]["seq"], checkpoint_sig)
    assert not result.ok
    assert result.first_break.reason == "modified"


def test_emptied_table_fails_the_checkpoint():
    result = verify_against_checkpoint([], checkpoint_seq=99, checkpoint_sig=b"\x00" * 32)
    assert not result.ok


# --- chains are per building ------------------------------------------------


def test_chains_of_different_buildings_are_independent():
    a = signed(make_rows(5, building="b001"))
    b = signed(make_rows(5, building="b002"))

    assert verify_chain(KEY, a, expect_first_seq=0).ok
    assert verify_chain(KEY, b, expect_first_seq=0).ok
    assert a[0]["sig"] != b[0]["sig"]        # building_id is inside the signed payload
