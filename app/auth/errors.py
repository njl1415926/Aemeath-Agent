from __future__ import annotations

from app.errors import AemeathError, ErrorCode


class AuthError(AemeathError):
    """Base error for local auth failures."""


class AuthRequiredError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.AUTH_REQUIRED,
            public_message="Authentication is required.",
            internal_message="authentication credentials were not provided",
        )


class AuthInvalidCredentialsError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.AUTH_INVALID_CREDENTIALS,
            public_message="Invalid authentication credentials.",
            internal_message="authentication credentials were invalid",
        )


class AuthAccountDisabledError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.AUTH_ACCOUNT_DISABLED,
            public_message="Account is disabled.",
            internal_message="authenticated account is disabled",
        )
