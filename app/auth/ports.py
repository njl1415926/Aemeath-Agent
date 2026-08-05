from __future__ import annotations

from typing import Literal, Protocol

from app.accounts.schemas import AccountRecord
from app.auth.schemas import (
    AccountInvitationRecord,
    AccountPasswordResetRecord,
    InvitationPreflight,
    LoginIdentity,
    PasswordResetPreflight,
    ResolvedSessionState,
    SessionMaterial,
    StoreSchemaInfo,
    WebAuthSessionRecord,
)

InvitationAcceptanceOutcome = Literal["accepted", "invalid", "identity_conflict"]


class BrowserSessionStorePort(Protocol):
    def get_login_identity(self, *, username_normalized: str) -> LoginIdentity | None: ...

    def create_login_session(
        self,
        *,
        account_id: str,
        expected_password_hash: str,
        expected_credential_version: int,
        replacement_password_hash: str | None,
        material: SessionMaterial,
    ) -> WebAuthSessionRecord | None: ...

    def resolve_session(self, *, token_digest: str) -> ResolvedSessionState | None: ...

    def revoke_session(self, *, session_id: str, reason: str, now: str) -> bool: ...

    def touch_session(
        self,
        *,
        session_id: str,
        observed_last_seen: str,
        now: str,
        idle_expires_at: str,
    ) -> bool: ...

    def change_password_and_rotate_session(
        self,
        *,
        account_id: str,
        expected_password_hash: str,
        expected_credential_version: int,
        new_password_hash: str,
        material: SessionMaterial,
        now: str,
    ) -> WebAuthSessionRecord | None: ...


class InvitationStorePort(Protocol):
    def issue_invitation(
        self,
        *,
        invitation_id: str,
        token_digest: str,
        username: str,
        display_name: str | None,
        roles: list[str],
        created_at: str,
        expires_at: str,
    ) -> AccountInvitationRecord | None: ...

    def preflight_invitation(
        self, *, token_digest: str, now: str
    ) -> InvitationPreflight | None: ...

    def accept_invitation(
        self,
        *,
        invitation_id: str,
        token_digest: str,
        password_hash: str,
        account_id: str,
        material: SessionMaterial,
        now: str,
    ) -> tuple[
        InvitationAcceptanceOutcome,
        AccountRecord | None,
        WebAuthSessionRecord | None,
    ]: ...


class PasswordResetStorePort(Protocol):
    def issue_password_reset(
        self,
        *,
        account_selector: str,
        reset_id: str,
        token_digest: str,
        created_at: str,
        expires_at: str,
    ) -> AccountPasswordResetRecord | None: ...

    def preflight_password_reset(
        self, *, token_digest: str, now: str
    ) -> PasswordResetPreflight | None: ...

    def accept_password_reset(
        self,
        *,
        reset_id: str,
        token_digest: str,
        password_hash: str,
        material: SessionMaterial,
        now: str,
    ) -> tuple[bool, AccountRecord | None, WebAuthSessionRecord | None]: ...


class BrowserAuthAdminStorePort(Protocol):
    def resolve_account_id(self, *, account_selector: str) -> str | None: ...

    def list_invitations(self, *, limit: int = 50) -> list[AccountInvitationRecord]: ...

    def revoke_invitation(self, *, invitation_id: str, now: str) -> bool: ...

    def list_password_resets(self, *, limit: int = 50) -> list[AccountPasswordResetRecord]: ...

    def revoke_account_sessions(self, *, account_id: str, reason: str, now: str) -> int: ...

    def disable_account(self, *, account_id: str, now: str) -> bool: ...

    def enable_account(self, *, account_id: str, now: str) -> bool: ...

    def cleanup_sessions(self, *, before: str, limit: int) -> int: ...


class BrowserAuthStorePort(
    BrowserSessionStorePort,
    InvitationStorePort,
    PasswordResetStorePort,
    BrowserAuthAdminStorePort,
    Protocol,
):
    def get_store_schema_info(self) -> StoreSchemaInfo: ...
