from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Literal

TokenKind = Literal["invitation", "password_reset", "web_session"]

_PREFIXES: dict[TokenKind, str] = {
    "invitation": "inv_",
    "password_reset": "rst_",
    "web_session": "wsa_",
}
_CONTEXTS: dict[TokenKind, bytes] = {
    "invitation": b"aemeath:invitation:v1\0",
    "password_reset": b"aemeath:password-reset:v1\0",
    "web_session": b"aemeath:web-session:v1\0",
}
_TOKEN_BODY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_token(kind: TokenKind) -> str:
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return f"{_PREFIXES[kind]}{body}"


def token_digest(raw_token: str, *, kind: TokenKind) -> str:
    token = validate_token_shape(raw_token, kind=kind)
    digest = hashlib.sha256(_CONTEXTS[kind] + token.encode("ascii")).hexdigest()
    return f"sha256:{digest}"


def validate_token_shape(raw_token: str, *, kind: TokenKind) -> str:
    if not isinstance(raw_token, str):
        raise ValueError("token_shape_invalid")
    prefix = _PREFIXES[kind]
    if len(raw_token) != len(prefix) + 43 or not raw_token.startswith(prefix):
        raise ValueError("token_shape_invalid")
    body = raw_token[len(prefix) :]
    if not _TOKEN_BODY_RE.fullmatch(body):
        raise ValueError("token_shape_invalid")
    return raw_token


def digest_matches(stored_digest: str, computed_digest: str) -> bool:
    return hmac.compare_digest(stored_digest, computed_digest)


def new_internal_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def web_session_correlation(web_session_id: str) -> str:
    return hashlib.sha256(
        b"aemeath:web-session-correlation:v1\0" + web_session_id.encode("ascii")
    ).hexdigest()
