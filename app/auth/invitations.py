from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from app.auth.browser_sessions import BrowserSessionAuthService, _format_time
from app.auth.errors import (
    InvitationInvalidError,
    PasswordResetInvalidError,
    UsernameUnavailableError,
)
from app.auth.passwords import PasswordEngine, validate_browser_username
from app.auth.ports import InvitationStorePort, PasswordResetStorePort
from app.auth.schemas import BrowserLoginResult, IssuedInvitation, IssuedPasswordReset
from app.auth.tokens import new_internal_id, new_token, token_digest
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord


class InvitationService:
    def __init__(
        self,
        *,
        store: InvitationStorePort,
        passwords: PasswordEngine,
        sessions: BrowserSessionAuthService,
        app_logger: AppLoggerPort,
        public_origin: str,
        lifetime_seconds: int = 86_400,
    ):
        if not 3_600 <= lifetime_seconds <= 604_800:
            raise ValueError("invitation_ttl_invalid")
        _validate_public_origin(public_origin)
        self.store = store
        self.passwords = passwords
        self.sessions = sessions
        self.app_logger = app_logger
        self.public_origin = public_origin
        self.lifetime_seconds = lifetime_seconds

    def issue(
        self, *, username: str, display_name: str | None, roles: list[str]
    ) -> IssuedInvitation:
        normalized = validate_browser_username(username)
        if roles != ["user"]:
            raise ValueError("invitation_roles_invalid")
        _validate_display_name(display_name)
        now_dt = self.sessions._now()
        now = _format_time(now_dt)
        raw_token = new_token("invitation")
        record = self.store.issue_invitation(
            invitation_id=new_internal_id("inv"),
            token_digest=token_digest(raw_token, kind="invitation"),
            username=normalized,
            display_name=display_name,
            roles=roles,
            created_at=now,
            expires_at=_format_time(now_dt + timedelta(seconds=self.lifetime_seconds)),
        )
        if record is None:
            raise UsernameUnavailableError()
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.invitation_issued",
                payload={"status": "completed", "invitation_id": record.id},
            ),
        )
        return IssuedInvitation(
            record=record,
            raw_token=raw_token,
            invitation_url=f"{self.public_origin}/invite#token={raw_token}",
        )

    def accept(
        self, *, raw_token: str, password: str, request_id: str | None = None
    ) -> BrowserLoginResult:
        try:
            digest = token_digest(raw_token, kind="invitation")
        except ValueError:
            self._log_failure("token_invalid", request_id=request_id)
            raise InvitationInvalidError() from None
        now = _format_time(self.sessions._now())
        preflight = self.store.preflight_invitation(token_digest=digest, now=now)
        if preflight is None:
            self._log_failure("candidate_unavailable", request_id=request_id)
            raise InvitationInvalidError()
        password_hash = self.passwords.hash_password(password)
        material = self.sessions.new_session_material()
        outcome, account, session = self.store.accept_invitation(
            invitation_id=preflight.invitation_id,
            token_digest=digest,
            password_hash=password_hash,
            account_id=new_internal_id("acct"),
            material=material,
            now=material.created_at,
        )
        if outcome != "accepted" or account is None or session is None:
            self._log_failure(outcome, request_id=request_id)
            raise InvitationInvalidError(reason=outcome)
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.invitation_accept_completed",
                request_id=request_id,
                user_id=account.id,
                payload={
                    "status": "completed",
                    "invitation_id": preflight.invitation_id,
                },
            ),
        )
        return BrowserLoginResult(account, session, material.raw_token)

    def _log_failure(self, reason: str, *, request_id: str | None) -> None:
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.invitation_accept_failed",
                level="warning",
                request_id=request_id,
                error_code="INVITATION_INVALID_OR_EXPIRED",
                payload={"status": "failed", "reason": reason},
            ),
        )


class PasswordResetService:
    def __init__(
        self,
        *,
        store: PasswordResetStorePort,
        passwords: PasswordEngine,
        sessions: BrowserSessionAuthService,
        app_logger: AppLoggerPort,
        public_origin: str,
        lifetime_seconds: int = 3_600,
    ):
        if not 300 <= lifetime_seconds <= 86_400:
            raise ValueError("password_reset_ttl_invalid")
        _validate_public_origin(public_origin)
        self.store = store
        self.passwords = passwords
        self.sessions = sessions
        self.app_logger = app_logger
        self.public_origin = public_origin
        self.lifetime_seconds = lifetime_seconds

    def issue(self, *, account_selector: str) -> IssuedPasswordReset:
        now_dt = self.sessions._now()
        now = _format_time(now_dt)
        raw_token = new_token("password_reset")
        record = self.store.issue_password_reset(
            account_selector=account_selector,
            reset_id=new_internal_id("rst"),
            token_digest=token_digest(raw_token, kind="password_reset"),
            created_at=now,
            expires_at=_format_time(now_dt + timedelta(seconds=self.lifetime_seconds)),
        )
        if record is None:
            raise PasswordResetInvalidError(reason="account_unavailable")
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.password_reset_issued",
                user_id=record.account_id,
                payload={"status": "completed", "password_reset_id": record.id},
            ),
        )
        return IssuedPasswordReset(
            record=record,
            raw_token=raw_token,
            reset_url=f"{self.public_origin}/reset-password#token={raw_token}",
        )

    def accept(
        self, *, raw_token: str, new_password: str, request_id: str | None = None
    ) -> BrowserLoginResult:
        try:
            digest = token_digest(raw_token, kind="password_reset")
        except ValueError:
            self._log_reset_failure("token_invalid", request_id=request_id)
            raise PasswordResetInvalidError() from None
        now = _format_time(self.sessions._now())
        preflight = self.store.preflight_password_reset(token_digest=digest, now=now)
        if preflight is None:
            self._log_reset_failure("candidate_unavailable", request_id=request_id)
            raise PasswordResetInvalidError()
        password_hash = self.passwords.hash_password(new_password)
        material = self.sessions.new_session_material()
        accepted, account, session = self.store.accept_password_reset(
            reset_id=preflight.reset_id,
            token_digest=digest,
            password_hash=password_hash,
            material=material,
            now=material.created_at,
        )
        if not accepted or account is None or session is None:
            self._log_reset_failure("transaction_revalidation_failed", request_id=request_id)
            raise PasswordResetInvalidError()
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.password_reset_accepted",
                request_id=request_id,
                user_id=account.id,
                payload={
                    "status": "completed",
                    "password_reset_id": preflight.reset_id,
                },
            ),
        )
        return BrowserLoginResult(account, session, material.raw_token)

    def _log_reset_failure(self, reason: str, *, request_id: str | None) -> None:
        _record_best_effort(
            self.app_logger,
            AppLogRecord(
                event_name="auth.password_reset_accept_failed",
                level="warning",
                request_id=request_id,
                error_code="PASSWORD_RESET_INVALID_OR_EXPIRED",
                payload={"status": "failed", "reason": reason},
            ),
        )


def _validate_public_origin(origin: str) -> None:
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin.endswith("/")
    ):
        raise ValueError("web_public_origin_invalid")


def _validate_display_name(display_name: str | None) -> None:
    if display_name is None:
        return
    try:
        size = len(display_name.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("display_name_invalid") from exc
    if not 1 <= len(display_name) <= 128 or size > 512:
        raise ValueError("display_name_invalid")


def _record_best_effort(app_logger: AppLoggerPort, record: AppLogRecord) -> None:
    try:
        app_logger.record(record)
    except Exception:
        return None
