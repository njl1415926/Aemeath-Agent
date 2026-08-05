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


class AuthCredentialConflictError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.AUTH_CREDENTIAL_CONFLICT,
            public_message="Conflicting authentication credentials.",
            internal_message="conflicting authentication credentials were provided",
        )


class AuthSessionExpiredError(AuthError):
    def __init__(self, *, reason: str = "expired") -> None:
        super().__init__(
            code=ErrorCode.AUTH_SESSION_EXPIRED,
            public_message="The authentication session is no longer valid. Sign in again.",
            internal_message=f"web_auth_session_expired:{reason}",
        )


class AuthSessionRevokedError(AuthError):
    def __init__(self, *, reason: str = "revoked") -> None:
        super().__init__(
            code=ErrorCode.AUTH_SESSION_REVOKED,
            public_message="The authentication session is no longer valid. Sign in again.",
            internal_message=f"web_auth_session_revoked:{reason}",
        )


class InvitationInvalidError(AuthError):
    def __init__(self, *, reason: str = "invalid") -> None:
        super().__init__(
            code=ErrorCode.INVITATION_INVALID_OR_EXPIRED,
            public_message="The invitation is invalid or expired.",
            internal_message=f"invitation_accept_failed:{reason}",
        )


class PasswordResetInvalidError(AuthError):
    def __init__(self, *, reason: str = "invalid") -> None:
        super().__init__(
            code=ErrorCode.PASSWORD_RESET_INVALID_OR_EXPIRED,
            public_message="The password reset is invalid or expired.",
            internal_message=f"password_reset_accept_failed:{reason}",
        )


class PasswordPolicyError(AuthError):
    def __init__(self, *, reason: str) -> None:
        super().__init__(
            code=ErrorCode.PASSWORD_POLICY_REJECTED,
            public_message="The password does not meet the password policy.",
            internal_message=f"password_policy_rejected:{reason}",
        )


class UsernameUnavailableError(AuthError):
    def __init__(self, *, reason: str = "unavailable") -> None:
        super().__init__(
            code=ErrorCode.USERNAME_UNAVAILABLE,
            public_message="The username is unavailable.",
            internal_message=f"username_unavailable:{reason}",
        )


class CsrfInvalidError(AuthError):
    def __init__(self, *, reason: str = "invalid") -> None:
        super().__init__(
            code=ErrorCode.CSRF_INVALID,
            public_message="Request verification failed.",
            internal_message=f"csrf_invalid:{reason}",
        )


class ContentTypeUnsupportedError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.CONTENT_TYPE_UNSUPPORTED,
            public_message="This endpoint requires application/json.",
            internal_message="web_unsafe_request_content_type_unsupported",
        )


class PasswordWorkBusyError(AuthError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.RUNTIME_BUSY,
            public_message="Password processing is busy. Please try again.",
            internal_message="password_work_gate_busy",
            retryable=True,
        )
