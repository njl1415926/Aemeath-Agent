from __future__ import annotations

from app.errors import AemeathError, ErrorCode


class PermissionError(AemeathError):
    """Base error for permission failures."""


class PermissionDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED,
            public_message="Permission denied.",
            internal_message="owner-only permission check failed",
        )
