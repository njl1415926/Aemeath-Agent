from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, Request

from app.accounts.service import AccountService
from app.auth.errors import AuthRequiredError
from app.auth.service import AuthService, LocalDefaultAccountResolver
from app.config import Settings
from app.db.store_factory import StoreFacade
from app.errors import redact_sensitive
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.errors import ApiRateLimitedError
from app.runtime.guardrails.ports import ApiRateLimiterPort
from app.runtime.guardrails.schemas import ApiRateLimitRequest
from app.runtime.readiness import ReadinessPort
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
    rate_limiter: ApiRateLimiterPort


def build_api_runtime(
    *,
    agent_service: AgentService,
    settings: Settings,
    store: StoreFacade,
    rate_limiter: ApiRateLimiterPort,
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
        rate_limiter=rate_limiter,
    )


def get_runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def get_readiness(request: Request) -> ReadinessPort:
    return request.app.state.readiness


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


def get_rate_limited_user_context(
    request: Request,
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> UserContext:
    decision = runtime.rate_limiter.check(
        request=ApiRateLimitRequest(
            user_id=user_context.user_id,
            ip_key=request.client.host if request.client is not None else None,
        )
    )
    if not decision.allowed:
        request.state.limit_reason = decision.reason_code
        runtime.app_logger.record(
            AppLogRecord(
                event_name="api.chat_rate_limited",
                level="warning",
                request_id=user_context.request_id,
                user_id=user_context.user_id,
                error_code="API_RATE_LIMITED",
                payload=redact_sensitive(
                    {
                        "limit_reason": decision.reason_code,
                        "retry_after_seconds": decision.retry_after_seconds,
                    }
                ),
            )
        )
        raise ApiRateLimitedError(
            decision.reason_code,
            retry_after_seconds=decision.retry_after_seconds,
        )
    runtime.app_logger.record(
        AppLogRecord(
            event_name="api.chat_rate_accepted",
            request_id=user_context.request_id,
            user_id=user_context.user_id,
            payload={"status": "accepted"},
        )
    )
    return user_context
