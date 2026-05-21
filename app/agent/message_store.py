from __future__ import annotations

from typing import Protocol

from app.schemas import MessageRecord


class MessageStore(Protocol):
    def save_message(
        self,
        *,
        session_id: str,
        user_id: str,
        role: str,
        content: str,
        emotion_label: str | None = None,
        importance_score: int | None = None,
    ) -> str:
        ...

    def get_recent_messages(self, *, session_id: str, limit: int) -> list[MessageRecord]:
        ...

    def list_messages_by_session(self, *, session_id: str) -> list[MessageRecord]:
        ...

