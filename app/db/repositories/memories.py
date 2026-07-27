from __future__ import annotations

from sqlalchemy import and_, func, or_

from app.db.models import LongTermMemoryModel
from app.db.repositories.common import SessionFactory, dump_json, load_json_list
from app.memory.schemas import ExactActiveMemoryResult
from app.schemas import MemoryRecord
from app.utils.ids import new_id, utc_now


class MemoryRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _from_model(self, model: LongTermMemoryModel) -> MemoryRecord:
        source_message_ids = [
            str(message_id) for message_id in load_json_list(model.source_message_ids_json)
        ]
        return MemoryRecord(
            id=model.id,
            user_id=model.user_id,
            namespace=model.namespace,
            memory_type=model.memory_type,
            content=model.content,
            importance=model.importance,
            confidence=model.confidence,
            source=model.source,
            source_message_id=model.source_message_id,
            related_memory_ids=model.related_memory_ids,
            version=model.version,
            status=model.status,
            last_used_at=model.last_used_at,
            use_count=model.use_count,
            decay_score=model.decay_score,
            created_at=model.created_at,
            updated_at=model.updated_at,
            expiry=model.expiry,
            is_active=model.is_active,
            scope=model.scope,
            scope_character_id=model.scope_character_id,
            scope_session_id=model.scope_session_id,
            source_session_id=model.source_session_id,
            source_job_id=model.source_job_id,
            source_message_ids=source_message_ids,
        )

    def _scope_filter(self, *, character_id: str | None, session_id: str | None):
        return or_(
            LongTermMemoryModel.scope == "user_global",
            and_(
                LongTermMemoryModel.scope == "character",
                LongTermMemoryModel.scope_character_id == character_id,
            ),
            and_(
                LongTermMemoryModel.scope == "session",
                LongTermMemoryModel.scope_character_id == character_id,
                LongTermMemoryModel.scope_session_id == session_id,
            ),
        )

    def _validate_memory_scope(
        self,
        *,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
    ) -> None:
        if scope == "user_global":
            if scope_character_id is not None or scope_session_id is not None:
                raise ValueError("user_global memory cannot have scope IDs")
            return
        if scope == "character":
            if scope_character_id is None or scope_session_id is not None:
                raise ValueError(
                    "character memory requires scope_character_id and no scope_session_id"
                )
            return
        if scope == "session":
            if scope_character_id is None or scope_session_id is None:
                raise ValueError(
                    "session memory requires scope_character_id and scope_session_id"
                )
            return
        raise ValueError(f"unsupported memory scope: {scope}")

    def _exact_active_filter(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        content: str,
    ):
        return (
            LongTermMemoryModel.user_id == user_id,
            LongTermMemoryModel.status == "active",
            LongTermMemoryModel.is_active == 1,
            LongTermMemoryModel.scope == scope,
            func.coalesce(LongTermMemoryModel.scope_character_id, "")
            == (scope_character_id or ""),
            func.coalesce(LongTermMemoryModel.scope_session_id, "")
            == (scope_session_id or ""),
            LongTermMemoryModel.content == content,
        )

    def create_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        namespace: str,
        memory_type: str,
        content: str,
        importance: int,
        confidence: float,
        source: str,
        source_message_id: str | None,
        source_session_id: str | None,
        source_job_id: str | None,
        source_message_ids: list[str] | None = None,
        related_memory_ids: list[str] | None = None,
        expiry: str | None = None,
    ) -> str:
        self._validate_memory_scope(
            scope=scope,
            scope_character_id=scope_character_id,
            scope_session_id=scope_session_id,
        )
        memory_id = new_id("memory")
        with self._session_factory() as session:
            session.add(
                LongTermMemoryModel(
                    id=memory_id,
                    user_id=user_id,
                    scope=scope,
                    scope_character_id=scope_character_id,
                    scope_session_id=scope_session_id,
                    namespace=namespace,
                    memory_type=memory_type,
                    content=content,
                    importance=importance,
                    confidence=confidence,
                    source=source,
                    source_message_id=source_message_id,
                    source_session_id=source_session_id,
                    source_job_id=source_job_id,
                    source_message_ids_json=dump_json(source_message_ids or []),
                    related_memory_ids=dump_json(
                        related_memory_ids or [],
                        ensure_ascii=True,
                    ),
                    version=1,
                    status="active",
                    last_used_at=None,
                    use_count=0,
                    decay_score=None,
                    created_at=utc_now(),
                    updated_at=None,
                    expiry=expiry,
                    is_active=1,
                )
            )
            session.commit()
        return memory_id

    def create_or_get_exact_active_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        namespace: str,
        memory_type: str,
        content: str,
        importance: int,
        confidence: float,
        source: str,
        source_message_id: str | None,
        source_session_id: str | None,
        source_job_id: str | None,
        source_message_ids: list[str] | None = None,
        related_memory_ids: list[str] | None = None,
        expiry: str | None = None,
    ) -> ExactActiveMemoryResult:
        self._validate_memory_scope(
            scope=scope,
            scope_character_id=scope_character_id,
            scope_session_id=scope_session_id,
        )
        memory_id = new_id("memory")
        with self._session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            existing = (
                session.query(LongTermMemoryModel.id)
                .filter(
                    *self._exact_active_filter(
                        user_id=user_id,
                        scope=scope,
                        scope_character_id=scope_character_id,
                        scope_session_id=scope_session_id,
                        content=content,
                    )
                )
                .limit(1)
                .one_or_none()
            )
            if existing is not None:
                session.commit()
                return ExactActiveMemoryResult(memory_id=existing[0], created=False)
            session.add(
                LongTermMemoryModel(
                    id=memory_id,
                    user_id=user_id,
                    scope=scope,
                    scope_character_id=scope_character_id,
                    scope_session_id=scope_session_id,
                    namespace=namespace,
                    memory_type=memory_type,
                    content=content,
                    importance=importance,
                    confidence=confidence,
                    source=source,
                    source_message_id=source_message_id,
                    source_session_id=source_session_id,
                    source_job_id=source_job_id,
                    source_message_ids_json=dump_json(source_message_ids or []),
                    related_memory_ids=dump_json(
                        related_memory_ids or [],
                        ensure_ascii=True,
                    ),
                    version=1,
                    status="active",
                    last_used_at=None,
                    use_count=0,
                    decay_score=None,
                    created_at=utc_now(),
                    updated_at=None,
                    expiry=expiry,
                    is_active=1,
                )
            )
            session.flush()
            session.commit()
        return ExactActiveMemoryResult(memory_id=memory_id, created=True)

    def get_memory(self, *, memory_id: str) -> MemoryRecord | None:
        with self._session_factory() as session:
            memory = session.get(LongTermMemoryModel, memory_id)
            return self._from_model(memory) if memory else None

    def list_active_memories(self, *, user_id: str, limit: int = 50) -> list[MemoryRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(LongTermMemoryModel)
                .filter(
                    LongTermMemoryModel.user_id == user_id,
                    LongTermMemoryModel.status == "active",
                    LongTermMemoryModel.is_active == 1,
                )
                .order_by(
                    LongTermMemoryModel.importance.desc(),
                    LongTermMemoryModel.created_at.desc(),
                )
                .limit(limit)
                .all()
            )
        return [self._from_model(row) for row in rows]

    def list_recallable_memories(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(LongTermMemoryModel)
                .filter(
                    LongTermMemoryModel.user_id == user_id,
                    LongTermMemoryModel.status == "active",
                    LongTermMemoryModel.is_active == 1,
                    self._scope_filter(
                        character_id=character_id,
                        session_id=session_id,
                    ),
                )
                .order_by(
                    LongTermMemoryModel.importance.desc(),
                    LongTermMemoryModel.created_at.desc(),
                )
                .limit(limit)
                .all()
            )
        return [self._from_model(row) for row in rows]

    def search_memories_keyword(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        keywords: list[str],
        limit: int = 8,
    ) -> list[MemoryRecord]:
        tokens = [token.strip() for token in keywords if token.strip()]
        if not tokens:
            return []
        keyword_filter = or_(
            *[LongTermMemoryModel.content.like(f"%{token}%") for token in tokens]
        )
        with self._session_factory() as session:
            rows = (
                session.query(LongTermMemoryModel)
                .filter(
                    LongTermMemoryModel.user_id == user_id,
                    LongTermMemoryModel.status == "active",
                    LongTermMemoryModel.is_active == 1,
                    self._scope_filter(
                        character_id=character_id,
                        session_id=session_id,
                    ),
                    keyword_filter,
                )
                .order_by(
                    LongTermMemoryModel.importance.desc(),
                    LongTermMemoryModel.use_count.desc(),
                    LongTermMemoryModel.created_at.desc(),
                )
                .limit(limit)
                .all()
            )
        return [self._from_model(row) for row in rows]

    def has_exact_active_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        content: str,
    ) -> bool:
        self._validate_memory_scope(
            scope=scope,
            scope_character_id=scope_character_id,
            scope_session_id=scope_session_id,
        )
        with self._session_factory() as session:
            exists = (
                session.query(LongTermMemoryModel.id)
                .filter(
                    *self._exact_active_filter(
                        user_id=user_id,
                        scope=scope,
                        scope_character_id=scope_character_id,
                        scope_session_id=scope_session_id,
                        content=content,
                    )
                )
                .limit(1)
                .one_or_none()
            )
        return exists is not None

    def mark_memory_used(self, *, memory_id: str) -> None:
        with self._session_factory() as session:
            memory = session.get(LongTermMemoryModel, memory_id)
            if memory is not None:
                memory.use_count += 1
                memory.last_used_at = utc_now()
            session.commit()

    def archive_memory(self, *, memory_id: str, user_id: str) -> bool:
        with self._session_factory() as session:
            memory = (
                session.query(LongTermMemoryModel)
                .filter(
                    LongTermMemoryModel.id == memory_id,
                    LongTermMemoryModel.user_id == user_id,
                )
                .limit(1)
                .one_or_none()
            )
            if memory is None:
                return False
            memory.status = "archived"
            memory.is_active = 0
            memory.updated_at = utc_now()
            session.commit()
            return True

    def disable_memory(self, *, memory_id: str, user_id: str) -> bool:
        with self._session_factory() as session:
            memory = (
                session.query(LongTermMemoryModel)
                .filter(
                    LongTermMemoryModel.id == memory_id,
                    LongTermMemoryModel.user_id == user_id,
                )
                .limit(1)
                .one_or_none()
            )
            if memory is None:
                return False
            memory.is_active = 0
            memory.updated_at = utc_now()
            session.commit()
            return True
