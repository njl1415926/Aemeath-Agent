from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.dependencies import ApiRuntime, build_api_runtime
from app.api.errors import error_response, register_exception_handlers, validation_error_info
from app.api.routes import auth, chat, chat_stream, health, sessions
from app.api.sse import SSEClientDisconnectMiddleware
from app.api.web_security import enforce_web_request_provenance
from app.config import Settings
from app.db.readiness import build_database_readiness
from app.deployment.preflight import run_startup_preflight
from app.errors import map_exception, redact_sensitive
from app.main import build_application_runtime
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.errors import ApiRateLimitedError
from app.runtime.guardrails.rate_limit import InProcessApiRateLimiter
from app.runtime.readiness import (
    ApplicationReadiness,
    CachedFilesystemReadinessProbe,
    LifecycleReadinessState,
)
from app.runtime.resources import ApplicationRuntime
from app.utils.ids import new_id

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PRE_AUTH_ENDPOINTS = {
    "/v1/auth/invitations/accept": "invitation_accept",
    "/v1/auth/login": "login",
    "/v1/auth/password-reset/accept": "password_reset_accept",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    run_startup_preflight(settings)
    api_settings = replace(
        settings,
        enable_prompt_run_log=settings.api_enable_prompt_run_log,
        enable_model_fallback=settings.api_enable_model_fallback,
    )
    application_runtime = build_application_runtime(settings=api_settings, surface="api")
    try:
        return _create_app_with_runtime(application_runtime)
    except BaseException:
        application_runtime.close()
        raise


def _create_app_with_runtime(application_runtime: ApplicationRuntime) -> FastAPI:
    resolved_settings = application_runtime.settings
    agent_stream_service = application_runtime.agent_stream_service
    if agent_stream_service is None:
        raise RuntimeError("api_stream_service_missing")
    runtime = build_api_runtime(
        agent_service=application_runtime.agent_service,
        agent_stream_service=agent_stream_service,
        settings=resolved_settings,
        store=application_runtime.store,
        rate_limiter=InProcessApiRateLimiter(
            user_rate_per_minute=resolved_settings.api_chat_user_rate_per_minute,
            user_burst=resolved_settings.api_chat_user_burst,
            ip_rate_per_minute=resolved_settings.api_chat_ip_rate_per_minute,
            ip_burst=resolved_settings.api_chat_ip_burst,
            idle_ttl_seconds=resolved_settings.api_rate_limit_idle_ttl_seconds,
        ),
    )
    lifecycle_readiness = LifecycleReadinessState()
    readiness = ApplicationReadiness(
        lifecycle=lifecycle_readiness,
        database=build_database_readiness(
            backend=resolved_settings.db_backend,
            db_path=resolved_settings.db_path,
            timeout_seconds=resolved_settings.readiness_db_timeout_seconds,
        ),
        filesystem=CachedFilesystemReadinessProbe(
            _readiness_filesystem_paths(resolved_settings),
            min_free_bytes=resolved_settings.readiness_min_free_bytes,
            cache_ttl_seconds=resolved_settings.readiness_filesystem_cache_ttl_seconds,
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        lifecycle_readiness.mark_ready()
        try:
            yield
        finally:
            lifecycle_readiness.mark_closing()
            application_runtime.close()

    app = FastAPI(title="Aemeath Agent API", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.application_runtime = application_runtime
    app.state.readiness = readiness
    app.state.readiness_timeout_seconds = resolved_settings.readiness_timeout_seconds

    if resolved_settings.api_cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.api_cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "PATCH", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = _request_id_from_header(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        request.state.external_request_id_accepted = bool(
            request.headers.get("X-Request-ID") is not None
            and _REQUEST_ID_RE.fullmatch(request.headers["X-Request-ID"])
        )
        started_at = perf_counter()
        response = None
        try:
            enforce_web_request_provenance(
                request=request,
                settings=resolved_settings,
            )
            _enforce_early_pre_auth(request=request, runtime=runtime)
            response = await call_next(request)
        except Exception as exc:
            if isinstance(exc, RequestValidationError):
                error_info = validation_error_info(exc)
            else:
                error_info = map_exception(exc)
            request.state.error_code = error_info.code.value
            response = error_response(request=request, error_info=error_info)
            _record_api_request_log(
                request=request,
                status_code=response.status_code,
                duration_ms=_duration_ms(started_at),
                error_code=error_info.code.value,
            )
            return response
        response.headers["X-Request-ID"] = request_id
        _record_api_request_log(
            request=request,
            status_code=response.status_code,
            duration_ms=_duration_ms(started_at),
            error_code=getattr(request.state, "error_code", None),
        )
        return response

    app.add_middleware(SSEClientDisconnectMiddleware)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(chat_stream.router)
    app.include_router(sessions.router)
    return app


def _enforce_early_pre_auth(*, request: Request, runtime: ApiRuntime) -> None:
    if runtime.settings.api_auth_mode != "web_session" or request.method != "POST":
        return
    endpoint = _PRE_AUTH_ENDPOINTS.get(request.url.path)
    if endpoint is None:
        return
    browser = runtime.browser_auth
    if browser is None:
        raise RuntimeError("browser_auth_runtime_missing")
    decision = browser.pre_auth_limiter.check_request(
        endpoint=endpoint,
        ip_value=request.client.host if request.client is not None else None,
    )
    if not decision.allowed:
        raise ApiRateLimitedError(
            f"auth_pre_{decision.reason}",
            retry_after_seconds=decision.retry_after_seconds,
        )
    request.state.pre_auth_endpoint = endpoint


def _readiness_filesystem_paths(settings: Settings) -> tuple[Path, ...]:
    paths = [settings.db_path.parent]
    if settings.enable_app_log:
        paths.append(settings.app_log_root)
    if settings.enable_trace_log:
        paths.append(settings.trace_log_root)
    if settings.enable_prompt_run_log:
        paths.append(settings.prompt_run_log_root)
    return tuple(paths)


def _request_id_from_header(value: str | None) -> str:
    if value is not None and _REQUEST_ID_RE.fullmatch(value):
        return value
    return new_id("req")


def _duration_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def _record_api_request_log(
    *,
    request: Request,
    status_code: int,
    duration_ms: float,
    error_code: str | None,
) -> None:
    runtime = request.app.state.runtime
    is_error = status_code >= 400
    try:
        runtime.app_logger.record(
            AppLogRecord(
                event_name="api.request_failed" if is_error else "api.request_completed",
                level="error" if is_error else "info",
                request_id=getattr(request.state, "request_id", None),
                user_id=getattr(request.state, "user_id", None),
                error_code=error_code,
                payload=redact_sensitive(
                    {
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                        "external_request_id_accepted": bool(
                            getattr(request.state, "external_request_id_accepted", False)
                        ),
                        "web_session_correlation": getattr(
                            request.state, "web_session_correlation", None
                        ),
                    }
                ),
            )
        )
    except Exception:
        return None
