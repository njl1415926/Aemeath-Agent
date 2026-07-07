from __future__ import annotations

import socket
from io import BytesIO
from urllib import error

from app.auth.errors import (
    AuthAccountDisabledError,
    AuthInvalidCredentialsError,
    AuthRequiredError,
)
from app.errors import ErrorCode, http_status_for_error, map_exception, redact_sensitive
from app.memory.extractor import MemoryExtractionError
from app.permissions.errors import PermissionDeniedError
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionNotFoundError,
    SessionOwnershipError,
)


def test_session_errors_map_to_stable_codes():
    cases = [
        (SessionNotFoundError("missing"), ErrorCode.SESSION_NOT_FOUND, 404),
        (SessionOwnershipError("owner mismatch"), ErrorCode.SESSION_OWNERSHIP_DENIED, 403),
        (SessionEndedError("ended"), ErrorCode.SESSION_ENDED, 409),
        (SessionArchivedError("archived"), ErrorCode.SESSION_ARCHIVED, 409),
    ]

    for exc, expected_code, expected_status in cases:
        error_info = map_exception(exc)

        assert error_info.code == expected_code
        assert http_status_for_error(error_info) == expected_status


def test_auth_and_permission_errors_map_to_stable_codes():
    cases = [
        (AuthRequiredError(), ErrorCode.AUTH_REQUIRED, 401),
        (AuthInvalidCredentialsError(), ErrorCode.AUTH_INVALID_CREDENTIALS, 401),
        (AuthAccountDisabledError(), ErrorCode.AUTH_ACCOUNT_DISABLED, 403),
        (PermissionDeniedError(), ErrorCode.PERMISSION_DENIED, 403),
    ]

    for exc, expected_code, expected_status in cases:
        error_info = map_exception(exc)

        assert error_info.code == expected_code
        assert http_status_for_error(error_info) == expected_status


def test_model_http_errors_map_to_retry_policy():
    rate_limited = error.HTTPError(
        url="https://api.deepseek.com/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=BytesIO(b"rate limited"),
    )
    auth_failed = error.HTTPError(
        url="https://api.deepseek.com/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=BytesIO(b"api_key=real-secret"),
    )

    rate_info = map_exception(rate_limited)
    auth_info = map_exception(auth_failed)

    assert rate_info.code == ErrorCode.MODEL_RATE_LIMITED
    assert rate_info.retryable is True
    assert http_status_for_error(rate_info) == 429
    assert auth_info.code == ErrorCode.MODEL_AUTH_FAILED
    assert auth_info.retryable is False
    assert "real-secret" not in auth_info.to_log_dict()["internal_message"]


def test_url_timeout_maps_to_model_timeout():
    error_info = map_exception(error.URLError(socket.timeout("timed out")))

    assert error_info.code == ErrorCode.MODEL_TIMEOUT
    assert error_info.retryable is True
    assert http_status_for_error(error_info) == 504


def test_memory_extraction_errors_distinguish_source_and_model_output():
    source_info = map_exception(
        MemoryExtractionError(
            "memory extraction messages do not match the current user/session context"
        )
    )
    output_info = map_exception(MemoryExtractionError("invalid memory extraction JSON"))

    assert source_info.code == ErrorCode.MEMORY_INVALID_SOURCE
    assert http_status_for_error(source_info) == 403
    assert output_info.code == ErrorCode.MEMORY_INVALID_MODEL_OUTPUT
    assert http_status_for_error(output_info) == 502


def test_redact_sensitive_handles_nested_values_and_text():
    value = {
        "authorization": "Bearer abc123",
        "api-key": "hyphen-secret",
        "x-api-key": "x-hyphen-secret",
        "metadata": {
            "note": "token=abc123 should not leak",
            "header": "x-api-key=secret123 should not leak",
            "safe": "visible",
            "ordinary-key": "not sensitive",
        },
    }

    redacted = redact_sensitive(value)

    assert redacted["authorization"] == "[redacted]"
    assert redacted["api-key"] == "[redacted]"
    assert redacted["x-api-key"] == "[redacted]"
    assert redacted["metadata"]["note"] == "token=[redacted] should not leak"
    assert redacted["metadata"]["header"] == "x-api-key=[redacted] should not leak"
    assert redacted["metadata"]["safe"] == "visible"
    assert redacted["metadata"]["ordinary-key"] == "not sensitive"
