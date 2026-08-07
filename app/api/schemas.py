from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

from app.schemas import MessageRecord
from app.services.schemas import ChatResponse, UsedMemorySummary
from app.session.schemas import SessionRecord

SessionStatusFilter = Literal["active", "ended", "archived", "all"]


class HealthResponse(BaseModel):
    status: str
    service: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: str
    reason: Literal[
        "ready",
        "starting",
        "closing",
        "storage_unavailable",
        "filesystem_unavailable",
        "check_timeout",
    ]


class ChatRequestBody(BaseModel):
    session_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamChatRequestBody(BaseModel):
    session_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    client_message_id: str = Field(min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UsedMemoryResponse(BaseModel):
    id: str
    content: str
    score: float | None
    match_reason: str

    @classmethod
    def from_service(cls, memory: UsedMemorySummary) -> "UsedMemoryResponse":
        return cls(
            id=memory.id,
            content=memory.content,
            score=memory.score,
            match_reason=memory.match_reason,
        )


class ChatResponseBody(BaseModel):
    turn_id: str
    session_id: str
    character_id: str
    response: str
    warnings: list[str]
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    used_memories: list[UsedMemoryResponse]

    @classmethod
    def from_service(cls, response: ChatResponse) -> "ChatResponseBody":
        return cls(
            turn_id=response.turn_id,
            session_id=response.session_id,
            character_id=response.character_id,
            response=response.response,
            warnings=response.warnings,
            used_memory_ids=response.used_memory_ids,
            state_delta=response.state_delta,
            used_memories=[
                UsedMemoryResponse.from_service(memory) for memory in response.used_memories
            ],
        )


class SessionCreateRequestBody(BaseModel):
    character_id: str | None = Field(default=None, min_length=1)
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResponseBody(BaseModel):
    id: str
    user_id: str
    character_id: str
    status: str
    title: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    ended_at: str | None
    archived_at: str | None
    last_message_at: str | None
    message_count: int

    @classmethod
    def from_record(cls, session: SessionRecord) -> "SessionResponseBody":
        return cls(
            id=session.id,
            user_id=session.user_id,
            character_id=session.character_id,
            status=session.status,
            title=session.title,
            metadata=session.metadata,
            created_at=session.created_at,
            updated_at=session.updated_at,
            ended_at=session.ended_at,
            archived_at=session.archived_at,
            last_message_at=session.last_message_at,
            message_count=session.message_count,
        )


class SessionListResponseBody(BaseModel):
    sessions: list[SessionResponseBody]


class MessageResponseBody(BaseModel):
    id: str
    session_id: str
    user_id: str
    role: str
    content: str
    created_at: str
    emotion_label: str | None
    importance_score: int | None

    @classmethod
    def from_record(cls, message: MessageRecord) -> "MessageResponseBody":
        return cls(
            id=message.id,
            session_id=message.session_id,
            user_id=message.user_id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
            emotion_label=message.emotion_label,
            importance_score=message.importance_score,
        )


class SessionMessagesResponseBody(BaseModel):
    messages: list[MessageResponseBody]


class InvitationAcceptBody(BaseModel):
    token: SecretStr
    password: SecretStr


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


class PasswordChangeBody(BaseModel):
    current_password: SecretStr
    new_password: SecretStr


class PasswordResetAcceptBody(BaseModel):
    token: SecretStr
    new_password: SecretStr


class MeResponse(BaseModel):
    account_id: str
    username: str
    display_name: str | None
    roles: list[str]


class CsrfResponse(BaseModel):
    csrf_token: str
