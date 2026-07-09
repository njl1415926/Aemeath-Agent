from __future__ import annotations

from typing import Any

from app.agent.message_store import MessageStore
from app.schemas import MessageRecord
from app.services.schemas import UserContext
from app.session.errors import (
    SessionArchivedError,
    SessionNotFoundError,
    SessionOwnershipError,
)
from app.session.ports import SessionStorePort
from app.session.schemas import SessionRecord, SessionStatus


class SessionService:
    def __init__(
        self,
        *,
        session_store: SessionStorePort,
        message_store: MessageStore,
    ) -> None:
        self.session_store = session_store
        self.message_store = message_store

    def create_session(
        self,
        *,
        user_context: UserContext,
        character_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        return self.session_store.create_session(
            user_id=user_context.user_id,
            character_id=character_id,
            title=title,
            metadata=metadata,
        )

    def list_sessions(
        self,
        *,
        user_context: UserContext,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
    ) -> list[SessionRecord]:
        return self.session_store.list_sessions(
            user_id=user_context.user_id,
            character_id=character_id,
            status=status,
            limit=limit,
        )

    def list_messages(
        self,
        *,
        user_context: UserContext,
        session_id: str,
        character_id: str | None = None,
        limit: int = 100,
    ) -> list[MessageRecord]:
        session = self.session_store.get_session(session_id=session_id)
        if session is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        if session.user_id != user_context.user_id:
            raise SessionOwnershipError(
                f"session {session_id!r} belongs to a different user"
            )
        checked_character_id = character_id or session.character_id
        session = self.session_store.require_session(
            session_id=session_id,
            user_id=user_context.user_id,
            character_id=checked_character_id,
        )
        if session.status == "archived":
            raise SessionArchivedError(f"session {session_id!r} is archived")
        if limit <= 0:
            return []
        messages = self.message_store.list_messages_by_session(session_id=session.id)
        return messages[-limit:]
