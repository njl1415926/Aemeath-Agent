from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    and_,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.stream_turns import STREAM_TURN_TERMINAL_REASON_VALUES


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
    username_normalized: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
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


class AccountCredentialModel(Base):
    __tablename__ = "account_credentials"

    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("accounts.id", ondelete="RESTRICT"), primary_key=True
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    password_changed_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("credential_version >= 1", name="ck_account_credential_version"),
    )


class AccountInvitationModel(Base):
    __tablename__ = "account_invitations"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    token_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    username_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    roles_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[str | None] = mapped_column(Text)
    accepted_account_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("accounts.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'used', 'revoked')",
            name="ck_account_invitation_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND used_at IS NULL AND revoked_at IS NULL "
            "AND accepted_account_id IS NULL) OR "
            "(status = 'used' AND used_at IS NOT NULL AND revoked_at IS NULL "
            "AND accepted_account_id IS NOT NULL) OR "
            "(status = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL "
            "AND accepted_account_id IS NULL)",
            name="ck_account_invitation_lifecycle",
        ),
        Index(
            "uq_account_invitations_pending_username",
            "username_normalized",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "idx_account_invitations_status_expiry",
            "status",
            "expires_at",
            "created_at",
            "id",
        ),
        Index(
            "idx_account_invitations_username_status",
            "username_normalized",
            "status",
        ),
    )


class AccountPasswordResetModel(Base):
    __tablename__ = "account_password_resets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    token_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "issued_credential_version >= 0",
            name="ck_account_password_reset_version",
        ),
        CheckConstraint(
            "status IN ('pending', 'used', 'revoked')",
            name="ck_account_password_reset_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND used_at IS NULL AND revoked_at IS NULL) OR "
            "(status = 'used' AND used_at IS NOT NULL AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL)",
            name="ck_account_password_reset_lifecycle",
        ),
        Index(
            "uq_account_password_resets_pending_account",
            "account_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "idx_account_password_resets_status_expiry",
            "status",
            "expires_at",
            "created_at",
            "id",
        ),
        Index(
            "idx_account_password_resets_account_status",
            "account_id",
            "status",
        ),
    )


class WebAuthSessionModel(Base):
    __tablename__ = "web_auth_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    token_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    csrf_verifier: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    authenticated_credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    idle_expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    absolute_expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(Text)
    revoke_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "authenticated_credential_version >= 1",
            name="ck_web_auth_session_credential_version",
        ),
        CheckConstraint("length(csrf_verifier) = 32", name="ck_web_auth_session_csrf"),
        CheckConstraint(
            "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_web_auth_session_revocation",
        ),
        Index(
            "idx_web_auth_sessions_account_active",
            "account_id",
            "revoked_at",
            "absolute_expires_at",
        ),
        Index(
            "idx_web_auth_sessions_absolute_expiry", "absolute_expires_at", "id"
        ),
        Index("idx_web_auth_sessions_revoked", "revoked_at", "id"),
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


class ChatRequestIdempotencyModel(Base):
    __tablename__ = "chat_request_idempotency"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sessions.id", ondelete="RESTRICT"), nullable=False
    )
    client_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    active_character_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("messages.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    owner_runtime_id: Mapped[str] = mapped_column(Text, nullable=False)
    warnings_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    state_delta_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'{}'")
    )
    terminal_error_code: Mapped[str | None] = mapped_column(Text)
    terminal_retryable: Mapped[int | None] = mapped_column(Integer)
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text)
    failed_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "request_fingerprint_version >= 1",
            name="ck_chat_request_idempotency_fingerprint_version",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="ck_chat_request_idempotency_status",
        ),
        CheckConstraint(
            "terminal_retryable IN (0, 1) OR terminal_retryable IS NULL",
            name="ck_chat_request_idempotency_retryable",
        ),
        CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason IN ("
            + ", ".join(f"'{reason}'" for reason in STREAM_TURN_TERMINAL_REASON_VALUES)
            + ")",
            name="ck_chat_request_idempotency_reason",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND assistant_message_id IS NULL "
            "AND completed_at IS NULL AND failed_at IS NULL "
            "AND terminal_error_code IS NULL AND terminal_retryable IS NULL "
            "AND terminal_reason IS NULL AND warnings_json = '[]' "
            "AND state_delta_json = '{}') OR "
            "(status = 'completed' AND assistant_message_id IS NOT NULL "
            "AND completed_at IS NOT NULL AND failed_at IS NULL "
            "AND terminal_error_code IS NULL AND terminal_retryable IS NULL "
            "AND terminal_reason IS NULL) OR "
            "(status = 'failed' AND assistant_message_id IS NULL "
            "AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND terminal_error_code IS NOT NULL AND terminal_retryable IN (0, 1) "
            "AND terminal_reason IS NOT NULL AND warnings_json = '[]' "
            "AND state_delta_json = '{}')",
            name="ck_chat_request_idempotency_lifecycle",
        ),
        Index(
            "uq_chat_request_idempotency_scope",
            "user_id",
            "session_id",
            "client_message_id",
            unique=True,
        ),
        Index("uq_chat_request_idempotency_turn", "turn_id", unique=True),
        Index(
            "idx_chat_request_idempotency_orphan",
            "status",
            "owner_runtime_id",
            "updated_at",
            "id",
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
        Index(
            "uq_memories_active_exact_identity",
            "user_id",
            "scope",
            func.coalesce(scope_character_id, ""),
            func.coalesce(scope_session_id, ""),
            "content",
            unique=True,
            sqlite_where=and_(status == "active", is_active == 1),
        ),
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


class LongTermMemoryEmbeddingArtifactModel(Base):
    __tablename__ = "long_term_memory_embedding_artifacts"

    memory_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_signature_json: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_signature_fingerprint: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_purpose: Mapped[str] = mapped_column(Text, primary_key=True)
    embedding_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_vector_format: Mapped[str] = mapped_column(Text, nullable=False)
    vector_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    embedding_vector_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "embedding_purpose = 'document'",
            name="ck_memory_embedding_artifact_purpose",
        ),
        CheckConstraint(
            "embedding_vector_format = 'f32le-v1'",
            name="ck_memory_embedding_artifact_vector_format",
        ),
        Index(
            "idx_memory_embedding_artifacts_user_signature",
            "user_id",
            "embedding_signature_fingerprint",
            "embedding_purpose",
        ),
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
