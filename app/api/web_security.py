from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from app.auth.errors import (
    AuthCredentialConflictError,
    ContentTypeUnsupportedError,
    CsrfInvalidError,
)
from app.config import Settings

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def enforce_web_request_provenance(*, request: Request, settings: Settings) -> None:
    if settings.api_auth_mode != "web_session":
        return
    authorization = request.headers.get("Authorization")
    cookie_count = _web_session_cookie_count(request)
    if cookie_count > 1:
        request.state.clear_web_session_cookie = True
        raise AuthCredentialConflictError()
    if authorization is not None:
        raise AuthCredentialConflictError()
    if not request.url.path.startswith("/v1/") or request.method in _SAFE_METHODS:
        return
    media_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ContentTypeUnsupportedError()
    target_origin = _canonical_origin(settings.web_public_origin, allow_path=False)
    origin = request.headers.get("Origin")
    if origin is not None:
        if origin == "null" or _canonical_origin(origin, allow_path=False) != target_origin:
            raise CsrfInvalidError(reason="origin_mismatch")
    else:
        referer = request.headers.get("Referer")
        if referer is None or _canonical_origin(referer, allow_path=True) != target_origin:
            raise CsrfInvalidError(reason="referer_mismatch")
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site.lower() in {"cross-site", "same-site"}:
        raise CsrfInvalidError(reason="fetch_site_rejected")


def _canonical_origin(value: str, *, allow_path: bool) -> tuple[str, str, int] | None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and bool(parsed.path))
        or (not allow_path and bool(parsed.query))
        or bool(parsed.fragment)
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _web_session_cookie_count(request: Request) -> int:
    count = 0
    for raw_header in request.headers.getlist("cookie"):
        for component in raw_header.split(";"):
            name, separator, _value = component.strip().partition("=")
            if separator and name == "__Host-aemeath_session":
                count += 1
    return count
