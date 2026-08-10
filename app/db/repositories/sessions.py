from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, desc, func, or_

from app.db.models import MessageModel, SessionModel
from app.db.repositories.common import SessionFactory, dump_json, load_json_object
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionNotFoundError,
    SessionOwnershipError,
    SessionTransitionError,
)
from app.session.pagination import format_storage_datetime
from app.session.schemas import SessionRecord, SessionStatus
from app.utils.ids import new_id, utc_now


class SessionRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _row_query(self, session):
        last_message_at = func.max(MessageModel.created_at).label("last_message_at")
        message_count = func.count(MessageModel.id).label("message_count")
        effective_updated_at = case(
            (
                (last_message_at.is_not(None)) & (last_message_at > SessionModel.updated_at),
                last_message_at,
            ),
            else_=SessionModel.updated_at,
        ).label("updated_at")
        return (
            session.query(
                SessionModel.id,
                SessionModel.user_id,
                SessionModel.character_id,
                SessionModel.status,
                SessionModel.title,
                SessionModel.metadata_json,
                SessionModel.created_at,
                effective_updated_at,
                SessionModel.ended_at,
                SessionModel.archived_at,
                last_message_at,
                message_count,
            )
            .outerjoin(MessageModel, MessageModel.session_id == SessionModel.id)
            .group_by(SessionModel.id)
        )

    def _from_row(self, row) -> SessionRecord:
        return SessionRecord(
            id=row.id,
            user_id=row.user_id,
            character_id=row.character_id,
            status=row.status,
            title=row.title,
            metadata=load_json_object(row.metadata_json),
            created_at=row.created_at,
            updated_at=row.updated_at,
            ended_at=row.ended_at,
            archived_at=row.archived_at,
            last_message_at=row.last_message_at,
            message_count=row.message_count,
        )

    def _get_with_session(self, session, *, session_id: str) -> SessionRecord | None:
        row = self._row_query(session).filter(SessionModel.id == session_id).limit(1).one_or_none()
        return self._from_row(row) if row else None

    def _validate_session_ownership(
        self,
        session_record: SessionRecord,
        *,
        user_id: str,
        character_id: str | None = None,
    ) -> None:
        if session_record.user_id != user_id:
            raise SessionOwnershipError(
                f"session {session_record.id!r} belongs to a different user"
            )
        if character_id is not None and session_record.character_id != character_id:
            raise SessionOwnershipError(
                f"session {session_record.id!r} belongs to a different character"
            )

    def _require_owned_session(
        self,
        session,
        *,
        session_id: str,
        user_id: str,
        character_id: str | None = None,
    ) -> SessionRecord:
        session_record = self._get_with_session(session, session_id=session_id)
        if session_record is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        self._validate_session_ownership(
            session_record,
            user_id=user_id,
            character_id=character_id,
        )
        return session_record

    def ensure_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        now = utc_now()
        metadata_json = dump_json(metadata or {}, sort_keys=True)
        with self._session_factory() as session:
            model = session.get(SessionModel, session_id)
            if model is None:
                session.add(
                    SessionModel(
                        id=session_id,
                        user_id=user_id,
                        character_id=character_id,
                        status="active",
                        title=None,
                        metadata_json=metadata_json,
                        created_at=now,
                        updated_at=now,
                        ended_at=None,
                        archived_at=None,
                    )
                )
                session.flush()
            session_record = self._get_with_session(session, session_id=session_id)
            if session_record is None:
                raise SessionNotFoundError(f"session {session_id!r} was not created")
            self._validate_session_ownership(
                session_record,
                user_id=user_id,
                character_id=character_id,
            )
            if session_record.status == "ended":
                raise SessionEndedError(f"session {session_id!r} has ended")
            if session_record.status == "archived":
                raise SessionArchivedError(f"session {session_id!r} is archived")
            session.commit()
            return session_record

    def create_session(
        self,
        *,
        user_id: str,
        character_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        session_id = new_id("session")
        now = utc_now()
        metadata_json = dump_json(metadata or {}, sort_keys=True)
        with self._session_factory() as session:
            session.add(
                SessionModel(
                    id=session_id,
                    user_id=user_id,
                    character_id=character_id,
                    status="active",
                    title=title,
                    metadata_json=metadata_json,
                    created_at=now,
                    updated_at=now,
                    ended_at=None,
                    archived_at=None,
                )
            )
            session.flush()
            session_record = self._get_with_session(session, session_id=session_id)
            session.commit()
        if session_record is None:
            raise SessionNotFoundError(f"session {session_id!r} was not created")
        return session_record

    def get_session(self, *, session_id: str) -> SessionRecord | None:
        with self._session_factory() as session:
            return self._get_with_session(session, session_id=session_id)

    def require_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        with self._session_factory() as session:
            session_record = self._require_owned_session(
                session,
                session_id=session_id,
                user_id=user_id,
                character_id=character_id,
            )
        if session_record.status == "archived":
            raise SessionArchivedError(f"session {session_id!r} is archived")
        return session_record

    def list_sessions(
        self,
        *,
        user_id: str,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
    ) -> list[SessionRecord]:
        return self.list_sessions_page(
            user_id=user_id,
            character_id=character_id,
            status=status,
            limit=limit,
        )

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
        if limit <= 0:
            return []
        with self._session_factory() as session:
            query = self._row_query(session).filter(SessionModel.user_id == user_id)
            if character_id is not None:
                query = query.filter(SessionModel.character_id == character_id)
            if status is not None:
                query = query.filter(SessionModel.status == status)
            if after_updated_at is not None and after_id is not None:
                after = format_storage_datetime(after_updated_at)
                query = query.having(
                    or_(
                        desc("updated_at").element < after,
                        (desc("updated_at").element == after)
                        & (SessionModel.id < after_id),
                    )
                )
            rows = query.order_by(desc("updated_at"), SessionModel.id.desc()).limit(limit).all()
        return [self._from_row(row) for row in rows]

    def update_session_title(
        self,
        *,
        session_id: str,
        user_id: str,
        title: str | None,
    ) -> SessionRecord:
        with self._session_factory() as session:
            now = utc_now()
            affected = (
                session.query(SessionModel)
                .filter(
                    SessionModel.id == session_id,
                    SessionModel.user_id == user_id,
                    SessionModel.status.in_(("active", "ended")),
                )
                .update(
                    {
                        SessionModel.title: title,
                        SessionModel.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if affected == 0:
                session_record = self._get_with_session(session, session_id=session_id)
                if session_record is None:
                    raise SessionNotFoundError(f"session {session_id!r} was not found")
                self._validate_session_ownership(session_record, user_id=user_id)
                if session_record.status == "archived":
                    raise SessionArchivedError(f"session {session_id!r} is archived")
                raise SessionTransitionError(
                    f"session {session_id!r} title update was not applied"
                )
            session.flush()
            updated = self._get_with_session(session, session_id=session_id)
            session.commit()
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def end_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        with self._session_factory() as session:
            now = utc_now()
            affected = (
                session.query(SessionModel)
                .filter(
                    SessionModel.id == session_id,
                    SessionModel.user_id == user_id,
                    SessionModel.status == "active",
                )
                .update(
                    {
                        SessionModel.status: "ended",
                        SessionModel.ended_at: now,
                        SessionModel.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if affected == 0:
                session_record = self._classify_lifecycle_zero_update(
                    session,
                    session_id=session_id,
                    user_id=user_id,
                )
                if session_record.status == "ended":
                    session.commit()
                    return session_record
                if session_record.status == "archived":
                    raise SessionArchivedError(f"session {session_id!r} is archived")
                raise SessionTransitionError(f"session {session_id!r} could not be ended")
            updated = self._get_with_session(session, session_id=session_id)
            session.commit()
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def resume_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        with self._session_factory() as session:
            now = utc_now()
            affected = (
                session.query(SessionModel)
                .filter(
                    SessionModel.id == session_id,
                    SessionModel.user_id == user_id,
                    SessionModel.character_id == character_id,
                    SessionModel.status == "ended",
                )
                .update(
                    {
                        SessionModel.status: "active",
                        SessionModel.ended_at: None,
                        SessionModel.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if affected == 0:
                session_record = self._classify_lifecycle_zero_update(
                    session,
                    session_id=session_id,
                    user_id=user_id,
                    character_id=character_id,
                )
                if session_record.status == "archived":
                    raise SessionArchivedError(f"session {session_id!r} is archived")
                raise SessionTransitionError(
                    f"session {session_id!r} could not be resumed"
                )
            updated = self._get_with_session(session, session_id=session_id)
            session.commit()
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def archive_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        with self._session_factory() as session:
            now = utc_now()
            affected = (
                session.query(SessionModel)
                .filter(
                    SessionModel.id == session_id,
                    SessionModel.user_id == user_id,
                    SessionModel.status.in_(("active", "ended")),
                )
                .update(
                    {
                        SessionModel.status: "archived",
                        SessionModel.archived_at: now,
                        SessionModel.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if affected == 0:
                session_record = self._classify_lifecycle_zero_update(
                    session,
                    session_id=session_id,
                    user_id=user_id,
                )
                if session_record.status == "archived":
                    session.commit()
                    return session_record
                raise SessionTransitionError(
                    f"session {session_id!r} could not be archived"
                )
            updated = self._get_with_session(session, session_id=session_id)
            session.commit()
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def _classify_lifecycle_zero_update(
        self,
        session,
        *,
        session_id: str,
        user_id: str,
        character_id: str | None = None,
    ) -> SessionRecord:
        session_record = self._get_with_session(session, session_id=session_id)
        if session_record is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        self._validate_session_ownership(
            session_record,
            user_id=user_id,
            character_id=character_id,
        )
        return session_record
