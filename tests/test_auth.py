from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.accounts.service import AccountService
from app.auth.errors import (
    AuthAccountDisabledError,
    AuthInvalidCredentialsError,
    AuthRequiredError,
)
from app.auth.service import AuthService, LocalDefaultAccountResolver, hash_dev_token
from app.errors import ErrorCode, http_status_for_error, map_exception
from app.main import build_agent_service


def test_dev_token_resolves_to_user_context(settings):
    service_settings = replace(
        settings,
        dev_auth_token="correct-dev-token",
        dev_auth_user_id="user_default",
    )
    _service, _memory_admin, _resolved_settings, store = build_agent_service(
        settings=service_settings
    )
    auth_service = AuthService(AccountService(store))

    user_context = auth_service.resolve_bearer_token(
        "Bearer correct-dev-token",
        request_id="request_1",
        metadata={"authorization": "Bearer should-not-survive"},
    )

    account = store.get_account(account_id="user_default")
    assert user_context.user_id == "user_default"
    assert user_context.roles == ["user"]
    assert user_context.request_id == "request_1"
    assert user_context.metadata["auth_method"] == "dev_token"
    assert user_context.metadata["authorization"] == "[redacted]"
    assert account is not None
    assert account.dev_token_hash == hash_dev_token("correct-dev-token")
    assert account.last_login_at is not None
    assert "correct-dev-token" not in json.dumps(user_context.metadata)


def test_wrong_or_missing_dev_token_maps_to_auth_errors(settings):
    service_settings = replace(settings, dev_auth_token="correct-dev-token")
    _service, _memory_admin, _resolved_settings, store = build_agent_service(
        settings=service_settings
    )
    auth_service = AuthService(AccountService(store))

    with pytest.raises(AuthInvalidCredentialsError) as wrong:
        auth_service.resolve_bearer_token("wrong-token-secret")
    wrong_info = map_exception(wrong.value)

    with pytest.raises(AuthRequiredError) as missing:
        auth_service.resolve_bearer_token("")
    missing_info = map_exception(missing.value)

    assert wrong_info.code == ErrorCode.AUTH_INVALID_CREDENTIALS
    assert http_status_for_error(wrong_info) == 401
    assert missing_info.code == ErrorCode.AUTH_REQUIRED
    assert http_status_for_error(missing_info) == 401
    assert "wrong-token-secret" not in json.dumps(wrong_info.to_public_dict())
    assert "wrong-token-secret" not in json.dumps(wrong_info.to_log_dict())


def test_disabled_account_cannot_resolve_user_context(settings):
    _service, _memory_admin, _resolved_settings, store = build_agent_service(settings=settings)
    account_service = AccountService(store)
    account_service.ensure_account(account_id="disabled_user")
    store.set_account_dev_token_hash(
        account_id="disabled_user",
        dev_token_hash=hash_dev_token("disabled-token"),
    )
    store.set_account_status(account_id="disabled_user", status="disabled")
    auth_service = AuthService(account_service)

    with pytest.raises(AuthAccountDisabledError) as raised:
        auth_service.resolve_bearer_token("disabled-token")

    error_info = map_exception(raised.value)
    assert error_info.code == ErrorCode.AUTH_ACCOUNT_DISABLED
    assert http_status_for_error(error_info) == 403
    assert "disabled-token" not in json.dumps(error_info.to_public_dict())
    assert "disabled-token" not in json.dumps(error_info.to_log_dict())


def test_local_default_account_resolver(settings):
    _service, _memory_admin, _resolved_settings, store = build_agent_service(settings=settings)
    resolver = LocalDefaultAccountResolver(
        account_service=AccountService(store),
        default_user_id="user_default",
    )

    user_context = resolver.resolve(metadata={"cookie": "local-cookie-secret"})

    assert user_context.user_id == "user_default"
    assert user_context.roles == ["user"]
    assert user_context.metadata["auth_method"] == "local_default"
    assert user_context.metadata["cookie"] == "[redacted]"
