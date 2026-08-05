from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.accounts.schemas import AccountRecord
from app.services.schemas import UserContext

InvitationStatus = Literal["pending", "used", "revoked"]
PasswordResetStatus = Literal["pending", "used", "revoked"]


@dataclass(frozen=True)
class AccountCredentialRecord:
    account_id: str
    password_hash: str = field(repr=False)
    credential_version: int = 1
    created_at: str = ""
    updated_at: str = ""
    password_changed_at: str = ""


@dataclass(frozen=True)
class AccountInvitationRecord:
    id: str
    username: str
    username_normalized: str
    display_name: str | None
    roles: list[str]
    status: InvitationStatus
    created_at: str
    updated_at: str
    expires_at: str
    used_at: str | None
    revoked_at: str | None
    accepted_account_id: str | None


@dataclass(frozen=True)
class AccountPasswordResetRecord:
    id: str
    account_id: str
    issued_credential_version: int
    status: PasswordResetStatus
    created_at: str
    updated_at: str
    expires_at: str
    used_at: str | None
    revoked_at: str | None


@dataclass(frozen=True)
class WebAuthSessionRecord:
    id: str = field(repr=False)
    account_id: str
    authenticated_credential_version: int
    created_at: str
    updated_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class LoginIdentity:
    account: AccountRecord
    password_hash: str = field(repr=False)
    credential_version: int = 1


@dataclass(frozen=True)
class InvitationPreflight:
    invitation_id: str = field(repr=False)
    updated_at: str = field(repr=False)


@dataclass(frozen=True)
class PasswordResetPreflight:
    reset_id: str = field(repr=False)
    account_id: str = field(repr=False)
    issued_credential_version: int = field(repr=False)
    updated_at: str = field(repr=False)


@dataclass(frozen=True)
class SessionMaterial:
    session_id: str = field(repr=False)
    raw_token: str = field(repr=False)
    token_digest: str = field(repr=False)
    csrf_verifier: bytes = field(repr=False)
    created_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str


@dataclass(frozen=True)
class AuthenticatedBrowserSession:
    user_context: UserContext
    account: AccountRecord
    web_session_id: str = field(repr=False)
    csrf_verifier: bytes = field(repr=False)
    correlation_digest: str = field(repr=False)


@dataclass(frozen=True)
class ResolvedSessionState:
    account: AccountRecord
    session: WebAuthSessionRecord
    csrf_verifier: bytes = field(repr=False)
    current_credential_version: int | None = field(repr=False)


@dataclass(frozen=True)
class IssuedInvitation:
    record: AccountInvitationRecord
    raw_token: str = field(repr=False)
    invitation_url: str = field(repr=False)


@dataclass(frozen=True)
class IssuedPasswordReset:
    record: AccountPasswordResetRecord
    raw_token: str = field(repr=False)
    reset_url: str = field(repr=False)


@dataclass(frozen=True)
class BrowserLoginResult:
    account: AccountRecord
    session: WebAuthSessionRecord
    raw_session_token: str = field(repr=False)


@dataclass(frozen=True)
class StoreSchemaInfo:
    db_backend: str
    migration_ids: tuple[str, ...]
    browser_auth_schema_revision: str
    schema_contract_fingerprint: str
