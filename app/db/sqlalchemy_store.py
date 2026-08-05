from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.accounts.schemas import AccountRecord, AccountStatus
from app.auth.schemas import StoreSchemaInfo
from app.db.repositories import (
    AccountRepository,
    BrowserAuthRepository,
    MemoryEmbeddingRepository,
    MemoryJobRepository,
    MemoryRepository,
    MessageRepository,
    RelationshipRepository,
    SessionRepository,
    StreamTurnRepository,
)
from app.db.session import (
    create_session_factory,
    create_sqlite_engine,
    initialize_database,
)
from app.db.stream_turns import (
    StreamRequestRecord,
    StreamTurnComplete,
    StreamTurnFail,
    StreamTurnResolveOrReserve,
    StreamTurnResolveOutcome,
)
from app.embeddings.artifacts import EmbeddingArtifactUpsertOutcome, EmbeddingArtifactWrite
from app.embeddings.schemas import EmbeddingModelSignature
from app.memory.schemas import (
    ExactActiveMemoryResult,
    MemoryArtifactEligibility,
    MemoryEmbeddingArtifactGapCursor,
    MemoryEmbeddingArtifactGapPage,
    MemoryEmbeddingSearchOutcome,
    MemoryExtractionJobRecord,
    MemoryJobEnqueueResult,
    MemoryVectorSearchResult,
)
from app.schemas import MemoryRecord, MessageRecord, RelationshipState
from app.session.schemas import SessionRecord, SessionStatus


class SqlAlchemyStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.engine = create_sqlite_engine(db_path)
        initialize_database(self.engine, db_path=db_path)
        self.session_factory = create_session_factory(self.engine)

        self.accounts = AccountRepository(self.session_factory)
        self.browser_auth = BrowserAuthRepository(db_path=db_path, backend="sqlalchemy")
        self.messages = MessageRepository(self.session_factory)
        self.sessions = SessionRepository(self.session_factory)
        self.relationships = RelationshipRepository(self.session_factory)
        self.stream_turns = StreamTurnRepository(self.session_factory)
        self.memories = MemoryRepository(self.session_factory)
        self.memory_jobs = MemoryJobRepository(self.session_factory)
        self.memory_embeddings = MemoryEmbeddingRepository(self.session_factory)
        self.accounts.bootstrap_default_account()

    def get_account(self, *, account_id: str) -> AccountRecord | None:
        return self.accounts.get_account(account_id=account_id)

    def get_account_by_dev_token_hash(
        self,
        *,
        dev_token_hash: str,
    ) -> AccountRecord | None:
        return self.accounts.get_account_by_dev_token_hash(
            dev_token_hash=dev_token_hash,
        )

    def upsert_account(
        self,
        *,
        account_id: str,
        username: str,
        display_name: str | None,
        status: AccountStatus,
        roles: list[str],
        dev_token_hash: str | None = None,
    ) -> AccountRecord:
        return self.accounts.upsert_account(
            account_id=account_id,
            username=username,
            display_name=display_name,
            status=status,
            roles=roles,
            dev_token_hash=dev_token_hash,
        )

    def set_account_dev_token_hash(
        self,
        *,
        account_id: str,
        dev_token_hash: str | None,
    ) -> AccountRecord:
        return self.accounts.set_account_dev_token_hash(
            account_id=account_id,
            dev_token_hash=dev_token_hash,
        )

    def set_account_status(
        self,
        *,
        account_id: str,
        status: AccountStatus,
    ) -> AccountRecord:
        return self.accounts.set_account_status(account_id=account_id, status=status)

    def touch_account_login(self, *, account_id: str) -> AccountRecord:
        return self.accounts.touch_account_login(account_id=account_id)

    def get_store_schema_info(self) -> StoreSchemaInfo:
        return self.browser_auth.get_store_schema_info()

    def ensure_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        return self.sessions.ensure_session(
            session_id=session_id,
            user_id=user_id,
            character_id=character_id,
            metadata=metadata,
        )

    def create_session(
        self,
        *,
        user_id: str,
        character_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        return self.sessions.create_session(
            user_id=user_id,
            character_id=character_id,
            title=title,
            metadata=metadata,
        )

    def get_session(self, *, session_id: str) -> SessionRecord | None:
        return self.sessions.get_session(session_id=session_id)

    def require_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        return self.sessions.require_session(
            session_id=session_id,
            user_id=user_id,
            character_id=character_id,
        )

    def list_sessions(
        self,
        *,
        user_id: str,
        character_id: str | None = None,
        status: SessionStatus | None = "active",
        limit: int = 50,
    ) -> list[SessionRecord]:
        return self.sessions.list_sessions(
            user_id=user_id,
            character_id=character_id,
            status=status,
            limit=limit,
        )

    def end_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        return self.sessions.end_session(session_id=session_id, user_id=user_id)

    def resume_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        return self.sessions.resume_session(
            session_id=session_id,
            user_id=user_id,
            character_id=character_id,
        )

    def archive_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        return self.sessions.archive_session(session_id=session_id, user_id=user_id)

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
        return self.messages.save_message(
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            emotion_label=emotion_label,
            importance_score=importance_score,
        )

    def get_recent_messages(self, *, session_id: str, limit: int) -> list[MessageRecord]:
        return self.messages.get_recent_messages(session_id=session_id, limit=limit)

    def get_message(self, *, message_id: str) -> MessageRecord | None:
        return self.messages.get_message(message_id=message_id)

    def resolve_or_reserve(
        self,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome:
        return self.stream_turns.resolve_or_reserve(request=request)

    def complete(self, *, request: StreamTurnComplete) -> StreamRequestRecord:
        return self.stream_turns.complete(request=request)

    def fail(self, *, request: StreamTurnFail) -> StreamRequestRecord:
        return self.stream_turns.fail(request=request)

    def fail_orphan_if_owned_by_other_runtime(
        self,
        *,
        owner_runtime_id: str,
        limit: int = 100,
    ) -> int:
        return self.stream_turns.fail_orphan_if_owned_by_other_runtime(
            owner_runtime_id=owner_runtime_id,
            limit=limit,
        )

    def list_messages_by_session(self, *, session_id: str) -> list[MessageRecord]:
        return self.messages.list_messages_by_session(session_id=session_id)

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
        return self.memories.create_memory(
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
            source_message_ids=source_message_ids,
            related_memory_ids=related_memory_ids,
            expiry=expiry,
        )

    def get_memory(self, *, memory_id: str) -> MemoryRecord | None:
        return self.memories.get_memory(memory_id=memory_id)

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
        return self.memories.create_or_get_exact_active_memory(
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
            source_message_ids=source_message_ids,
            related_memory_ids=related_memory_ids,
            expiry=expiry,
        )

    def list_active_memories(self, *, user_id: str, limit: int = 50) -> list[MemoryRecord]:
        return self.memories.list_active_memories(user_id=user_id, limit=limit)

    def list_recallable_memories(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        return self.memories.list_recallable_memories(
            user_id=user_id,
            character_id=character_id,
            session_id=session_id,
            limit=limit,
        )

    def search_memories_keyword(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        keywords: list[str],
        limit: int = 8,
    ) -> list[MemoryRecord]:
        return self.memories.search_memories_keyword(
            user_id=user_id,
            character_id=character_id,
            session_id=session_id,
            keywords=keywords,
            limit=limit,
        )

    def has_exact_active_memory(
        self,
        *,
        user_id: str,
        scope: str,
        scope_character_id: str | None,
        scope_session_id: str | None,
        content: str,
    ) -> bool:
        return self.memories.has_exact_active_memory(
            user_id=user_id,
            scope=scope,
            scope_character_id=scope_character_id,
            scope_session_id=scope_session_id,
            content=content,
        )

    def mark_memory_used(self, *, memory_id: str) -> None:
        self.memories.mark_memory_used(memory_id=memory_id)

    def archive_memory(self, *, memory_id: str, user_id: str) -> bool:
        return self.memories.archive_memory(memory_id=memory_id, user_id=user_id)

    def disable_memory(self, *, memory_id: str, user_id: str) -> bool:
        return self.memories.disable_memory(memory_id=memory_id, user_id=user_id)

    def list_unprocessed_window_messages(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[MessageRecord]:
        return self.memory_jobs.list_unprocessed_window_messages(
            session_id=session_id,
            limit=limit,
        )

    def enqueue_memory_extraction_job(
        self,
        *,
        idempotency_key: str,
        user_id: str,
        session_id: str,
        character_id: str,
        trigger_type: str,
        source_message_ids: list[str],
        input_payload: dict[str, Any],
    ) -> MemoryJobEnqueueResult:
        return self.memory_jobs.enqueue_memory_extraction_job(
            idempotency_key=idempotency_key,
            user_id=user_id,
            session_id=session_id,
            character_id=character_id,
            trigger_type=trigger_type,
            source_message_ids=source_message_ids,
            input_payload=input_payload,
        )

    def claim_next_memory_extraction_job(
        self,
        *,
        worker_id: str,
    ) -> MemoryExtractionJobRecord | None:
        return self.memory_jobs.claim_next_memory_extraction_job(worker_id=worker_id)

    def complete_memory_extraction_job(
        self,
        *,
        job_id: str,
        result_payload: dict[str, Any],
    ) -> None:
        self.memory_jobs.complete_memory_extraction_job(
            job_id=job_id,
            result_payload=result_payload,
        )

    def fail_memory_extraction_job(self, *, job_id: str, error: str) -> None:
        self.memory_jobs.fail_memory_extraction_job(job_id=job_id, error=error)

    def list_memory_extraction_jobs(
        self,
        *,
        session_id: str | None = None,
    ) -> list[MemoryExtractionJobRecord]:
        return self.memory_jobs.list_memory_extraction_jobs(session_id=session_id)

    def upsert_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_model: str,
        embedding: np.ndarray,
    ) -> None:
        self.memory_embeddings.upsert_memory_embedding(
            memory_id=memory_id,
            user_id=user_id,
            embedding_model=embedding_model,
            embedding=embedding,
        )

    def search_memory_embeddings(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> list[MemoryVectorSearchResult]:
        return self.memory_embeddings.search_memory_embeddings(
            user_id=user_id,
            character_id=character_id,
            session_id=session_id,
            query_embedding=query_embedding,
            limit=limit,
            candidate_limit=candidate_limit,
        )

    def upsert_memory_embedding_artifact(
        self,
        *,
        memory_id: str,
        user_id: str,
        artifact: EmbeddingArtifactWrite,
    ) -> EmbeddingArtifactUpsertOutcome:
        return self.memory_embeddings.upsert_memory_embedding_artifact(
            memory_id=memory_id,
            user_id=user_id,
            artifact=artifact,
        )

    def search_memory_embedding_artifacts(
        self,
        *,
        eligibility: MemoryArtifactEligibility,
        query_signature: EmbeddingModelSignature,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> MemoryEmbeddingSearchOutcome:
        return self.memory_embeddings.search_memory_embedding_artifacts(
            eligibility=eligibility,
            query_signature=query_signature,
            query_embedding=query_embedding,
            limit=limit,
            candidate_limit=candidate_limit,
        )

    def list_memory_embedding_artifact_gaps(
        self,
        *,
        query_signature: EmbeddingModelSignature,
        user_id: str | None = None,
        cursor: MemoryEmbeddingArtifactGapCursor | None = None,
        limit: int = 50,
    ) -> MemoryEmbeddingArtifactGapPage:
        return self.memory_embeddings.list_memory_embedding_artifact_gaps(
            query_signature=query_signature,
            user_id=user_id,
            cursor=cursor,
            limit=limit,
        )

    def get_or_create_relationship_state(
        self,
        *,
        user_id: str,
        character_id: str,
    ) -> RelationshipState:
        return self.relationships.get_or_create_relationship_state(
            user_id=user_id,
            character_id=character_id,
        )

    def update_relationship_state(
        self,
        *,
        user_id: str,
        character_id: str,
        mood: str,
        user_emotion: str,
        trust_delta: int,
        affection_delta: int,
        familiarity_delta: int,
        last_topic: str | None,
    ) -> RelationshipState:
        return self.relationships.update_relationship_state(
            user_id=user_id,
            character_id=character_id,
            mood=mood,
            user_emotion=user_emotion,
            trust_delta=trust_delta,
            affection_delta=affection_delta,
            familiarity_delta=familiarity_delta,
            last_topic=last_topic,
        )
