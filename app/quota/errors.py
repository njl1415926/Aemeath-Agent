from __future__ import annotations

from app.errors.base import AemeathError
from app.errors.codes import ErrorCode


class UsageQuotaExceededError(AemeathError):
    def __init__(self, *, retry_after_seconds: int, reason: str = "daily_limit_reached"):
        if (
            not isinstance(retry_after_seconds, int)
            or isinstance(retry_after_seconds, bool)
            or retry_after_seconds < 0
        ):
            raise ValueError("quota_retry_after_invalid")
        super().__init__(
            code=ErrorCode.USAGE_QUOTA_EXCEEDED,
            public_message="Daily usage limit reached. Please try again later.",
            internal_message=reason,
            retryable=True,
            metadata={
                "reason": reason,
                "retry_after_seconds": retry_after_seconds,
            },
        )
