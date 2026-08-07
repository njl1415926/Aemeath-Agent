from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any

FINGERPRINT_VERSION_V1 = 1
_FINGERPRINT_DOMAIN_V1 = b"AEMEATH_CHAT_REQUEST_FINGERPRINT_V1\0"


def fingerprint_chat_request_v1(
    *,
    character_id: str,
    message: str,
    metadata: dict[str, Any],
) -> str:
    payload = {
        "character_id": character_id,
        "message": message,
        "metadata": metadata,
    }
    canonical_tree = _canonical_node(payload)
    encoded = json.dumps(
        canonical_tree,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_FINGERPRINT_DOMAIN_V1 + encoded).hexdigest()


def _canonical_node(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("stream_fingerprint_float_nonfinite")
        return ["float64", struct.pack(">d", value).hex()]
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("stream_fingerprint_string_utf8_invalid") from exc
        return ["str", value]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_node(item) for item in value]]
    if isinstance(value, dict):
        items: list[tuple[str, Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("stream_fingerprint_object_key_invalid")
            try:
                key_bytes = key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("stream_fingerprint_object_key_utf8_invalid") from exc
            items.append((key, (key_bytes, _canonical_node(item))))
        items.sort(key=lambda entry: entry[1][0])
        return ["object", [[key, node] for key, (_, node) in items]]
    raise ValueError("stream_fingerprint_value_invalid")
