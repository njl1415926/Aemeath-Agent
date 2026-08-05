from __future__ import annotations

import base64
import hashlib
import hmac


def derive_csrf_token(*, key: bytes, web_session_id: str, verifier: bytes) -> str:
    if len(key) < 32:
        raise ValueError("csrf_key_too_short")
    if len(verifier) != 32:
        raise ValueError("csrf_verifier_invalid")
    digest = hmac.new(
        key,
        b"aemeath:csrf:v1\0" + web_session_id.encode("ascii") + b"\0" + verifier,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def csrf_token_matches(*, expected: str, provided: str | None) -> bool:
    if provided is None or len(provided) != 43:
        return False
    return hmac.compare_digest(expected, provided)
