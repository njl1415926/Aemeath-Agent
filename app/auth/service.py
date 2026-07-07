from __future__ import annotations

import hashlib
from typing import Any

from app.accounts.schemas import AccountRecord
from app.accounts.service import AccountService
from app.auth.errors import (
    AuthAccountDisabledError,
    AuthInvalidCredentialsError,
    AuthRequiredError,
)
from app.errors import redact_sensitive
from app.services.schemas import UserContext

_DEV_TOKEN_HASH_PREFIX = "sha256:"
_DEV_TOKEN_HASH_CONTEXT = "aemeath-dev-token-v0.1:"


def hash_dev_token(token: str) -> str:
    digest = hashlib.sha256(
        f"{_DEV_TOKEN_HASH_CONTEXT}{token}".encode("utf-8")
    ).hexdigest()
    return f"{_DEV_TOKEN_HASH_PREFIX}{digest}"


class AuthService:
    def __init__(self, account_service: AccountService):
        self.account_service = account_service

    def resolve_bearer_token(
        self,
        token: str | None,
        *,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserContext:
        token_value = _normalize_bearer_token(token)
        token_hash = hash_dev_token(token_value)
        account = self.account_service.get_by_dev_token_hash(
            dev_token_hash=token_hash,
        )
        if account is None:
            raise AuthInvalidCredentialsError()
        account = _require_active(account)
        self.account_service.record_login(account_id=account.id)
        safe_metadata = redact_sensitive(dict(metadata or {}))
        safe_metadata["auth_method"] = "dev_token"
        return UserContext(
            user_id=account.id,
            roles=list(account.roles),
            request_id=request_id,
            metadata=safe_metadata,
        )


class LocalDefaultAccountResolver:
    def __init__(
        self,
        *,
        account_service: AccountService,
        default_user_id: str,
    ):
        self.account_service = account_service
        self.default_user_id = default_user_id

    def resolve(
        self,
        *,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserContext:
        account = self.account_service.ensure_account(account_id=self.default_user_id)
        account = _require_active(account)
        safe_metadata = redact_sensitive(dict(metadata or {}))
        safe_metadata["auth_method"] = "local_default"
        return UserContext(
            user_id=account.id,
            roles=list(account.roles),
            request_id=request_id,
            metadata=safe_metadata,
        )


def _normalize_bearer_token(token: str | None) -> str:
    if token is None:
        raise AuthRequiredError()
    token_value = token.strip()
    if token_value.lower().startswith("bearer "):
        token_value = token_value[7:].strip()
    if not token_value:
        raise AuthRequiredError()
    return token_value


def _require_active(account: AccountRecord) -> AccountRecord:
    if account.status != "active":
        raise AuthAccountDisabledError()
    return account
