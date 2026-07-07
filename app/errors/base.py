from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.errors.codes import ErrorCode
from app.errors.sanitizer import redact_sensitive, sanitize_text


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    public_message: str
    internal_message: str
    error_type: str
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.public_message,
            "retryable": self.retryable,
        }

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.public_message,
            "internal_message": sanitize_text(self.internal_message),
            "error_type": self.error_type,
            "retryable": self.retryable,
            "metadata": redact_sensitive(self.metadata),
        }


class AemeathError(RuntimeError):
    def __init__(
        self,
        *,
        code: ErrorCode,
        public_message: str,
        internal_message: str | None = None,
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
    ):
        super().__init__(internal_message or public_message)
        self.code = code
        self.public_message = public_message
        self.internal_message = internal_message or public_message
        self.retryable = retryable
        self.metadata = metadata or {}

    def to_error_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            public_message=self.public_message,
            internal_message=self.internal_message,
            error_type=type(self).__name__,
            retryable=self.retryable,
            metadata=self.metadata,
        )
