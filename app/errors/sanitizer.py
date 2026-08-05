from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "x_api_key",
    "authorization",
    "cookie",
    "csrf",
    "digest",
    "password",
    "secret",
    "token",
    "verifier",
)

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", flags=re.I)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(x[_-]?api[_-]?key|api[_-]?key|authorization|cookie|csrf|digest|password|secret|token|verifier)\b"
    r"\s*[:=]\s*[^,\s;]+"
)


def is_sensitive_key(key: Any) -> bool:
    key_text = str(key).lower()
    return any(part in key_text for part in SENSITIVE_KEY_PARTS)


def sanitize_text(text: str) -> str:
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                safe[key] = "[redacted]"
            else:
                safe[key] = redact_sensitive(item)
        return safe
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value
