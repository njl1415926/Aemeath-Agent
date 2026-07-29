from __future__ import annotations

import math

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AemeathError, ErrorCode, ErrorInfo, http_status_for_error, map_exception


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return error_response(request=request, error_info=validation_error_info(exc))


def validation_error_info(exc: RequestValidationError) -> ErrorInfo:
    error = AemeathError(
        code=ErrorCode.INVALID_INPUT,
        public_message="Invalid input.",
        internal_message="request validation failed",
        metadata={"validation_error_count": len(exc.errors())},
    )
    return map_exception(error)


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    if exc.status_code == 404:
        public_message = "Route not found."
    elif exc.status_code == 405:
        public_message = "Method not allowed."
    else:
        public_message = "Invalid request."
    error = AemeathError(
        code=ErrorCode.INVALID_INPUT,
        public_message=public_message,
        internal_message=f"HTTP {exc.status_code}: {exc.detail}",
    )
    return error_response(request=request, error_info=map_exception(error))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(request=request, error_info=map_exception(exc))


def error_response(*, request: Request, error_info: ErrorInfo) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    request.state.error_code = error_info.code.value
    content = {
        "error": error_info.to_public_dict(),
        "request_id": request_id,
    }
    headers = {"X-Request-ID": request_id} if request_id else {}
    retry_after = error_info.metadata.get("retry_after_seconds")
    if (
        isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and retry_after >= 0
    ):
        headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return JSONResponse(
        status_code=http_status_for_error(error_info),
        content=content,
        headers=headers or None,
    )
