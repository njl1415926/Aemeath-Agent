from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Index, Integer, LargeBinary, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    emotion_label: Mapped[str | None] = mapped_column(Text)
    importance_score: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("idx_messages_session_created", "session_id", "created_at"),
    )


class AccountModel(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    roles_json: Mapped[str] = mapped_column(Text, nullable=False, default='["user"]')
    dev_token_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_login_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_accounts_status"),
        Index("idx_accounts_dev_token_hash", "dev_token_hash"),
    )


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    character_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'ended', 'archived')",
            name="ck_sessions_status",
        ),
        Index("idx_sessions_user_status_updated", "user_id", "status", "updated_at"),
        Index(
            "idx_sessions_user_character_status_updated",
            "user_id",
            "character_id",
            "status",
            "updated_at",
        ),
    )


class LongTermMemoryModel(Base):
    __tablename__ = "long_term_memories"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="user_global")
    scope_character_id: Mapped[str | None] = mapped_column(Text)
    scope_session_id: Mapped[str | None] = mapped_column(Text)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(Text)
    source_session_id: Mapped[str | None] = mapped_column(Text)
    source_job_id: Mapped[str | None] = mapped_column(Text)
    source_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    related_memory_ids: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    last_used_at: Mapped[str | None] = mapped_column(Text)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decay_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text)
    expiry: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        Index("idx_memories_user_status", "user_id", "status", "is_active", "importance"),
        Index(
            "idx_memories_scope_visibility",
            "user_id",
            "scope",
            "scope_character_id",
            "scope_session_id",
            "status",
            "is_active",
        ),
        Index("idx_memories_source_job", "source_job_id"),
    )


class MemoryExtractionJobModel(Base):
    __tablename__ = "memory_extraction_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    character_id: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_memory_jobs_status_created", "status", "created_at"),
    )


class LongTermMemoryEmbeddingModel(Base):
    __tablename__ = "long_term_memory_embeddings"

    memory_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    vector_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_memory_embeddings_user", "user_id"),
    )


class RelationshipStateModel(Base):
    __tablename__ = "relationship_states"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    character_id: Mapped[str] = mapped_column(Text, nullable=False)
    mood: Mapped[str | None] = mapped_column(Text)
    user_emotion: Mapped[str | None] = mapped_column(Text)
    trust: Mapped[int | None] = mapped_column(Integer)
    affection: Mapped[int | None] = mapped_column(Integer)
    familiarity: Mapped[int | None] = mapped_column(Integer)
    current_scene: Mapped[str | None] = mapped_column(Text)
    last_topic: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "idx_relationship_user_character",
            "user_id",
            "character_id",
            unique=True,
        ),
    )
