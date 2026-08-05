from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, Request

from app.accounts.service import AccountService
from app.auth.browser_sessions import BrowserSessionAuthService
from app.auth.csrf import csrf_token_matches, derive_csrf_token
from app.auth.errors import (
    AuthCredentialConflictError,
    AuthRequiredError,
    CsrfInvalidError,
)
from app.auth.invitations import InvitationService, PasswordResetService
from app.auth.passwords import PasswordEngine
from app.auth.rate_limit import InProcessPreAuthRateLimiter
from app.auth.schemas import AuthenticatedBrowserSession
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
    browser_auth: "BrowserAuthRuntime | None" = None


@dataclass(frozen=True)
class BrowserAuthRuntime:
    sessions: BrowserSessionAuthService
    invitations: InvitationService
    password_resets: PasswordResetService
    pre_auth_limiter: InProcessPreAuthRateLimiter
    csrf_key: bytes


def build_api_runtime(
    *,
    agent_service: AgentService,
    settings: Settings,
    store: StoreFacade,
    rate_limiter: ApiRateLimiterPort,
) -> ApiRuntime:
    account_service = AccountService(store)
    browser_auth = None
    if settings.api_auth_mode == "web_session":
        password_engine = PasswordEngine(
            concurrency=settings.password_work_concurrency,
            wait_timeout_seconds=settings.password_work_wait_timeout_seconds,
        )
        browser_sessions = BrowserSessionAuthService(
            store=store.browser_auth,
            passwords=password_engine,
            app_logger=agent_service.app_logger,
            idle_seconds=settings.web_session_idle_seconds,
            absolute_seconds=settings.web_session_absolute_seconds,
            touch_seconds=settings.web_session_touch_seconds,
        )
        csrf_key = settings.web_auth_csrf_key
        if csrf_key is None:
            raise ValueError("web_auth_csrf_key_missing")
        browser_auth = BrowserAuthRuntime(
            sessions=browser_sessions,
            invitations=InvitationService(
                store=store.browser_auth,
                passwords=password_engine,
                sessions=browser_sessions,
                app_logger=agent_service.app_logger,
                public_origin=settings.web_public_origin,
                lifetime_seconds=settings.invitation_ttl_seconds,
            ),
            password_resets=PasswordResetService(
                store=store.browser_auth,
                passwords=password_engine,
                sessions=browser_sessions,
                app_logger=agent_service.app_logger,
                public_origin=settings.web_public_origin,
                lifetime_seconds=settings.password_reset_ttl_seconds,
            ),
            pre_auth_limiter=InProcessPreAuthRateLimiter(
                rate_per_minute=settings.auth_pre_rate_per_minute,
                burst=settings.auth_pre_burst,
                global_rate_per_minute=settings.auth_pre_global_rate_per_minute,
                global_burst=settings.auth_pre_global_burst,
                idle_ttl_seconds=settings.auth_pre_rate_idle_ttl_seconds,
                max_buckets=settings.auth_pre_rate_max_buckets,
            ),
            csrf_key=csrf_key,
        )
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
        browser_auth=browser_auth,
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
    cookie_token = extract_web_session_cookie(request)
    if authorization and cookie_token is not None:
        raise AuthCredentialConflictError()
    if runtime.settings.api_auth_mode == "web_session":
        if authorization:
            raise AuthCredentialConflictError()
        if cookie_token is None:
            raise AuthRequiredError()
        browser = require_browser_auth_runtime(runtime)
        try:
            authenticated = browser.sessions.resolve_session_token(
                token=cookie_token,
                request_id=request_id,
            )
        except Exception:
            request.state.clear_web_session_cookie = True
            raise
        request.state.web_auth_session = authenticated
        request.state.web_session_id = authenticated.web_session_id
        request.state.web_session_correlation = authenticated.correlation_digest
        user_context = authenticated.user_context
    elif cookie_token is not None:
        raise AuthCredentialConflictError()
    elif authorization:
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
    if runtime.settings.api_auth_mode == "web_session" and request.method not in {
        "GET",
        "HEAD",
        "OPTIONS",
    }:
        require_valid_csrf(request=request, runtime=runtime)
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


def get_authenticated_browser_session(
    request: Request,
    user_context: UserContext = Depends(get_user_context),
) -> AuthenticatedBrowserSession:
    del user_context
    authenticated = getattr(request.state, "web_auth_session", None)
    if not isinstance(authenticated, AuthenticatedBrowserSession):
        raise AuthRequiredError()
    return authenticated


def get_csrf_authenticated_browser_session(
    request: Request,
    authenticated: AuthenticatedBrowserSession = Depends(get_authenticated_browser_session),
    runtime: ApiRuntime = Depends(get_runtime),
) -> AuthenticatedBrowserSession:
    require_valid_csrf(request=request, runtime=runtime)
    return authenticated


def require_valid_csrf(*, request: Request, runtime: ApiRuntime) -> None:
    browser = require_browser_auth_runtime(runtime)
    authenticated = getattr(request.state, "web_auth_session", None)
    if not isinstance(authenticated, AuthenticatedBrowserSession):
        raise AuthRequiredError()
    expected = derive_csrf_token(
        key=browser.csrf_key,
        web_session_id=authenticated.web_session_id,
        verifier=authenticated.csrf_verifier,
    )
    if not csrf_token_matches(
        expected=expected,
        provided=request.headers.get("X-CSRF-Token"),
    ):
        raise CsrfInvalidError()


def require_browser_auth_runtime(runtime: ApiRuntime) -> BrowserAuthRuntime:
    if runtime.browser_auth is None:
        raise AuthRequiredError()
    return runtime.browser_auth


def extract_web_session_cookie(request: Request) -> str | None:
    values: list[str] = []
    for raw_header in request.headers.getlist("cookie"):
        for component in raw_header.split(";"):
            name, separator, value = component.strip().partition("=")
            if separator and name == "__Host-aemeath_session":
                values.append(value)
    if len(values) > 1:
        request.state.clear_web_session_cookie = True
        raise AuthCredentialConflictError()
    if not values or not values[0]:
        return None
    if len(values[0]) > 64:
        request.state.clear_web_session_cookie = True
        raise AuthCredentialConflictError()
    return values[0]
