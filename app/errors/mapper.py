from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from urllib import error as url_error

from app.errors.base import AemeathError, ErrorInfo
from app.errors.codes import ErrorCode
from app.errors.sanitizer import sanitize_text
from app.memory.extractor import MemoryExtractionError
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionError,
    SessionMigrationError,
    SessionNotFoundError,
    SessionOwnershipError,
    SessionTransitionError,
)


def map_exception(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, AemeathError):
        return exc.to_error_info()
    if isinstance(exc, url_error.HTTPError):
        return _map_http_error(exc)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return _info(
            exc,
            code=ErrorCode.MODEL_TIMEOUT,
            public_message="The model request timed out. Please try again.",
            retryable=True,
        )
    if isinstance(exc, url_error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            return _info(
                exc,
                code=ErrorCode.MODEL_TIMEOUT,
                public_message="The model request timed out. Please try again.",
                retryable=True,
            )
        return _info(
            exc,
            code=ErrorCode.MODEL_UNAVAILABLE,
            public_message="The model service is temporarily unavailable.",
            retryable=True,
        )
    if isinstance(exc, json.JSONDecodeError):
        return _info(
            exc,
            code=ErrorCode.MODEL_INVALID_RESPONSE,
            public_message="The model returned an invalid response.",
        )
    if isinstance(exc, SessionError):
        return _map_session_error(exc)
    if isinstance(exc, MemoryExtractionError):
        return _map_memory_extraction_error(exc)
    if isinstance(exc, sqlite3.IntegrityError):
        return _info(
            exc,
            code=ErrorCode.STORAGE_CONSTRAINT_VIOLATION,
            public_message="The request conflicts with existing stored data.",
        )
    if isinstance(exc, sqlite3.Error):
        return _info(
            exc,
            code=ErrorCode.STORAGE_UNAVAILABLE,
            public_message="Storage is temporarily unavailable.",
            retryable=True,
        )
    if isinstance(exc, FileNotFoundError):
        return _map_file_not_found(exc)
    if isinstance(exc, KeyError):
        return _info(
            exc,
            code=ErrorCode.CHARACTER_NOT_FOUND,
            public_message="The requested character is not available.",
        )
    if isinstance(exc, ValueError):
        return _map_value_error(exc)
    return _info(
        exc,
        code=ErrorCode.INTERNAL_ERROR,
        public_message="The request failed due to an internal error.",
    )


def _map_http_error(exc: url_error.HTTPError) -> ErrorInfo:
    status = exc.code
    if status == 400:
        return _info(
            exc,
            code=ErrorCode.MODEL_BAD_REQUEST,
            public_message="The model request was rejected.",
        )
    if status in {401, 403}:
        return _info(
            exc,
            code=ErrorCode.MODEL_AUTH_FAILED,
            public_message="The model service rejected authentication.",
        )
    if status == 429:
        return _info(
            exc,
            code=ErrorCode.MODEL_RATE_LIMITED,
            public_message="The model service is rate limited. Please try again later.",
            retryable=True,
        )
    if status == 408:
        return _info(
            exc,
            code=ErrorCode.MODEL_TIMEOUT,
            public_message="The model request timed out. Please try again.",
            retryable=True,
        )
    if status in {500, 502, 503, 504}:
        return _info(
            exc,
            code=ErrorCode.MODEL_UNAVAILABLE,
            public_message="The model service is temporarily unavailable.",
            retryable=True,
        )
    return _info(
        exc,
        code=ErrorCode.MODEL_UNAVAILABLE,
        public_message="The model service returned an unexpected error.",
        retryable=500 <= status < 600,
    )


def _map_session_error(exc: SessionError) -> ErrorInfo:
    if isinstance(exc, SessionNotFoundError):
        return _info(exc, code=ErrorCode.SESSION_NOT_FOUND, public_message="Session not found.")
    if isinstance(exc, SessionOwnershipError):
        return _info(
            exc,
            code=ErrorCode.SESSION_OWNERSHIP_DENIED,
            public_message="Session is unavailable or access is denied.",
        )
    if isinstance(exc, SessionEndedError):
        return _info(
            exc,
            code=ErrorCode.SESSION_ENDED,
            public_message="Session has ended. Resume it before sending messages.",
        )
    if isinstance(exc, SessionArchivedError):
        return _info(
            exc,
            code=ErrorCode.SESSION_ARCHIVED,
            public_message="Session is archived and cannot be used.",
        )
    if isinstance(exc, SessionTransitionError):
        return _info(
            exc,
            code=ErrorCode.SESSION_INVALID_TRANSITION,
            public_message="Session lifecycle transition is not allowed.",
        )
    if isinstance(exc, SessionMigrationError):
        return _info(
            exc,
            code=ErrorCode.SESSION_MIGRATION_FAILED,
            public_message="Session migration failed.",
        )
    return _info(exc, code=ErrorCode.INVALID_INPUT, public_message="Session command failed.")


def _map_memory_extraction_error(exc: MemoryExtractionError) -> ErrorInfo:
    message = str(exc).lower()
    if "do not match" in message or "current context" in message:
        return _info(
            exc,
            code=ErrorCode.MEMORY_INVALID_SOURCE,
            public_message="Memory source does not match the current session context.",
        )
    if "json" in message or "memories" in message or "memory item" in message:
        return _info(
            exc,
            code=ErrorCode.MEMORY_INVALID_MODEL_OUTPUT,
            public_message="Memory extraction returned an invalid model response.",
        )
    return _info(
        exc,
        code=ErrorCode.MEMORY_EXTRACTION_FAILED,
        public_message="Memory extraction failed.",
    )


def _map_file_not_found(exc: FileNotFoundError) -> ErrorInfo:
    message = str(exc)
    if "character" in message.lower():
        return _info(
            exc,
            code=ErrorCode.CHARACTER_LOAD_FAILED,
            public_message="Character data could not be loaded.",
        )
    return _info(
        exc,
        code=ErrorCode.INVALID_INPUT,
        public_message="Requested file was not found.",
        metadata={"path": _safe_missing_path(exc)},
    )


def _map_value_error(exc: ValueError) -> ErrorInfo:
    message = str(exc)
    lowered = message.lower()
    if "api_key" in lowered or "openai_api_key" in lowered:
        return _info(
            exc,
            code=ErrorCode.CONFIG_MISSING_SECRET,
            public_message="Required model credentials are missing.",
        )
    if "character" in lowered and (
        "manifest" in lowered
        or "card" in lowered
        or "component" in lowered
        or "schema" in lowered
    ):
        return _info(
            exc,
            code=ErrorCode.CHARACTER_INVALID_MANIFEST,
            public_message="Character data is invalid.",
        )
    if "aemeath_memory_embedding_provider" in lowered:
        return _info(
            exc,
            code=ErrorCode.CONFIG_INVALID,
            public_message="Configuration value is invalid.",
        )
    if "memory" in lowered and (
        "source message" in lowered
        or "verified session_id" in lowered
        or "current context" in lowered
    ):
        return _info(
            exc,
            code=ErrorCode.MEMORY_INVALID_SOURCE,
            public_message="Memory source does not match the current session context.",
        )
    return _info(exc, code=ErrorCode.INVALID_INPUT, public_message="Invalid input.")


def _info(
    exc: BaseException,
    *,
    code: ErrorCode,
    public_message: str,
    retryable: bool = False,
    metadata: dict | None = None,
) -> ErrorInfo:
    return ErrorInfo(
        code=code,
        public_message=public_message,
        internal_message=sanitize_text(str(exc)),
        error_type=type(exc).__name__,
        retryable=retryable,
        metadata=metadata or {},
    )


def _safe_missing_path(exc: FileNotFoundError) -> str | None:
    filename = getattr(exc, "filename", None)
    if filename is None:
        return None
    return Path(filename).name
