from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import replace
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.api.dependencies import build_api_runtime
from app.api.errors import error_response, register_exception_handlers, validation_error_info
from app.api.routes import chat, health, sessions
from app.config import Settings
from app.errors import map_exception, redact_sensitive
from app.main import build_application_runtime
from app.observability.schemas import AppLogRecord
from app.runtime.guardrails.rate_limit import InProcessApiRateLimiter
from app.utils.ids import new_id

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    api_settings = replace(
        settings,
        enable_prompt_run_log=settings.api_enable_prompt_run_log,
        enable_model_fallback=settings.api_enable_model_fallback,
    )
    application_runtime = build_application_runtime(settings=api_settings, surface="api")
    resolved_settings = application_runtime.settings
    runtime = build_api_runtime(
        agent_service=application_runtime.agent_service,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            application_runtime.close()

    app = FastAPI(title="Aemeath Agent API", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.application_runtime = application_runtime

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

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(sessions.router)
    return app


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
                }
            ),
        )
    )
