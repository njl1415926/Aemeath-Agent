from __future__ import annotations

from app.errors.base import ErrorInfo
from app.errors.codes import ErrorCode

_HTTP_STATUS_BY_CODE = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_ACCOUNT_DISABLED: 403,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.CONFIG_INVALID: 500,
    ErrorCode.CONFIG_MISSING_SECRET: 500,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.SESSION_OWNERSHIP_DENIED: 403,
    ErrorCode.SESSION_ENDED: 409,
    ErrorCode.SESSION_ARCHIVED: 409,
    ErrorCode.SESSION_INVALID_TRANSITION: 409,
    ErrorCode.SESSION_MIGRATION_FAILED: 500,
    ErrorCode.CHARACTER_NOT_FOUND: 404,
    ErrorCode.CHARACTER_LOAD_FAILED: 500,
    ErrorCode.CHARACTER_INVALID_MANIFEST: 500,
    ErrorCode.MEMORY_INVALID_SOURCE: 403,
    ErrorCode.MEMORY_EXTRACTION_FAILED: 502,
    ErrorCode.MEMORY_INVALID_MODEL_OUTPUT: 502,
    ErrorCode.MODEL_BAD_REQUEST: 502,
    ErrorCode.MODEL_AUTH_FAILED: 502,
    ErrorCode.MODEL_RATE_LIMITED: 429,
    ErrorCode.MODEL_TIMEOUT: 504,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.MODEL_INVALID_RESPONSE: 502,
    ErrorCode.STORAGE_UNAVAILABLE: 503,
    ErrorCode.STORAGE_MIGRATION_FAILED: 500,
    ErrorCode.STORAGE_CONSTRAINT_VIOLATION: 409,
}


def http_status_for_error(error_info: ErrorInfo) -> int:
    return _HTTP_STATUS_BY_CODE.get(error_info.code, 500)
