from __future__ import annotations

from dataclasses import replace
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.api.dependencies import build_api_runtime
from app.api.errors import error_response, register_exception_handlers, validation_error_info
from app.api.routes import chat, health, sessions
from app.config import Settings
from app.errors import map_exception, redact_sensitive
from app.main import build_agent_service
from app.observability.schemas import AppLogRecord
from app.utils.ids import new_id


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    api_settings = replace(
        settings,
        enable_prompt_run_log=settings.api_enable_prompt_run_log,
        enable_model_fallback=settings.api_enable_model_fallback,
    )
    agent_service, _memory_admin, resolved_settings, store = build_agent_service(
        settings=api_settings
    )
    runtime = build_api_runtime(
        agent_service=agent_service,
        settings=resolved_settings,
        store=store,
    )
    app = FastAPI(title="Aemeath Agent API", version="0.1.0")
    app.state.runtime = runtime

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = _request_id_from_header(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
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
    if value and value.strip():
        return value.strip()
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
                }
            ),
        )
    )
