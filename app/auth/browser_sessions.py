from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.auth.errors import (
    AuthInvalidCredentialsError,
    AuthSessionExpiredError,
    AuthSessionRevokedError,
    PasswordPolicyError,
)
from app.auth.passwords import (
    PasswordEngine,
    legacy_username_is_browser_eligible,
    validate_browser_username,
    validate_password,
)
from app.auth.ports import BrowserSessionStorePort
from app.auth.schemas import (
    AuthenticatedBrowserSession,
    BrowserLoginResult,
    SessionMaterial,
)
from app.auth.tokens import (
    new_internal_id,
    new_token,
    token_digest,
    web_session_correlation,
)
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.services.schemas import UserContext


class BrowserSessionAuthService:
    def __init__(
        self,
        *,
        store: BrowserSessionStorePort,
        passwords: PasswordEngine,
        app_logger: AppLoggerPort,
        idle_seconds: int = 604_800,
        absolute_seconds: int = 2_592_000,
        touch_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ):
        if idle_seconds <= 0 or absolute_seconds < idle_seconds:
            raise ValueError("web_session_timeout_invalid")
        if touch_seconds <= 0 or touch_seconds >= idle_seconds:
            raise ValueError("web_session_touch_invalid")
        self.store = store
        self.passwords = passwords
        self.app_logger = app_logger
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.touch_seconds = touch_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def login(
        self, *, username: str, password: str, request_id: str | None = None
    ) -> BrowserLoginResult:
        try:
            normalized = validate_browser_username(username)
            validate_password(password)
        except (ValueError, PasswordPolicyError):
            self._log_failure("input_invalid", request_id=request_id)
            raise AuthInvalidCredentialsError() from None
        identity = self.store.get_login_identity(username_normalized=normalized)
        if (
            identity is None
            or identity.account.status != "active"
            or not legacy_username_is_browser_eligible(identity.account.username)
        ):
            self.passwords.verify_dummy(password)
            if identity is not None and not legacy_username_is_browser_eligible(
                identity.account.username
            ):
                reason = "browser_username_ineligible"
            elif identity is None:
                reason = "identity_unavailable"
            else:
                reason = "account_disabled"
            self._log_failure(
                reason,
                request_id=request_id,
            )
            raise AuthInvalidCredentialsError()
        if not self.passwords.verify_password(identity.password_hash, password):
            self._log_failure("password_mismatch", request_id=request_id)
            raise AuthInvalidCredentialsError()
        replacement_hash = None
        if self.passwords.check_needs_rehash(identity.password_hash):
            replacement_hash = self.passwords.hash_password(password)
        material = self.new_session_material()
        session = self.store.create_login_session(
            account_id=identity.account.id,
            expected_password_hash=identity.password_hash,
            expected_credential_version=identity.credential_version,
            replacement_password_hash=replacement_hash,
            material=material,
        )
        if session is None:
            self._log_failure("credential_changed", request_id=request_id)
            raise AuthInvalidCredentialsError()
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.login_completed",
                request_id=request_id,
                user_id=identity.account.id,
                payload={
                    "status": "completed",
                    "web_session_correlation": web_session_correlation(session.id),
                },
            ),
        )
        return BrowserLoginResult(identity.account, session, material.raw_token)

    def resolve_session_token(
        self, *, token: str | None, request_id: str | None = None
    ) -> AuthenticatedBrowserSession:
        if token is None:
            raise AuthSessionRevokedError(reason="missing")
        try:
            digest = token_digest(token, kind="web_session")
        except ValueError:
            raise AuthSessionRevokedError(reason="malformed") from None
        state = self.store.resolve_session(token_digest=digest)
        if state is None:
            raise AuthSessionRevokedError(reason="unknown")
        session = state.session
        now_dt = self._now()
        now = _format_time(now_dt)
        if session.revoked_at is not None:
            raise AuthSessionRevokedError(reason="revoked")
        if now >= session.absolute_expires_at or now >= session.idle_expires_at:
            self.store.revoke_session(session_id=session.id, reason="expired", now=now)
            raise AuthSessionExpiredError()
        if state.account.status != "active":
            self.store.revoke_session(session_id=session.id, reason="account_disabled", now=now)
            raise AuthSessionRevokedError(reason="account_disabled")
        if (
            state.current_credential_version is None
            or session.authenticated_credential_version != state.current_credential_version
        ):
            self.store.revoke_session(
                session_id=session.id, reason="credential_version_mismatch", now=now
            )
            raise AuthSessionRevokedError(reason="credential_version_mismatch")
        last_seen = _parse_time(session.last_seen_at)
        if (now_dt - last_seen).total_seconds() >= self.touch_seconds:
            idle_deadline = min(
                now_dt + timedelta(seconds=self.idle_seconds),
                _parse_time(session.absolute_expires_at),
            )
            self.store.touch_session(
                session_id=session.id,
                observed_last_seen=session.last_seen_at,
                now=now,
                idle_expires_at=_format_time(idle_deadline),
            )
        correlation = web_session_correlation(session.id)
        return AuthenticatedBrowserSession(
            user_context=UserContext(
                user_id=state.account.id,
                roles=list(state.account.roles),
                request_id=request_id,
                metadata={"auth_method": "web_session"},
            ),
            account=state.account,
            web_session_id=session.id,
            csrf_verifier=state.csrf_verifier,
            correlation_digest=correlation,
        )

    def logout(self, *, web_session_id: str) -> None:
        self.store.revoke_session(
            session_id=web_session_id,
            reason="logout",
            now=_format_time(self._now()),
        )

    def change_password(
        self,
        *,
        authenticated: AuthenticatedBrowserSession,
        current_password: str,
        new_password: str,
        request_id: str | None = None,
    ) -> BrowserLoginResult:
        identity = self.store.get_login_identity(
            username_normalized=authenticated.account.username_normalized
        )
        if identity is None or identity.account.id != authenticated.account.id:
            raise AuthInvalidCredentialsError()
        try:
            validate_password(current_password)
        except PasswordPolicyError:
            raise AuthInvalidCredentialsError() from None
        if not self.passwords.verify_password(identity.password_hash, current_password):
            raise AuthInvalidCredentialsError()
        validate_password(new_password)
        if self.passwords.verify_password(identity.password_hash, new_password):
            raise PasswordPolicyError(reason="password_unchanged")
        new_hash = self.passwords.hash_password(new_password)
        material = self.new_session_material()
        session = self.store.change_password_and_rotate_session(
            account_id=identity.account.id,
            expected_password_hash=identity.password_hash,
            expected_credential_version=identity.credential_version,
            new_password_hash=new_hash,
            material=material,
            now=material.created_at,
        )
        if session is None:
            raise AuthInvalidCredentialsError()
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.password_changed",
                request_id=request_id,
                user_id=identity.account.id,
                payload={
                    "status": "completed",
                    "web_session_correlation": web_session_correlation(session.id),
                },
            ),
        )
        return BrowserLoginResult(identity.account, session, material.raw_token)

    def new_session_material(self) -> SessionMaterial:
        now_dt = self._now()
        now = _format_time(now_dt)
        raw_token = new_token("web_session")
        return SessionMaterial(
            session_id=new_internal_id("wsa"),
            raw_token=raw_token,
            token_digest=token_digest(raw_token, kind="web_session"),
            csrf_verifier=secrets.token_bytes(32),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=_format_time(now_dt + timedelta(seconds=self.idle_seconds)),
            absolute_expires_at=_format_time(now_dt + timedelta(seconds=self.absolute_seconds)),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("auth_clock_must_be_timezone_aware")
        return value.astimezone(timezone.utc)

    def _log_failure(self, reason: str, *, request_id: str | None) -> None:
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.login_failed",
                level="warning",
                request_id=request_id,
                error_code="AUTH_INVALID_CREDENTIALS",
                payload={"status": "failed", "reason": reason},
            ),
        )


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("stored_auth_timestamp_missing_timezone")
    return parsed.astimezone(timezone.utc)


def _record_best_effort(app_logger: AppLoggerPort, record: AppLogRecord) -> None:
    try:
        app_logger.record(record)
    except Exception:
        return None
