from __future__ import annotations

from time import monotonic

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.dependencies import get_readiness
from app.api.schemas import HealthResponse, ReadinessResponse
from app.runtime.readiness import ReadinessPort

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(status="ok", service="aemeath-agent")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
def ready(
    request: Request,
    response: Response,
    readiness: ReadinessPort = Depends(get_readiness),
) -> ReadinessResponse:
    deadline = monotonic() + request.app.state.readiness_timeout_seconds
    result = readiness.check(deadline)
    response.headers["Cache-Control"] = "no-store"
    if not result.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if result.ready else "not_ready",
        service="aemeath-agent",
        reason=result.reason.value,
    )
