from __future__ import annotations

from app.db.models import MessageModel
from app.db.repositories.common import SessionFactory
from app.schemas import MessageRecord
from app.utils.ids import new_id, utc_now


class MessageRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _from_model(self, model: MessageModel) -> MessageRecord:
        return MessageRecord(
            id=model.id,
            session_id=model.session_id,
            user_id=model.user_id,
            role=model.role,
            content=model.content,
            created_at=model.created_at,
            emotion_label=model.emotion_label,
            importance_score=model.importance_score,
        )

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
        message_id = new_id("msg")
        with self._session_factory() as session:
            session.add(
                MessageModel(
                    id=message_id,
                    session_id=session_id,
                    user_id=user_id,
                    role=role,
                    content=content,
                    created_at=utc_now(),
                    emotion_label=emotion_label,
                    importance_score=importance_score,
                )
            )
            session.commit()
        return message_id

    def get_recent_messages(self, *, session_id: str, limit: int) -> list[MessageRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(MessageModel)
                .filter(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at.desc())
                .limit(limit)
                .all()
            )
        return [self._from_model(row) for row in reversed(rows)]

    def get_message(self, *, message_id: str) -> MessageRecord | None:
        with self._session_factory() as session:
            message = session.get(MessageModel, message_id)
            return self._from_model(message) if message else None

    def list_messages_by_session(self, *, session_id: str) -> list[MessageRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(MessageModel)
                .filter(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at.asc())
                .all()
            )
        return [self._from_model(row) for row in rows]
