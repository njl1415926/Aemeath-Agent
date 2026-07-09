from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Body, Depends, Query

from app.api.dependencies import ApiRuntime, get_runtime, get_user_context
from app.api.schemas import (
    MessageResponseBody,
    SessionCreateRequestBody,
    SessionListResponseBody,
    SessionMessagesResponseBody,
    SessionResponseBody,
    SessionStatusFilter,
)
from app.services.schemas import UserContext
from app.session.schemas import SessionStatus

router = APIRouter(prefix="/v1", tags=["sessions"])


@router.post("/sessions", response_model=SessionResponseBody, status_code=201)
def create_session(
    body: SessionCreateRequestBody | None = Body(default=None),
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionResponseBody:
    request_body = body or SessionCreateRequestBody()
    character_id = request_body.character_id or runtime.settings.default_character_id
    session = runtime.session_service.create_session(
        user_context=user_context,
        character_id=character_id,
        title=request_body.title,
        metadata=request_body.metadata,
    )
    return SessionResponseBody.from_record(session)


@router.get("/sessions", response_model=SessionListResponseBody)
def list_sessions(
    character_id: str | None = Query(default=None, min_length=1),
    status: SessionStatusFilter = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionListResponseBody:
    sessions = runtime.session_service.list_sessions(
        user_context=user_context,
        character_id=character_id,
        status=_to_session_status(status),
        limit=limit,
    )
    return SessionListResponseBody(
        sessions=[SessionResponseBody.from_record(session) for session in sessions]
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=SessionMessagesResponseBody,
)
def list_session_messages(
    session_id: str,
    character_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionMessagesResponseBody:
    messages = runtime.session_service.list_messages(
        user_context=user_context,
        session_id=session_id,
        character_id=character_id,
        limit=limit,
    )
    return SessionMessagesResponseBody(
        messages=[MessageResponseBody.from_record(message) for message in messages]
    )


def _to_session_status(status: SessionStatusFilter) -> SessionStatus | None:
    if status == "all":
        return None
    return cast(SessionStatus, status)
