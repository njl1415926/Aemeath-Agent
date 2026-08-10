from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

import pytest

from app.errors import AemeathError, ErrorCode
from app.session.pagination import (
    decode_cursor,
    encode_cursor,
    fingerprint_filters,
    format_cursor_datetime,
)


def _fingerprint() -> str:
    return fingerprint_filters(
        kind="sessions",
        values={"user_id": "user_1", "character_id": None, "status": "active"},
    )


def _cursor_payload(**overrides):
    payload = {
        "v": 1,
        "kind": "sessions",
        "order": "effective_updated_at_desc_id_desc",
        "filter": _fingerprint(),
        "anchor_time": "2026-08-07T01:02:03.000000Z",
        "anchor_id": "session_1",
    }
    payload.update(overrides)
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _encode_raw_json(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _assert_invalid(cursor: str) -> None:
    with pytest.raises(AemeathError) as exc_info:
        decode_cursor(cursor, kind="sessions", filter_fingerprint=_fingerprint())
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert exc_info.value.public_message == "Invalid cursor."


def test_cursor_round_trip_is_strict_ascii_base64url_and_bounded() -> None:
    cursor = encode_cursor(
        kind="sessions",
        filter_fingerprint=_fingerprint(),
        anchor_time=datetime(2026, 8, 7, 1, 2, 3, tzinfo=timezone.utc),
        anchor_id="session_1",
    )

    assert len(cursor) <= 2048
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
    decoded = decode_cursor(cursor, kind="sessions", filter_fingerprint=_fingerprint())
    assert decoded is not None
    assert format_cursor_datetime(decoded.anchor_time) == "2026-08-07T01:02:03.000000Z"
    assert decoded.anchor_id == "session_1"


def test_cursor_rejects_invalid_base64url_and_bool_version() -> None:
    valid = encode_cursor(
        kind="sessions",
        filter_fingerprint=_fingerprint(),
        anchor_time="2026-08-07T01:02:03+00:00",
        anchor_id="session_1",
    )

    _assert_invalid(valid + "!")
    _assert_invalid(_cursor_payload(v=True))
    _assert_invalid(valid + "\u00e9")


@pytest.mark.parametrize(
    "cursor",
    [
        _cursor_payload(extra="nope"),
        _cursor_payload(anchor_time={"nested": "nope"}),
        _cursor_payload(anchor_id=["nested"]),
        _cursor_payload(anchor_time="2026-08-07T01:02:03Z"),
        _cursor_payload(anchor_time="2026-08-07T01:02:03.000000+00:00"),
        _cursor_payload(filter="not-a-sha"),
    ],
)
def test_cursor_rejects_unknown_fields_wrong_types_and_illegal_times(cursor: str) -> None:
    _assert_invalid(cursor)


def test_cursor_rejects_non_canonical_json_duplicate_fields_and_pad_bits() -> None:
    payload = {
        "v": 1,
        "kind": "sessions",
        "order": "effective_updated_at_desc_id_desc",
        "filter": _fingerprint(),
        "anchor_time": "2026-08-07T01:02:03.000000Z",
        "anchor_id": "session_1",
    }
    noncanonical_order = json.dumps(payload, ensure_ascii=True, sort_keys=False, indent=2)
    duplicate_field = (
        '{"anchor_id":"ignored","anchor_id":"session_1","anchor_time":'
        '"2026-08-07T01:02:03.000000Z","filter":"'
        + _fingerprint()
        + '","kind":"sessions","order":"effective_updated_at_desc_id_desc","v":1}'
    )
    valid = _cursor_with_pad_bits()

    _assert_invalid(_encode_raw_json(noncanonical_order))
    _assert_invalid(_encode_raw_json(duplicate_field))
    _assert_invalid(_with_noncanonical_pad_bits(valid))


def _with_noncanonical_pad_bits(cursor: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    remainder = len(cursor) % 4
    assert remainder in {2, 3}
    last_value = alphabet.index(cursor[-1])
    if remainder == 2:
        mask = 0b110000
    else:
        mask = 0b111100
    significant = last_value & mask
    for index, candidate in enumerate(alphabet):
        if index != last_value and (index & mask) == significant:
            return cursor[:-1] + candidate
    raise AssertionError("could not build noncanonical cursor")


def _cursor_with_pad_bits() -> str:
    for index in range(20):
        cursor = _cursor_payload(anchor_id=f"session_{index}")
        if len(cursor) % 4 in {2, 3}:
            return cursor
    raise AssertionError("could not build cursor with pad bits")
