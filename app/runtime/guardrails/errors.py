from __future__ import annotations

from app.errors import AemeathError, ErrorCode


class RuntimeBusyError(AemeathError):
    def __init__(self, reason: str, *, retry_after_seconds: int | None = None):
        metadata: dict[str, object] = {"reason": reason}
        if retry_after_seconds is not None:
            metadata["retry_after_seconds"] = retry_after_seconds
        super().__init__(
            code=ErrorCode.RUNTIME_BUSY,
            public_message="The service is busy. Please try again shortly.",
            internal_message=reason,
            retryable=True,
            metadata=metadata,
        )
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class ApiRateLimitedError(AemeathError):
    def __init__(self, reason: str, *, retry_after_seconds: int):
        super().__init__(
            code=ErrorCode.API_RATE_LIMITED,
            public_message="Too many requests. Please try again later.",
            internal_message=reason,
            retryable=True,
            metadata={
                "reason": reason,
                "retry_after_seconds": retry_after_seconds,
            },
        )
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
