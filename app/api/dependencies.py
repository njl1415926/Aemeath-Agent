from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, Request

from app.accounts.service import AccountService
from app.auth.errors import AuthRequiredError
from app.auth.service import AuthService, LocalDefaultAccountResolver
from app.config import Settings
from app.db.store_factory import StoreFacade
from app.observability.ports import AppLoggerPort
from app.services.agent_service import AgentService
from app.services.schemas import UserContext
from app.services.session_service import SessionService


@dataclass(frozen=True)
class ApiRuntime:
    agent_service: AgentService
    session_service: SessionService
    auth_service: AuthService
    local_default_resolver: LocalDefaultAccountResolver
    settings: Settings
    store: StoreFacade
    app_logger: AppLoggerPort


def build_api_runtime(
    *,
    agent_service: AgentService,
    settings: Settings,
    store: StoreFacade,
) -> ApiRuntime:
    account_service = AccountService(store)
    return ApiRuntime(
        agent_service=agent_service,
        session_service=SessionService(session_store=store, message_store=store),
        auth_service=AuthService(account_service),
        local_default_resolver=LocalDefaultAccountResolver(
            account_service=account_service,
            default_user_id=settings.default_user_id,
        ),
        settings=settings,
        store=store,
        app_logger=agent_service.app_logger,
    )


def get_runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def get_request_id(request: Request) -> str:
    return request.state.request_id


def get_user_context(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    runtime: ApiRuntime = Depends(get_runtime),
) -> UserContext:
    request_id = get_request_id(request)
    metadata: dict[str, Any] = {
        "client": "api",
        "method": request.method,
        "path": request.url.path,
    }
    if authorization:
        user_context = runtime.auth_service.resolve_bearer_token(
            authorization,
            request_id=request_id,
            metadata=metadata,
        )
    elif runtime.settings.api_allow_local_default_user:
        user_context = runtime.local_default_resolver.resolve(
            request_id=request_id,
            metadata=metadata,
        )
    else:
        raise AuthRequiredError()
    request.state.user_id = user_context.user_id
    return user_context
