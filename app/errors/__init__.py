from app.errors.base import AemeathError, ErrorInfo
from app.errors.codes import ErrorCode
from app.errors.http import http_status_for_error
from app.errors.sanitizer import redact_sensitive


def map_exception(exc: BaseException) -> ErrorInfo:
    """Import the mapper lazily so leaf infrastructure errors cannot form cycles."""
    from app.errors.mapper import map_exception as _map_exception

    return _map_exception(exc)

__all__ = [
    "AemeathError",
    "ErrorCode",
    "ErrorInfo",
    "http_status_for_error",
    "map_exception",
    "redact_sensitive",
]
