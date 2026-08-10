from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from app.errors import AemeathError, ErrorCode

T = TypeVar("T")
CursorKind = Literal["sessions", "messages"]

_MAX_CURSOR_LENGTH = 2048
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_CURSOR_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_SESSION_ORDER = "effective_updated_at_desc_id_desc"
_MESSAGE_ORDER = "created_at_desc_id_desc"
_CURSOR_DOMAIN = "aemeath.session.cursor.v1"
_FILTER_DOMAIN = "aemeath.session.cursor.filter.v1"
_ALLOWED_FIELDS = {"v", "kind", "order", "filter", "anchor_time", "anchor_id"}


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class CursorAnchor:
    anchor_time: datetime
    anchor_id: str


def encode_cursor(
    *,
    kind: CursorKind,
    filter_fingerprint: str,
    anchor_time: str | datetime,
    anchor_id: str,
) -> str:
    anchor_dt = parse_storage_datetime(anchor_time)
    payload = {
        "v": 1,
        "kind": kind,
        "order": order_for_kind(kind),
        "filter": filter_fingerprint,
        "anchor_time": format_cursor_datetime(anchor_dt),
        "anchor_id": anchor_id,
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")
    if len(encoded) > _MAX_CURSOR_LENGTH:
        _raise_invalid_cursor("encoded_too_long")
    return encoded


def decode_cursor(
    cursor: str | None,
    *,
    kind: CursorKind,
    filter_fingerprint: str,
) -> CursorAnchor | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        _raise_invalid_cursor("empty")
    if len(cursor) > _MAX_CURSOR_LENGTH:
        _raise_invalid_cursor("too_long")
    try:
        cursor.encode("ascii")
    except UnicodeEncodeError:
        _raise_invalid_cursor("non_ascii")
    if not _CURSOR_RE.fullmatch(cursor):
        _raise_invalid_cursor("invalid_base64url_charset")
    padding = "=" * (-len(cursor) % 4)
    try:
        decoded = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
    except (binascii.Error, ValueError):
        _raise_invalid_cursor("malformed_base64")
    try:
        payload = json.loads(decoded.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise_invalid_cursor("malformed_json")
    if not isinstance(payload, dict):
        _raise_invalid_cursor("not_object")
    if set(payload) != _ALLOWED_FIELDS:
        _raise_invalid_cursor("field_mismatch")
    version = payload["v"]
    payload_kind = payload["kind"]
    order = payload["order"]
    payload_filter = payload["filter"]
    anchor_id = payload["anchor_id"]
    anchor_time = payload["anchor_time"]
    if type(version) is not int:
        _raise_invalid_cursor("version_type")
    if not isinstance(payload_kind, str):
        _raise_invalid_cursor("kind_type")
    if not isinstance(order, str):
        _raise_invalid_cursor("order_type")
    if not isinstance(payload_filter, str) or not _FINGERPRINT_RE.fullmatch(payload_filter):
        _raise_invalid_cursor("filter_type")
    if not isinstance(anchor_id, str) or not anchor_id:
        _raise_invalid_cursor("anchor_id_invalid")
    if not isinstance(anchor_time, str):
        _raise_invalid_cursor("anchor_time_invalid")
    if version != 1:
        _raise_invalid_cursor("unsupported_version")
    if payload_kind != kind:
        _raise_invalid_cursor("kind_mismatch")
    if order != order_for_kind(kind):
        _raise_invalid_cursor("order_mismatch")
    if payload_filter != filter_fingerprint:
        _raise_invalid_cursor("filter_mismatch")
    parsed_time = parse_cursor_datetime(anchor_time)
    if _encode_payload(payload) != cursor:
        _raise_invalid_cursor("non_canonical")
    return CursorAnchor(anchor_time=parsed_time, anchor_id=anchor_id)


def fingerprint_filters(*, kind: CursorKind, values: dict[str, Any]) -> str:
    payload = {
        "domain": _FILTER_DOMAIN,
        "kind": kind,
        "values": values,
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def order_for_kind(kind: CursorKind) -> str:
    if kind == "sessions":
        return _SESSION_ORDER
    return _MESSAGE_ORDER


def _encode_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def parse_cursor_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not _CURSOR_TIME_RE.fullmatch(value):
        _raise_invalid_cursor("time_format")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _raise_invalid_cursor("time_value")
    if format_cursor_datetime(parsed) != value:
        _raise_invalid_cursor("time_non_canonical")
    return parsed


def format_cursor_datetime(value: str | datetime) -> str:
    parsed = parse_storage_datetime(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_storage_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.replace(" ", "T", 1)
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("storage_time_invalid") from exc
    else:
        raise TypeError("storage_time_invalid_type")
    if parsed.tzinfo is None:
        raise ValueError("storage_time_naive")
    return parsed.astimezone(timezone.utc)


def format_storage_datetime(value: datetime) -> str:
    parsed = parse_storage_datetime(value)
    return parsed.isoformat()


def _raise_invalid_cursor(reason: str) -> None:
    raise AemeathError(
        code=ErrorCode.INVALID_INPUT,
        public_message="Invalid cursor.",
        internal_message=f"invalid_cursor:{reason}",
        metadata={"reason": reason},
    )
