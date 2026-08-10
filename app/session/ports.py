from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.session.schemas import SessionRecord, SessionStatus


class SessionStorePort(Protocol):
    def ensure_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        ...

    def create_session(
        self,
        *,
        user_id: str,
        character_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        ...

    def get_session(self, *, session_id: str) -> SessionRecord | None:
        ...

    def require_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        ...

    def list_sessions(
        self,
        *,
        user_id: str,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
    ) -> list[SessionRecord]:
        ...

    def list_sessions_page(
        self,
        *,
        user_id: str,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
        after_updated_at: datetime | None = None,
        after_id: str | None = None,
    ) -> list[SessionRecord]:
        ...

    def update_session_title(
        self,
        *,
        session_id: str,
        user_id: str,
        title: str | None,
    ) -> SessionRecord:
        ...

    def end_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        ...

    def resume_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        ...

    def archive_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        ...
