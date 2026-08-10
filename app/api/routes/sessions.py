from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Body, Depends, Query

from app.api.dependencies import ApiRuntime, get_runtime, get_user_context
from app.api.schemas import (
    MessageResponseBody,
    SessionCreateRequestBody,
    SessionLifecycleRequestBody,
    SessionListResponseBody,
    SessionMessagesResponseBody,
    SessionResponseBody,
    SessionStatusFilter,
    SessionUpdateRequestBody,
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
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionListResponseBody:
    page = runtime.session_service.list_sessions_page(
        user_context=user_context,
        character_id=character_id,
        status=_to_session_status(status),
        limit=limit,
        cursor=cursor,
    )
    return SessionListResponseBody(
        sessions=[SessionResponseBody.from_record(session) for session in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=SessionMessagesResponseBody,
)
def list_session_messages(
    session_id: str,
    character_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionMessagesResponseBody:
    page = runtime.session_service.list_messages_page(
        user_context=user_context,
        session_id=session_id,
        character_id=character_id,
        limit=limit,
        cursor=cursor,
    )
    return SessionMessagesResponseBody(
        messages=[MessageResponseBody.from_record(message) for message in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.patch("/sessions/{session_id}", response_model=SessionResponseBody)
def update_session(
    session_id: str,
    body: SessionUpdateRequestBody,
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionResponseBody:
    session = runtime.session_service.update_title(
        user_context=user_context,
        session_id=session_id,
        character_id=body.character_id,
        title=body.title,
    )
    return SessionResponseBody.from_record(session)


@router.post("/sessions/{session_id}/end", response_model=SessionResponseBody)
def end_session(
    session_id: str,
    body: SessionLifecycleRequestBody,
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionResponseBody:
    session = runtime.session_service.end_session(
        user_context=user_context,
        session_id=session_id,
        character_id=body.character_id,
    )
    return SessionResponseBody.from_record(session)


@router.post("/sessions/{session_id}/resume", response_model=SessionResponseBody)
def resume_session(
    session_id: str,
    body: SessionLifecycleRequestBody,
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionResponseBody:
    session = runtime.session_service.resume_session(
        user_context=user_context,
        session_id=session_id,
        character_id=body.character_id,
    )
    return SessionResponseBody.from_record(session)


@router.post("/sessions/{session_id}/archive", response_model=SessionResponseBody)
def archive_session(
    session_id: str,
    body: SessionLifecycleRequestBody,
    user_context: UserContext = Depends(get_user_context),
    runtime: ApiRuntime = Depends(get_runtime),
) -> SessionResponseBody:
    session = runtime.session_service.archive_session(
        user_context=user_context,
        session_id=session_id,
        character_id=body.character_id,
    )
    return SessionResponseBody.from_record(session)


def _to_session_status(status: SessionStatusFilter) -> SessionStatus | None:
    if status == "all":
        return None
    return cast(SessionStatus, status)
