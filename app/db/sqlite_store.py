from __future__ import annotations

import json
import sqlite3
import warnings
from pathlib import Path
from typing import Any

import numpy as np

from app.accounts.schemas import AccountRecord, AccountStatus
from app.auth.schemas import StoreSchemaInfo
from app.db.browser_auth_migrations import ensure_browser_auth_schema
from app.db.errors import StorageMigrationRequiredError
from app.db.memory_artifact_contract import (
    StoredMemoryEmbeddingArtifact,
    classify_memory_embedding_artifact_gap,
)
from app.db.repositories.browser_auth import BrowserAuthRepository
from app.db.sqlite_index_contract import (
    ACTIVE_MEMORY_IDENTITY_INDEX,
    has_valid_active_memory_identity_index,
)
from app.db.stream_turns import (
    StreamRequestRecord,
    StreamTurnComplete,
    StreamTurnFail,
    StreamTurnResolveOrReserve,
    StreamTurnResolveOutcome,
    apply_stream_turn_state_update,
    parse_stream_turn_state_delta,
    parse_stream_turn_warnings,
    stream_turn_state_delta_json,
    stream_turn_warnings_json,
)
from app.db.streaming_chat_migrations import (
    STREAMING_CHAT_IDEMPOTENCY_MIGRATION_IDS,
    STREAMING_CHAT_IDEMPOTENCY_TABLE,
    ensure_streaming_chat_schema,
)
from app.embeddings.artifacts import (
    EmbeddingArtifactDiagnostics,
    EmbeddingArtifactUpsertOutcome,
    EmbeddingArtifactValidationError,
    EmbeddingArtifactWrite,
    canonical_content_hash,
    canonicalize_embedding_text,
    deterministic_hash_v1_legacy_signature,
    diagnostics_from_reasons,
    embedding_content_hash,
    is_promoted_deterministic_hash_legacy,
    prepare_embedding_artifact,
    prepare_embedding_query,
    validate_stored_embedding_artifact,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingError
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingModelSignature, EmbeddingRequest
from app.memory.schemas import (
    ExactActiveMemoryResult,
    MemoryArtifactEligibility,
    MemoryEmbeddingArtifactGap,
    MemoryEmbeddingArtifactGapCursor,
    MemoryEmbeddingArtifactGapPage,
    MemoryEmbeddingSearchOutcome,
    MemoryExtractionJobRecord,
    MemoryJobEnqueueResult,
    MemoryVectorSearchResult,
)
from app.schemas import MemoryRecord, MessageRecord, RelationshipState
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionMigrationError,
    SessionNotFoundError,
    SessionOwnershipError,
    SessionTransitionError,
)
from app.session.pagination import format_storage_datetime
from app.session.schemas import SessionRecord, SessionStatus
from app.utils.ids import new_id, utc_now

MEMORY_SCOPE_MIGRATION_VERSION = "memory_scope_provenance_v1"
MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION = "memory_embedding_artifact_v1"
MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION = "memory_exact_active_identity_v1"
_MAX_MEMORY_ARTIFACT_GAP_LIMIT = 100
_MAX_MEMORY_ARTIFACT_GAP_SCAN_LIMIT = 400
DEFAULT_ACCOUNT_ID = "user_default"
DEFAULT_ACCOUNT_ROLES_JSON = json.dumps(["user"], ensure_ascii=True)


def _clamp_stat(value: int) -> int:
    return max(0, min(100, value))


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.browser_auth = BrowserAuthRepository(db_path=db_path, backend="sqlite_raw")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise StorageMigrationRequiredError(reason="foreign_keys_not_enabled")
        return connection

    def _init_db(self) -> None:
        self._preflight_streaming_chat_schema()
        schema_path = Path(__file__).with_name("schema.sql")
        schema_sql = schema_path.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.executescript(schema_sql)
            self._migrate_relationship_states(conn)
            self._migrate_session_columns(conn)
            self._backfill_sessions(conn)
            self._backfill_accounts(conn)
            conn.commit()
            self._migrate_memory_scope_provenance(conn)
            self._migrate_memory_exact_active_identity(conn)
            self._migrate_memory_embedding_artifacts(conn)
        ensure_browser_auth_schema(self.db_path)
        ensure_streaming_chat_schema(self.db_path)

    def _preflight_streaming_chat_schema(self) -> None:
        """Reject marked drift before schema.sql can create a missing object."""

        with self._connect() as conn:
            migration_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            placeholders = ", ".join(
                "?" for _ in STREAMING_CHAT_IDEMPOTENCY_MIGRATION_IDS
            )
            marker = (
                conn.execute(
                    "SELECT 1 FROM schema_migrations "
                    f"WHERE version IN ({placeholders})",
                    STREAMING_CHAT_IDEMPOTENCY_MIGRATION_IDS,
                ).fetchone()
                if migration_table is not None
                else None
            )
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (STREAMING_CHAT_IDEMPOTENCY_TABLE,),
            ).fetchone()
        if table is not None:
            ensure_streaming_chat_schema(self.db_path)
        elif marker is not None:
            raise StorageMigrationRequiredError(reason="streaming_chat_table_missing")

    def _has_valid_active_memory_identity_index(self, conn: sqlite3.Connection) -> bool:
        index_list_rows = conn.execute(
            "PRAGMA index_list('long_term_memories')"
        ).fetchall()
        index_xinfo_rows = conn.execute(
            f"PRAGMA index_xinfo('{ACTIVE_MEMORY_IDENTITY_INDEX}')"
        ).fetchall()
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (ACTIVE_MEMORY_IDENTITY_INDEX,),
        ).fetchone()
        return has_valid_active_memory_identity_index(
            index_list_rows=index_list_rows,
            index_xinfo_rows=index_xinfo_rows,
            index_sql=row["sql"] if row is not None else None,
        )

    def _migrate_memory_exact_active_identity(self, conn: sqlite3.Connection) -> None:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION,),
        ).fetchone()
        if applied:
            if not self._has_valid_active_memory_identity_index(conn):
                raise StorageMigrationRequiredError(
                    reason="raw_active_memory_identity_index_invalid"
                )
            return

        try:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                """
                SELECT 1
                FROM long_term_memories
                WHERE status = 'active' AND is_active = 1
                GROUP BY
                    user_id,
                    scope,
                    COALESCE(scope_character_id, ''),
                    COALESCE(scope_session_id, ''),
                    content
                HAVING COUNT(*) > 1
                LIMIT 1
                """
            ).fetchone()
            if duplicate is not None:
                raise StorageMigrationRequiredError(
                    reason="raw_active_memory_identity_duplicates"
                )
            if not self._has_valid_active_memory_identity_index(conn):
                existing_index = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (ACTIVE_MEMORY_IDENTITY_INDEX,),
                ).fetchone()
                if existing_index is not None:
                    raise StorageMigrationRequiredError(
                        reason="raw_active_memory_identity_index_invalid"
                    )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX uq_memories_active_exact_identity
                    ON long_term_memories (
                        user_id,
                        scope,
                        COALESCE(scope_character_id, ''),
                        COALESCE(scope_session_id, ''),
                        content
                    )
                    WHERE status = 'active' AND is_active = 1
                    """
                )
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION, utc_now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate_memory_embedding_artifacts(self, conn: sqlite3.Connection) -> None:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
        ).fetchone()
        if applied:
            return

        try:
            conn.execute("BEGIN IMMEDIATE")
            profile = load_embedding_space_profile("deterministic_hash_v1")
            provider = DeterministicHashEmbeddingProvider(profile=profile)
            rows = conn.execute(
                """
                SELECT e.memory_id, e.user_id, e.embedding_model, e.vector_dim, e.embedding,
                       m.content
                FROM long_term_memory_embeddings AS e
                INNER JOIN long_term_memories AS m ON m.id = e.memory_id
                WHERE e.user_id = m.user_id
                """
            ).fetchall()
            for row in rows:
                if row["embedding_model"] != "deterministic_hash_v1" or row["vector_dim"] != 384:
                    continue
                try:
                    canonical_text = canonicalize_embedding_text(row["content"])
                    result = provider.embed(
                        EmbeddingRequest(texts=(canonical_text,), purpose="document")
                    )
                    artifact = prepare_embedding_artifact(
                        EmbeddingArtifactWrite(
                            signature=result.signature,
                            content_hash=embedding_content_hash(canonical_text),
                            vector=result.vectors[0],
                        )
                    )
                    if bytes(row["embedding"]) != artifact.vector_bytes:
                        continue
                except (EmbeddingError, TypeError, ValueError, OverflowError, UnicodeError):
                    # Legacy records are evidence, not a startup precondition. A row
                    # that cannot be verified remains legacy while valid rows migrate.
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO long_term_memory_embedding_artifacts (
                        memory_id, user_id, embedding_signature_json,
                        embedding_signature_fingerprint, embedding_purpose,
                        embedding_content_hash, embedding_vector_format, vector_dim,
                        embedding, embedding_vector_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        row["memory_id"],
                        row["user_id"],
                        artifact.signature_json,
                        artifact.signature_fingerprint,
                        artifact.purpose,
                        artifact.content_hash,
                        artifact.vector_format,
                        artifact.vector_dim,
                        artifact.vector_bytes,
                        artifact.vector_sha256,
                        utc_now(),
                    ),
                )
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION, utc_now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate_memory_scope_provenance(self, conn: sqlite3.Connection) -> None:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MEMORY_SCOPE_MIGRATION_VERSION,),
        ).fetchone()
        if applied:
            return

        try:
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(long_term_memories)").fetchall()
            }
            additions = {
                "scope": "TEXT NOT NULL DEFAULT 'user_global'",
                "scope_character_id": "TEXT",
                "scope_session_id": "TEXT",
                "source_session_id": "TEXT",
                "source_job_id": "TEXT",
                "source_message_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE long_term_memories ADD COLUMN {name} {definition}"
                    )

            memory_rows = conn.execute(
                """
                SELECT id, user_id, source_message_id, related_memory_ids
                FROM long_term_memories
                """
            ).fetchall()
            memory_ids = {row["id"] for row in memory_rows}
            message_rows = conn.execute(
                "SELECT id, user_id, session_id FROM messages"
            ).fetchall()
            message_provenance = {
                row["id"]: {
                    "user_id": row["user_id"],
                    "session_id": row["session_id"],
                }
                for row in message_rows
            }
            job_sources = self._memory_job_sources(conn)

            for row in memory_rows:
                source_ids: list[str] = []
                related_ids: list[str] = []
                legacy_related = json.loads(row["related_memory_ids"] or "[]")
                if not isinstance(legacy_related, list):
                    raise ValueError(
                        f"memory {row['id']!r} related_memory_ids must be a JSON list"
                    )
                for raw_id in legacy_related:
                    item_id = str(raw_id)
                    if item_id in message_provenance:
                        if message_provenance[item_id]["user_id"] != row["user_id"]:
                            raise ValueError(
                                f"memory {row['id']!r} references another user's message"
                            )
                        source_ids.append(item_id)
                    else:
                        related_ids.append(item_id)
                        if item_id not in memory_ids:
                            warnings.warn(
                                "memory scope migration preserved unknown legacy reference "
                                f"{item_id!r} on memory {row['id']!r}",
                                RuntimeWarning,
                                stacklevel=2,
                            )

                source_message_id = row["source_message_id"]
                if source_message_id and source_message_id in message_provenance:
                    if message_provenance[source_message_id]["user_id"] != row["user_id"]:
                        raise ValueError(
                            f"memory {row['id']!r} has another user's source message"
                        )
                    source_ids.insert(0, source_message_id)
                source_ids = list(dict.fromkeys(source_ids))
                related_ids = list(dict.fromkeys(related_ids))

                source_sessions = {
                    message_provenance[message_id]["session_id"]
                    for message_id in source_ids
                    if message_id in message_provenance
                }
                if len(source_sessions) > 1:
                    raise ValueError(
                        f"memory {row['id']!r} has source messages from multiple sessions"
                    )
                source_session_id = next(iter(source_sessions), None)
                source_job_id = None
                job_source = job_sources.get(row["id"])
                if job_source is not None:
                    if job_source["user_id"] != row["user_id"]:
                        raise ValueError(
                            f"memory {row['id']!r} belongs to a different user than its job"
                        )
                    if (
                        source_session_id is not None
                        and source_session_id != job_source["session_id"]
                    ):
                        raise ValueError(
                            f"memory {row['id']!r} has conflicting source Session provenance"
                        )
                    source_job_id = job_source["job_id"]
                    if source_session_id is None:
                        source_session_id = job_source["session_id"]

                conn.execute(
                    """
                    UPDATE long_term_memories
                    SET scope = 'user_global',
                        scope_character_id = NULL,
                        scope_session_id = NULL,
                        source_session_id = ?,
                        source_job_id = ?,
                        source_message_ids_json = ?,
                        related_memory_ids = ?
                    WHERE id = ?
                    """,
                    (
                        source_session_id,
                        source_job_id,
                        json.dumps(source_ids, ensure_ascii=False),
                        json.dumps(related_ids, ensure_ascii=False),
                        row["id"],
                    ),
                )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_scope_visibility
                ON long_term_memories (
                    user_id, scope, scope_character_id, scope_session_id,
                    status, is_active
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_source_job
                ON long_term_memories (source_job_id)
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (MEMORY_SCOPE_MIGRATION_VERSION, utc_now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _memory_job_sources(
        self,
        conn: sqlite3.Connection,
    ) -> dict[str, dict[str, str]]:
        has_jobs = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_extraction_jobs'
            """
        ).fetchone()
        if not has_jobs:
            return {}

        sources: dict[str, dict[str, str]] = {}
        rows = conn.execute(
            """
            SELECT id, user_id, session_id, result_json
            FROM memory_extraction_jobs
            WHERE status = 'completed' AND result_json IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            result = json.loads(row["result_json"])
            memory_ids = result.get("created_memory_ids", []) if isinstance(result, dict) else []
            if not isinstance(memory_ids, list):
                raise ValueError(
                    f"memory job {row['id']!r} created_memory_ids must be a JSON list"
                )
            for memory_id in memory_ids:
                memory_id = str(memory_id)
                existing = sources.get(memory_id)
                current = {
                    "job_id": row["id"],
                    "user_id": row["user_id"],
                    "session_id": row["session_id"],
                }
                if existing is not None and existing != current:
                    raise ValueError(
                        f"memory {memory_id!r} is attributed to multiple extraction jobs"
                    )
                sources[memory_id] = current
        return sources

    def _migrate_session_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "ended_at" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN ended_at TEXT")
        if "archived_at" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")

    def _backfill_sessions(self, conn: sqlite3.Connection) -> None:
        message_rows = conn.execute(
            """
            SELECT
                session_id,
                COUNT(DISTINCT user_id) AS owner_count,
                MIN(user_id) AS user_id,
                MIN(created_at) AS created_at,
                MAX(created_at) AS updated_at
            FROM messages
            GROUP BY session_id
            """
        ).fetchall()
        message_provenance: dict[str, dict[str, str]] = {}
        for row in message_rows:
            if row["owner_count"] != 1:
                raise SessionMigrationError(
                    f"legacy session {row['session_id']!r} has multiple message owners"
                )
            message_provenance[row["session_id"]] = {
                "user_id": row["user_id"],
                "character_id": "aemeath",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

        job_provenance: dict[str, dict[str, str]] = {}
        has_jobs = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_extraction_jobs'
            """
        ).fetchone()
        if has_jobs:
            job_rows = conn.execute(
                """
                SELECT
                    session_id,
                    COUNT(DISTINCT user_id || char(0) || character_id) AS owner_count,
                    MIN(user_id) AS user_id,
                    MIN(character_id) AS character_id,
                    MIN(created_at) AS created_at,
                    MAX(COALESCE(updated_at, created_at)) AS updated_at
                FROM memory_extraction_jobs
                GROUP BY session_id
                """
            ).fetchall()
            for row in job_rows:
                if row["owner_count"] != 1:
                    raise SessionMigrationError(
                        f"legacy session {row['session_id']!r} has conflicting memory jobs"
                    )
                job_provenance[row["session_id"]] = {
                    "user_id": row["user_id"],
                    "character_id": row["character_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }

        for session_id in set(message_provenance) | set(job_provenance):
            messages = message_provenance.get(session_id)
            jobs = job_provenance.get(session_id)
            existing = conn.execute(
                """
                SELECT user_id, character_id
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing:
                if messages and existing["user_id"] != messages["user_id"]:
                    raise SessionMigrationError(
                        f"session {session_id!r} conflicts with message ownership"
                    )
                if jobs and (
                    existing["user_id"] != jobs["user_id"]
                    or existing["character_id"] != jobs["character_id"]
                ):
                    raise SessionMigrationError(
                        f"session {session_id!r} conflicts with memory-job provenance"
                    )
                continue

            if messages and jobs and messages["user_id"] != jobs["user_id"]:
                raise SessionMigrationError(
                    f"legacy session {session_id!r} has conflicting message/job provenance"
                )

            provenance = jobs or messages
            if provenance is None:
                continue
            created_at = provenance["created_at"]
            updated_at = provenance["updated_at"]
            if messages and jobs:
                created_at = min(messages["created_at"], jobs["created_at"])
                updated_at = max(messages["updated_at"], jobs["updated_at"])
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    id, user_id, character_id, status, title, metadata_json,
                    created_at, updated_at, ended_at, archived_at
                )
                VALUES (?, ?, ?, 'active', NULL, '{}', ?, ?, NULL, NULL)
                """,
                (
                    session_id,
                    provenance["user_id"],
                    provenance["character_id"],
                    created_at,
                    updated_at,
                ),
            )

    def _table_has_column(
        self,
        conn: sqlite3.Connection,
        *,
        table_name: str,
        column_name: str,
    ) -> bool:
        table = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        if table is None:
            return False
        return any(
            row["name"] == column_name
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        )

    def _distinct_user_ids_from_table(
        self,
        conn: sqlite3.Connection,
        *,
        table_name: str,
    ) -> set[str]:
        if not self._table_has_column(
            conn,
            table_name=table_name,
            column_name="user_id",
        ):
            return set()
        rows = conn.execute(
            f"""
            SELECT DISTINCT user_id
            FROM {table_name}
            WHERE user_id IS NOT NULL AND user_id != ''
            """
        ).fetchall()
        return {row["user_id"] for row in rows}

    def _backfill_accounts(self, conn: sqlite3.Connection) -> None:
        user_ids = {DEFAULT_ACCOUNT_ID}
        for table_name in [
            "sessions",
            "messages",
            "relationship_states",
            "long_term_memories",
            "memory_extraction_jobs",
        ]:
            user_ids.update(
                self._distinct_user_ids_from_table(conn, table_name=table_name)
            )

        now = utc_now()
        has_normalized = self._table_has_column(
            conn, table_name="accounts", column_name="username_normalized"
        )
        for user_id in sorted(user_ids):
            display_name = "Default User" if user_id == DEFAULT_ACCOUNT_ID else user_id
            if has_normalized:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO accounts (
                        id, username, username_normalized, display_name, status, roles_json,
                        dev_token_hash, created_at, updated_at, last_login_at
                    )
                    VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, NULL)
                    """,
                    (
                        user_id,
                        user_id,
                        user_id.lower(),
                        display_name,
                        DEFAULT_ACCOUNT_ROLES_JSON,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO accounts (
                        id, username, display_name, status, roles_json,
                        dev_token_hash, created_at, updated_at, last_login_at
                    )
                    VALUES (?, ?, ?, 'active', ?, NULL, ?, ?, NULL)
                    """,
                    (
                        user_id,
                        user_id,
                        display_name,
                        DEFAULT_ACCOUNT_ROLES_JSON,
                        now,
                        now,
                    ),
                )

    def _account_from_row(self, row: sqlite3.Row) -> AccountRecord:
        return AccountRecord(
            id=row["id"],
            username=row["username"],
            username_normalized=row["username_normalized"],
            display_name=row["display_name"],
            status=row["status"],
            roles=json.loads(row["roles_json"]),
            dev_token_hash=row["dev_token_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_login_at=row["last_login_at"],
        )

    def get_account(self, *, account_id: str) -> AccountRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM accounts
                WHERE id = ?
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return self._account_from_row(row) if row else None

    def get_account_by_dev_token_hash(
        self,
        *,
        dev_token_hash: str,
    ) -> AccountRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM accounts
                WHERE dev_token_hash = ?
                LIMIT 1
                """,
                (dev_token_hash,),
            ).fetchone()
        return self._account_from_row(row) if row else None

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
        now = utc_now()
        roles_json = json.dumps(roles, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    id, username, username_normalized, display_name, status, roles_json,
                    dev_token_hash, created_at, updated_at, last_login_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    status = excluded.status,
                    roles_json = excluded.roles_json,
                    dev_token_hash = excluded.dev_token_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    username,
                    username.lower(),
                    display_name,
                    status,
                    roles_json,
                    dev_token_hash,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM accounts
                WHERE id = ?
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"account {account_id!r} was not persisted")
        return self._account_from_row(row)

    def get_store_schema_info(self) -> StoreSchemaInfo:
        return self.browser_auth.get_store_schema_info()

    def set_account_dev_token_hash(
        self,
        *,
        account_id: str,
        dev_token_hash: str | None,
    ) -> AccountRecord:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET dev_token_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (dev_token_hash, utc_now(), account_id),
            )
            row = conn.execute(
                """
                SELECT *
                FROM accounts
                WHERE id = ?
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"account {account_id!r} was not found")
        return self._account_from_row(row)

    def set_account_status(
        self,
        *,
        account_id: str,
        status: AccountStatus,
    ) -> AccountRecord:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, utc_now(), account_id),
            )
            row = conn.execute(
                """
                SELECT *
                FROM accounts
                WHERE id = ?
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"account {account_id!r} was not found")
        return self._account_from_row(row)

    def touch_account_login(self, *, account_id: str) -> AccountRecord:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET last_login_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, account_id),
            )
            row = conn.execute(
                """
                SELECT *
                FROM accounts
                WHERE id = ?
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"account {account_id!r} was not found")
        return self._account_from_row(row)

    def _session_select_sql(self) -> str:
        return """
            SELECT
                s.id,
                s.user_id,
                s.character_id,
                s.status,
                s.title,
                s.metadata_json,
                s.created_at,
                CASE
                    WHEN MAX(m.created_at) IS NOT NULL
                         AND MAX(m.created_at) > s.updated_at
                    THEN MAX(m.created_at)
                    ELSE s.updated_at
                END AS updated_at,
                s.ended_at,
                s.archived_at,
                MAX(m.created_at) AS last_message_at,
                COUNT(m.id) AS message_count
            FROM sessions AS s
            LEFT JOIN messages AS m ON m.session_id = s.id
        """

    def _session_from_row(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            user_id=row["user_id"],
            character_id=row["character_id"],
            status=row["status"],
            title=row["title"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            archived_at=row["archived_at"],
            last_message_at=row["last_message_at"],
            message_count=row["message_count"],
        )

    def _get_session_with_conn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
    ) -> SessionRecord | None:
        row = conn.execute(
            self._session_select_sql()
            + """
            WHERE s.id = ?
            GROUP BY s.id
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return self._session_from_row(row) if row else None

    def ensure_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        now = utc_now()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    id, user_id, character_id, status, title, metadata_json,
                    created_at, updated_at, ended_at, archived_at
                )
                VALUES (?, ?, ?, 'active', NULL, ?, ?, ?, NULL, NULL)
                """,
                (session_id, user_id, character_id, metadata_json, now, now),
            )
            session = self._get_session_with_conn(conn, session_id=session_id)
            if session is None:
                raise SessionNotFoundError(f"session {session_id!r} was not created")
            self._validate_session_ownership(
                session,
                user_id=user_id,
                character_id=character_id,
            )
            if session.status == "ended":
                raise SessionEndedError(f"session {session_id!r} has ended")
            if session.status == "archived":
                raise SessionArchivedError(f"session {session_id!r} is archived")
            return session

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
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, user_id, character_id, status, title, metadata_json,
                    created_at, updated_at, ended_at, archived_at
                )
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?, NULL, NULL)
                """,
                (session_id, user_id, character_id, title, metadata_json, now, now),
            )
            session = self._get_session_with_conn(conn, session_id=session_id)
        if session is None:
            raise SessionNotFoundError(f"session {session_id!r} was not created")
        return session

    def get_session(self, *, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            return self._get_session_with_conn(conn, session_id=session_id)

    def require_session(
        self,
        *,
        session_id: str,
        user_id: str,
        character_id: str,
    ) -> SessionRecord:
        with self._connect() as conn:
            session = self._require_owned_session(
                conn,
                session_id=session_id,
                user_id=user_id,
                character_id=character_id,
            )
        if session.status == "archived":
            raise SessionArchivedError(f"session {session_id!r} is archived")
        return session

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
        after_updated_at=None,
        after_id: str | None = None,
    ) -> list[SessionRecord]:
        if limit <= 0:
            return []
        conditions = ["s.user_id = ?"]
        params: list[Any] = [user_id]
        if character_id is not None:
            conditions.append("s.character_id = ?")
            params.append(character_id)
        if status is not None:
            conditions.append("s.status = ?")
            params.append(status)
        having = ""
        if after_updated_at is not None and after_id is not None:
            having = """
                HAVING updated_at < ?
                   OR (updated_at = ? AND s.id < ?)
            """
            after = format_storage_datetime(after_updated_at)
            params.extend([after, after, after_id])
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                self._session_select_sql()
                + f"""
                WHERE {" AND ".join(conditions)}
                GROUP BY s.id
                {having}
                ORDER BY updated_at DESC, s.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def update_session_title(
        self,
        *,
        session_id: str,
        user_id: str,
        title: str | None,
    ) -> SessionRecord:
        with self._connect() as conn:
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE sessions
                SET title = ?, updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status IN ('active', 'ended')
                """,
                (title, now, session_id, user_id),
            )
            if cursor.rowcount == 0:
                session = self._get_session_with_conn(conn, session_id=session_id)
                if session is None:
                    raise SessionNotFoundError(f"session {session_id!r} was not found")
                self._validate_session_ownership(session, user_id=user_id)
                if session.status == "archived":
                    raise SessionArchivedError(f"session {session_id!r} is archived")
                raise SessionTransitionError(
                    f"session {session_id!r} title update was not applied"
                )
            updated = self._get_session_with_conn(conn, session_id=session_id)
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def end_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        with self._connect() as conn:
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE sessions
                SET status = 'ended', ended_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'active'
                """,
                (now, now, session_id, user_id),
            )
            if cursor.rowcount == 0:
                session = self._classify_lifecycle_zero_update(
                    conn,
                    session_id=session_id,
                    user_id=user_id,
                )
                if session.status == "ended":
                    return session
                if session.status == "archived":
                    raise SessionArchivedError(f"session {session_id!r} is archived")
                raise SessionTransitionError(f"session {session_id!r} could not be ended")
            updated = self._get_session_with_conn(conn, session_id=session_id)
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
        with self._connect() as conn:
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE sessions
                SET status = 'active', ended_at = NULL, updated_at = ?
                WHERE id = ? AND user_id = ? AND character_id = ? AND status = 'ended'
                """,
                (now, session_id, user_id, character_id),
            )
            if cursor.rowcount == 0:
                session = self._classify_lifecycle_zero_update(
                    conn,
                    session_id=session_id,
                    user_id=user_id,
                    character_id=character_id,
                )
                if session.status == "archived":
                    raise SessionArchivedError(f"session {session_id!r} is archived")
                raise SessionTransitionError(
                    f"session {session_id!r} could not be resumed"
                )
            updated = self._get_session_with_conn(conn, session_id=session_id)
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def archive_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        with self._connect() as conn:
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE sessions
                SET status = 'archived', archived_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status IN ('active', 'ended')
                """,
                (now, now, session_id, user_id),
            )
            if cursor.rowcount == 0:
                session = self._classify_lifecycle_zero_update(
                    conn,
                    session_id=session_id,
                    user_id=user_id,
                )
                if session.status == "archived":
                    return session
                raise SessionTransitionError(
                    f"session {session_id!r} could not be archived"
                )
            updated = self._get_session_with_conn(conn, session_id=session_id)
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def _classify_lifecycle_zero_update(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        user_id: str,
        character_id: str | None = None,
    ) -> SessionRecord:
        session = self._get_session_with_conn(conn, session_id=session_id)
        if session is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        self._validate_session_ownership(
            session,
            user_id=user_id,
            character_id=character_id,
        )
        return session

    def _validate_session_ownership(
        self,
        session: SessionRecord,
        *,
        user_id: str,
        character_id: str | None = None,
    ) -> None:
        if session.user_id != user_id:
            raise SessionOwnershipError(
                f"session {session.id!r} belongs to a different user"
            )
        if character_id is not None and session.character_id != character_id:
            raise SessionOwnershipError(
                f"session {session.id!r} belongs to a different character"
            )

    def _require_owned_session(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        user_id: str,
        character_id: str | None = None,
    ) -> SessionRecord:
        session = self._get_session_with_conn(conn, session_id=session_id)
        if session is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        self._validate_session_ownership(
            session,
            user_id=user_id,
            character_id=character_id,
        )
        return session

    def _job_from_row(self, row: sqlite3.Row) -> MemoryExtractionJobRecord:
        data = dict(row)
        return MemoryExtractionJobRecord(
            id=data["id"],
            idempotency_key=data["idempotency_key"],
            user_id=data["user_id"],
            session_id=data["session_id"],
            character_id=data["character_id"],
            trigger_type=data["trigger_type"],
            status=data["status"],
            source_message_ids=json.loads(data["source_message_ids_json"]),
            input_payload=json.loads(data["input_json"]),
            result_payload=json.loads(data["result_json"]) if data["result_json"] else None,
            error=data["error"],
            attempt_count=data["attempt_count"],
            locked_by=data["locked_by"],
            locked_at=data["locked_at"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def _migrate_relationship_states(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_states)").fetchall()
        }
        if "character_name" in columns and "character_id" not in columns:
            conn.execute("ALTER TABLE relationship_states ADD COLUMN character_id TEXT")
            conn.execute(
                """
                UPDATE relationship_states
                SET character_id = character_name
                WHERE character_id IS NULL
                """
            )
        if "character_id" in columns or "character_name" in columns:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_user_character_id
                ON relationship_states (user_id, character_id)
                """
            )

    def _relationship_columns(self, conn: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in conn.execute("PRAGMA table_info(relationship_states)").fetchall()
        }

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages
                (
                    id, session_id, user_id, role, content, created_at, emotion_label,
                    importance_score
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    user_id,
                    role,
                    content,
                    utc_now(),
                    emotion_label,
                    importance_score,
                ),
            )
        return message_id

    def get_recent_messages(self, *, session_id: str, limit: int) -> list[MessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, session_id, user_id, role, content, created_at, emotion_label,
                    importance_score
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        rows = list(reversed(rows))
        return [MessageRecord(**dict(row)) for row in rows]

    def get_message(self, *, message_id: str) -> MessageRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id, session_id, user_id, role, content, created_at, emotion_label,
                    importance_score
                FROM messages
                WHERE id = ?
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return MessageRecord(**dict(row)) if row else None

    def list_messages_by_session(self, *, session_id: str) -> list[MessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, session_id, user_id, role, content, created_at, emotion_label,
                    importance_score
                FROM messages
                WHERE session_id = ?
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()
        return [MessageRecord(**dict(row)) for row in rows]

    def list_messages_by_session_page(
        self,
        *,
        session_id: str,
        limit: int,
        before_created_at=None,
        before_id: str | None = None,
    ) -> list[MessageRecord]:
        if limit <= 0:
            return []
        conditions = ["session_id = ?"]
        params: list[Any] = [session_id]
        if before_created_at is not None and before_id is not None:
            before = format_storage_datetime(before_created_at)
            conditions.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([before, before, before_id])
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    id, session_id, user_id, role, content, created_at, emotion_label,
                    importance_score
                FROM messages
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [MessageRecord(**dict(row)) for row in rows]

    def list_unprocessed_window_messages(
        self,
        *,
        session_id: str,
        limit: int,
    ) -> list[MessageRecord]:
        messages = self.list_messages_by_session(session_id=session_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_message_ids_json
                FROM memory_extraction_jobs
                WHERE session_id = ?
                  AND trigger_type = 'window'
                  AND status IN ('pending', 'running', 'completed')
                """,
                (session_id,),
            ).fetchall()
        covered_ids: set[str] = set()
        for row in rows:
            covered_ids.update(json.loads(row["source_message_ids_json"]))
        return [message for message in messages if message.id not in covered_ids][:limit]

    def _memory_from_row(self, row: sqlite3.Row) -> MemoryRecord:
        data = dict(row)
        source_message_ids_json = data.pop("source_message_ids_json", "[]")
        source_message_ids = json.loads(source_message_ids_json or "[]")
        if not isinstance(source_message_ids, list):
            raise ValueError("source_message_ids_json must be a JSON list")
        data["source_message_ids"] = [str(message_id) for message_id in source_message_ids]
        return MemoryRecord(**data)

    def _memory_scope_filter(
        self,
        *,
        alias: str = "",
    ) -> str:
        prefix = f"{alias}." if alias else ""
        return f"""
            (
                {prefix}scope = 'user_global'
                OR (
                    {prefix}scope = 'character'
                    AND {prefix}scope_character_id = ?
                )
                OR (
                    {prefix}scope = 'session'
                    AND {prefix}scope_character_id = ?
                    AND {prefix}scope_session_id = ?
                )
            )
        """

    def _memory_scope_params(
        self,
        *,
        character_id: str | None,
        session_id: str | None,
    ) -> list[str | None]:
        return [character_id, character_id, session_id]

    def _memory_artifact_eligibility_predicate(
        self,
        eligibility: MemoryArtifactEligibility,
        *,
        alias: str,
    ) -> tuple[str, list[Any]]:
        prefix = f"{alias}."
        clauses = [
            f"{prefix}user_id = ?",
            f"{prefix}status = 'active'",
            f"{prefix}is_active = 1",
            self._memory_scope_filter(alias=alias),
        ]
        params: list[Any] = [
            eligibility.user_id,
            *self._memory_scope_params(
                character_id=eligibility.character_id,
                session_id=eligibility.session_id,
            ),
        ]
        if eligibility.namespaces:
            placeholders = ", ".join("?" for _ in eligibility.namespaces)
            clauses.append(f"{prefix}namespace IN ({placeholders})")
            params.extend(eligibility.namespaces)
        if eligibility.memory_types:
            placeholders = ", ".join("?" for _ in eligibility.memory_types)
            clauses.append(f"{prefix}memory_type IN ({placeholders})")
            params.extend(eligibility.memory_types)
        if eligibility.min_confidence is not None:
            clauses.append(f"{prefix}confidence >= ?")
            params.append(eligibility.min_confidence)
        return " AND ".join(clauses), params

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO long_term_memories
                (
                    id, user_id, scope, scope_character_id, scope_session_id,
                    namespace, memory_type, content, importance, confidence, source,
                    source_message_id, source_session_id, source_job_id,
                    source_message_ids_json, related_memory_ids, version, status,
                    last_used_at, use_count, decay_score, created_at, updated_at,
                    expiry, is_active
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    1, 'active', NULL, 0, NULL, ?, NULL, ?, 1
                )
                """,
                (
                    memory_id,
                    user_id,
                    scope,
                    scope_character_id,
                    scope_session_id,
                    namespace,
                    memory_type,
                    content,
                    importance,
                    confidence,
                    source,
                    source_message_id,
                    source_session_id,
                    source_job_id,
                    json.dumps(source_message_ids or [], ensure_ascii=False),
                    json.dumps(related_memory_ids or [], ensure_ascii=True),
                    utc_now(),
                    expiry,
                ),
            )
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
        identity_params = (
            user_id,
            scope,
            scope_character_id,
            scope_session_id,
            content,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id
                FROM long_term_memories
                WHERE user_id = ?
                  AND status = 'active'
                  AND is_active = 1
                  AND scope = ?
                  AND COALESCE(scope_character_id, '') = COALESCE(?, '')
                  AND COALESCE(scope_session_id, '') = COALESCE(?, '')
                  AND content = ?
                LIMIT 1
                """,
                identity_params,
            ).fetchone()
            if existing is not None:
                return ExactActiveMemoryResult(memory_id=existing["id"], created=False)
            conn.execute(
                """
                INSERT INTO long_term_memories
                (
                    id, user_id, scope, scope_character_id, scope_session_id,
                    namespace, memory_type, content, importance, confidence, source,
                    source_message_id, source_session_id, source_job_id,
                    source_message_ids_json, related_memory_ids, version, status,
                    last_used_at, use_count, decay_score, created_at, updated_at,
                    expiry, is_active
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    1, 'active', NULL, 0, NULL, ?, NULL, ?, 1
                )
                """,
                (
                    memory_id,
                    user_id,
                    scope,
                    scope_character_id,
                    scope_session_id,
                    namespace,
                    memory_type,
                    content,
                    importance,
                    confidence,
                    source,
                    source_message_id,
                    source_session_id,
                    source_job_id,
                    json.dumps(source_message_ids or [], ensure_ascii=False),
                    json.dumps(related_memory_ids or [], ensure_ascii=True),
                    utc_now(),
                    expiry,
                ),
            )
        return ExactActiveMemoryResult(memory_id=memory_id, created=True)

    def get_memory(self, *, memory_id: str) -> MemoryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM long_term_memories
                WHERE id = ?
                LIMIT 1
                """,
                (memory_id,),
            ).fetchone()
        return self._memory_from_row(row) if row else None

    def list_active_memories(self, *, user_id: str, limit: int = 50) -> list[MemoryRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM long_term_memories
                WHERE user_id = ? AND status = 'active' AND is_active = 1
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def list_recallable_memories(
        self,
        *,
        user_id: str,
        character_id: str | None,
        session_id: str | None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        scope_filter = self._memory_scope_filter()
        params: list[Any] = [
            user_id,
            *self._memory_scope_params(
                character_id=character_id,
                session_id=session_id,
            ),
            limit,
        ]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM long_term_memories
                WHERE user_id = ?
                  AND status = 'active'
                  AND is_active = 1
                  AND {scope_filter}
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

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
        conditions = " OR ".join(["content LIKE ?"] * len(tokens))
        scope_filter = self._memory_scope_filter()
        params = [
            user_id,
            *self._memory_scope_params(
                character_id=character_id,
                session_id=session_id,
            ),
            *[f"%{token}%" for token in tokens],
            limit,
        ]
        query = f"""
            SELECT *
            FROM long_term_memories
            WHERE user_id = ?
              AND status = 'active'
              AND is_active = 1
              AND {scope_filter}
              AND ({conditions})
            ORDER BY importance DESC, use_count DESC, created_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._memory_from_row(row) for row in rows]

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM long_term_memories
                WHERE user_id = ?
                  AND status = 'active'
                  AND is_active = 1
                  AND scope = ?
                  AND COALESCE(scope_character_id, '') = COALESCE(?, '')
                  AND COALESCE(scope_session_id, '') = COALESCE(?, '')
                  AND content = ?
                LIMIT 1
                """,
                (
                    user_id,
                    scope,
                    scope_character_id,
                    scope_session_id,
                    content,
                ),
            ).fetchone()
        return row is not None

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
        job_id = new_id("memjob")
        now = utc_now()
        source_json = json.dumps(source_message_ids, ensure_ascii=False)
        input_json = json.dumps(input_payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO memory_extraction_jobs (
                        id, idempotency_key, user_id, session_id, character_id,
                        trigger_type, status, source_message_ids_json, input_json,
                        result_json, error, attempt_count, locked_by, locked_at,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, 0, NULL, NULL, ?, NULL)
                    """,
                    (
                        job_id,
                        idempotency_key,
                        user_id,
                        session_id,
                        character_id,
                        trigger_type,
                        source_json,
                        input_json,
                        now,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = conn.execute(
                """
                SELECT *
                FROM memory_extraction_jobs
                WHERE idempotency_key = ?
                LIMIT 1
                """,
                (idempotency_key,),
            ).fetchone()
        return MemoryJobEnqueueResult(job=self._job_from_row(row), created=created)

    def claim_next_memory_extraction_job(
        self,
        *,
        worker_id: str,
    ) -> MemoryExtractionJobRecord | None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM memory_extraction_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE memory_extraction_jobs
                SET status = 'running',
                    locked_by = ?,
                    locked_at = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (worker_id, now, now, row["id"]),
            )
            claimed = conn.execute(
                """
                SELECT *
                FROM memory_extraction_jobs
                WHERE id = ?
                LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
        return self._job_from_row(claimed)

    def complete_memory_extraction_job(
        self,
        *,
        job_id: str,
        result_payload: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_extraction_jobs
                SET status = 'completed',
                    result_json = ?,
                    error = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(result_payload, ensure_ascii=False, sort_keys=True), utc_now(), job_id),
            )

    def fail_memory_extraction_job(self, *, job_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_extraction_jobs
                SET status = 'failed',
                    error = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, utc_now(), job_id),
            )

    def list_memory_extraction_jobs(
        self,
        *,
        session_id: str | None = None,
    ) -> list[MemoryExtractionJobRecord]:
        with self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM memory_extraction_jobs
                    WHERE session_id = ?
                    ORDER BY created_at ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM memory_extraction_jobs
                    ORDER BY created_at ASC
                    """
                ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def upsert_memory_embedding(
        self,
        *,
        memory_id: str,
        user_id: str,
        embedding_model: str,
        embedding: np.ndarray,
    ) -> None:
        vector = np.asarray(embedding, dtype=np.float32)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO long_term_memory_embeddings (
                    memory_id, user_id, embedding_model, vector_dim, embedding,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(memory_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    embedding_model = excluded.embedding_model,
                    vector_dim = excluded.vector_dim,
                    embedding = excluded.embedding,
                    updated_at = excluded.created_at
                """,
                (
                    memory_id,
                    user_id,
                    embedding_model,
                    int(vector.shape[0]),
                    vector.tobytes(),
                    now,
                ),
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
        candidate_limit = candidate_limit or max(limit * 4, limit + 8)
        query = np.asarray(query_embedding, dtype=np.float32)
        results: list[MemoryVectorSearchResult] = []
        scope_filter = self._memory_scope_filter(alias="m")
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.memory_id, e.embedding
                FROM long_term_memory_embeddings AS e
                INNER JOIN long_term_memories AS m ON m.id = e.memory_id
                WHERE e.user_id = ?
                  AND m.user_id = ?
                  AND m.status = 'active'
                  AND m.is_active = 1
                  AND {scope_filter}
                """,
                (
                    user_id,
                    user_id,
                    *self._memory_scope_params(
                        character_id=character_id,
                        session_id=session_id,
                    ),
                ),
            ).fetchall()
        for row in rows:
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
            if vector.shape != query.shape:
                continue
            results.append(
                MemoryVectorSearchResult(
                    memory_id=row["memory_id"],
                    score=float(np.dot(query, vector)),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:candidate_limit]

    def upsert_memory_embedding_artifact(
        self,
        *,
        memory_id: str,
        user_id: str,
        artifact: EmbeddingArtifactWrite,
    ) -> EmbeddingArtifactUpsertOutcome:
        prepared = prepare_embedding_artifact(artifact)
        with self._connect() as conn:
            # Keep target-content verification and artifact identity resolution in
            # one SQLite writer transaction so a concurrent content edit cannot
            # bind a vector to a newer memory value.
            conn.execute("BEGIN IMMEDIATE")
            memory = conn.execute(
                """
                SELECT content
                FROM long_term_memories
                WHERE id = ? AND user_id = ?
                """,
                (memory_id, user_id),
            ).fetchone()
            if memory is None:
                raise ValueError("memory_embedding_artifact_target_not_found")
            if canonical_content_hash(memory["content"]) != prepared.content_hash:
                raise EmbeddingArtifactValidationError("content_hash_target_mismatch")

            existing = conn.execute(
                """
                SELECT user_id, embedding_signature_json, embedding_content_hash,
                       embedding_vector_sha256
                FROM long_term_memory_embedding_artifacts
                WHERE memory_id = ?
                  AND embedding_signature_fingerprint = ?
                  AND embedding_purpose = ?
                """,
                (
                    memory_id,
                    prepared.signature_fingerprint,
                    prepared.purpose,
                ),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO long_term_memory_embedding_artifacts (
                        memory_id, user_id, embedding_signature_json,
                        embedding_signature_fingerprint, embedding_purpose,
                        embedding_content_hash, embedding_vector_format, vector_dim,
                        embedding, embedding_vector_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        memory_id,
                        user_id,
                        prepared.signature_json,
                        prepared.signature_fingerprint,
                        prepared.purpose,
                        prepared.content_hash,
                        prepared.vector_format,
                        prepared.vector_dim,
                        prepared.vector_bytes,
                        prepared.vector_sha256,
                        utc_now(),
                    ),
                )
                return EmbeddingArtifactUpsertOutcome(status="created")
            if existing["embedding_signature_json"] != prepared.signature_json:
                raise EmbeddingArtifactValidationError("signature_fingerprint_collision")
            if existing["user_id"] != user_id:
                raise ValueError("memory_embedding_artifact_owner_mismatch")
            if existing["embedding_content_hash"] == prepared.content_hash:
                diagnostics = ()
                if existing["embedding_vector_sha256"] != prepared.vector_sha256:
                    diagnostics = diagnostics_from_reasons(["incoming_vector_differs"])
                return EmbeddingArtifactUpsertOutcome(status="reused", diagnostics=diagnostics)
            conn.execute(
                """
                UPDATE long_term_memory_embedding_artifacts
                SET embedding_content_hash = ?,
                    embedding_vector_format = ?,
                    vector_dim = ?,
                    embedding = ?,
                    embedding_vector_sha256 = ?,
                    updated_at = ?
                WHERE memory_id = ?
                  AND embedding_signature_fingerprint = ?
                  AND embedding_purpose = ?
                """,
                (
                    prepared.content_hash,
                    prepared.vector_format,
                    prepared.vector_dim,
                    prepared.vector_bytes,
                    prepared.vector_sha256,
                    utc_now(),
                    memory_id,
                    prepared.signature_fingerprint,
                    prepared.purpose,
                ),
            )
            return EmbeddingArtifactUpsertOutcome(status="replaced")

    def search_memory_embedding_artifacts(
        self,
        *,
        eligibility: MemoryArtifactEligibility,
        query_signature: EmbeddingModelSignature,
        query_embedding: np.ndarray,
        limit: int,
        candidate_limit: int | None = None,
    ) -> MemoryEmbeddingSearchOutcome:
        candidate_limit = candidate_limit or max(limit * 4, limit + 8)
        query = prepare_embedding_query(
            signature=query_signature,
            vector=query_embedding,
        )
        memory_filter, memory_filter_params = self._memory_artifact_eligibility_predicate(
            eligibility,
            alias="m",
        )
        legacy_hash_signature = deterministic_hash_v1_legacy_signature()
        with self._connect() as conn:
            conn.execute("BEGIN")
            eligible_memory_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM long_term_memories AS m
                WHERE {memory_filter}
                """,
                memory_filter_params,
            ).fetchone()["count"]
            rows = conn.execute(
                f"""
                SELECT a.memory_id, a.embedding_signature_json,
                       a.embedding_signature_fingerprint, a.embedding_purpose,
                       a.embedding_content_hash, a.embedding_vector_format,
                       a.vector_dim, a.embedding, a.embedding_vector_sha256,
                       m.content
                FROM long_term_memory_embedding_artifacts AS a
                INNER JOIN long_term_memories AS m ON m.id = a.memory_id
                WHERE a.user_id = m.user_id
                  AND a.embedding_signature_fingerprint = ?
                  AND a.embedding_purpose = 'document'
                  AND {memory_filter}
                """,
                (query.signature_fingerprint, *memory_filter_params),
            ).fetchall()
            artifact_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM long_term_memory_embedding_artifacts AS a
                INNER JOIN long_term_memories AS m ON m.id = a.memory_id
                WHERE a.user_id = m.user_id
                  AND a.embedding_purpose = 'document'
                  AND {memory_filter}
                """,
                memory_filter_params,
            ).fetchone()["count"]
            incompatible_signature_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM long_term_memory_embedding_artifacts AS a
                INNER JOIN long_term_memories AS m ON m.id = a.memory_id
                WHERE a.user_id = m.user_id
                  AND a.embedding_purpose = 'document'
                  AND a.embedding_signature_fingerprint != ?
                  AND {memory_filter}
                """,
                (query.signature_fingerprint, *memory_filter_params),
            ).fetchone()["count"]
            legacy_rows = conn.execute(
                f"""
                SELECT e.memory_id, e.embedding_model, e.vector_dim,
                       e.embedding AS legacy_embedding, m.content,
                       a.embedding_signature_json AS artifact_signature_json,
                       a.embedding_signature_fingerprint AS artifact_signature_fingerprint,
                       a.embedding_purpose AS artifact_purpose,
                       a.embedding_content_hash AS artifact_content_hash,
                       a.embedding_vector_format AS artifact_vector_format,
                       a.vector_dim AS artifact_vector_dim,
                       a.embedding AS artifact_embedding,
                       a.embedding_vector_sha256 AS artifact_vector_sha256
                FROM long_term_memory_embeddings AS e
                INNER JOIN long_term_memories AS m ON m.id = e.memory_id
                LEFT JOIN long_term_memory_embedding_artifacts AS a
                    ON a.memory_id = e.memory_id
                   AND a.user_id = e.user_id
                   AND a.embedding_signature_fingerprint = ?
                   AND a.embedding_purpose = 'document'
                WHERE e.user_id = m.user_id
                  AND {memory_filter}
                """,
                (
                    legacy_hash_signature.fingerprint(),
                    *memory_filter_params,
                ),
            ).fetchall()

        reasons: list[str] = ["signature_mismatch"] * incompatible_signature_count
        results: list[MemoryVectorSearchResult] = []
        compatible_count = 0
        for row in rows:
            if row["embedding_signature_json"] != query.signature_json:
                reasons.append("signature_mismatch")
                continue
            try:
                _signature, vector = validate_stored_embedding_artifact(
                    signature_json=row["embedding_signature_json"],
                    signature_fingerprint=row["embedding_signature_fingerprint"],
                    content_hash=row["embedding_content_hash"],
                    purpose=row["embedding_purpose"],
                    vector_format=row["embedding_vector_format"],
                    vector_dim=row["vector_dim"],
                    vector_bytes=row["embedding"],
                    vector_sha256=row["embedding_vector_sha256"],
                )
            except EmbeddingArtifactValidationError as exc:
                reasons.append(
                    "signature_corrupt"
                    if exc.reason.startswith("signature_")
                    else "vector_corrupt"
                )
                continue
            if canonical_content_hash(row["content"]) != row["embedding_content_hash"]:
                reasons.append("content_hash_stale")
                continue
            compatible_count += 1
            results.append(
                MemoryVectorSearchResult(
                    memory_id=row["memory_id"],
                    score=float(np.dot(query.vector, vector)),
                )
            )
        reasons.extend(
            "legacy_unverified"
            for row in legacy_rows
            if not is_promoted_deterministic_hash_legacy(
                embedding_model=row["embedding_model"],
                vector_dim=row["vector_dim"],
                legacy_vector_bytes=row["legacy_embedding"],
                target_content=row["content"],
                artifact_signature_json=row["artifact_signature_json"],
                artifact_signature_fingerprint=row["artifact_signature_fingerprint"],
                artifact_purpose=row["artifact_purpose"],
                artifact_content_hash=row["artifact_content_hash"],
                artifact_vector_format=row["artifact_vector_format"],
                artifact_vector_dim=row["artifact_vector_dim"],
                artifact_vector_bytes=row["artifact_embedding"],
                artifact_vector_sha256=row["artifact_vector_sha256"],
            )
        )
        results.sort(key=lambda item: item.score, reverse=True)
        return MemoryEmbeddingSearchOutcome(
            hits=results[:candidate_limit],
            diagnostics=EmbeddingArtifactDiagnostics(
                scanned_count=artifact_count + len(legacy_rows),
                compatible_count=compatible_count,
                issues=diagnostics_from_reasons(reasons),
            ),
            eligible_memory_count=eligible_memory_count,
        )

    def list_memory_embedding_artifact_gaps(
        self,
        *,
        query_signature: EmbeddingModelSignature,
        user_id: str | None = None,
        cursor: MemoryEmbeddingArtifactGapCursor | None = None,
        limit: int = 50,
    ) -> MemoryEmbeddingArtifactGapPage:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_MEMORY_ARTIFACT_GAP_LIMIT
        ):
            raise ValueError("memory_artifact_gap_limit_invalid")
        scan_limit = min(limit * 4, _MAX_MEMORY_ARTIFACT_GAP_SCAN_LIMIT)
        signature_json = query_signature.canonical()
        signature_fingerprint = query_signature.fingerprint()
        clauses = ["m.status = 'active'", "m.is_active = 1"]
        params: list[Any] = [signature_fingerprint]
        if user_id is not None:
            clauses.append("m.user_id = ?")
            params.append(user_id)
        if cursor is not None:
            clauses.append("(m.created_at > ? OR (m.created_at = ? AND m.id > ?))")
            params.extend((cursor.created_at, cursor.created_at, cursor.memory_id))
        params.append(scan_limit + 1)
        with self._connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                f"""
                SELECT
                    m.id AS memory_id,
                    m.user_id,
                    m.content,
                    m.created_at,
                    a.embedding_signature_json AS artifact_signature_json,
                    a.embedding_signature_fingerprint AS artifact_signature_fingerprint,
                    a.embedding_purpose AS artifact_purpose,
                    a.embedding_content_hash AS artifact_content_hash,
                    a.embedding_vector_format AS artifact_vector_format,
                    a.vector_dim AS artifact_vector_dim,
                    a.embedding AS artifact_embedding,
                    a.embedding_vector_sha256 AS artifact_vector_sha256,
                    EXISTS(
                        SELECT 1
                        FROM long_term_memory_embedding_artifacts AS any_artifact
                        WHERE any_artifact.memory_id = m.id
                          AND any_artifact.user_id = m.user_id
                          AND any_artifact.embedding_purpose = 'document'
                    ) AS has_any_canonical_artifact,
                    EXISTS(
                        SELECT 1
                        FROM long_term_memory_embeddings AS legacy
                        WHERE legacy.memory_id = m.id
                          AND legacy.user_id = m.user_id
                    ) AS has_legacy_embedding
                FROM long_term_memories AS m
                LEFT JOIN long_term_memory_embedding_artifacts AS a
                    ON a.memory_id = m.id
                   AND a.user_id = m.user_id
                   AND a.embedding_signature_fingerprint = ?
                   AND a.embedding_purpose = 'document'
                WHERE {' AND '.join(clauses)}
                ORDER BY m.created_at ASC, m.id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

        candidates = rows[:scan_limit]
        has_more_candidates = len(rows) > scan_limit
        gaps: list[MemoryEmbeddingArtifactGap] = []
        checked = 0
        next_cursor: MemoryEmbeddingArtifactGapCursor | None = None
        for row in candidates:
            checked += 1
            artifact = None
            if row["artifact_signature_fingerprint"] is not None:
                artifact = StoredMemoryEmbeddingArtifact(
                    signature_json=row["artifact_signature_json"],
                    signature_fingerprint=row["artifact_signature_fingerprint"],
                    purpose=row["artifact_purpose"],
                    content_hash=row["artifact_content_hash"],
                    vector_format=row["artifact_vector_format"],
                    vector_dim=row["artifact_vector_dim"],
                    vector_bytes=row["artifact_embedding"],
                    vector_sha256=row["artifact_vector_sha256"],
                )
            reason = classify_memory_embedding_artifact_gap(
                target_content=row["content"],
                expected_signature_json=signature_json,
                artifact=artifact,
                has_any_canonical_artifact=bool(row["has_any_canonical_artifact"]),
                has_legacy_embedding=bool(row["has_legacy_embedding"]),
            )
            if reason is not None:
                gaps.append(
                    MemoryEmbeddingArtifactGap(
                        memory_id=row["memory_id"],
                        user_id=row["user_id"],
                        content=row["content"],
                        reason=reason,
                        created_at=row["created_at"],
                    )
                )
            if len(gaps) == limit:
                if checked < len(candidates) or has_more_candidates:
                    next_cursor = MemoryEmbeddingArtifactGapCursor(
                        created_at=row["created_at"],
                        memory_id=row["memory_id"],
                    )
                break
        else:
            if candidates and has_more_candidates:
                last = candidates[-1]
                next_cursor = MemoryEmbeddingArtifactGapCursor(
                    created_at=last["created_at"],
                    memory_id=last["memory_id"],
                )
        return MemoryEmbeddingArtifactGapPage(
            gaps=gaps,
            next_cursor=next_cursor,
            scanned_memory_count=checked,
        )

    def mark_memory_used(self, *, memory_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE long_term_memories
                SET use_count = use_count + 1, last_used_at = ?
                WHERE id = ?
                """,
                (utc_now(), memory_id),
            )

    def archive_memory(self, *, memory_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE long_term_memories
                SET status = 'archived', is_active = 0, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (utc_now(), memory_id, user_id),
            )
        return cursor.rowcount > 0

    def disable_memory(self, *, memory_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE long_term_memories
                SET is_active = 0, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (utc_now(), memory_id, user_id),
            )
        return cursor.rowcount > 0

    def get_or_create_relationship_state(
        self,
        *,
        user_id: str,
        character_id: str,
    ) -> RelationshipState:
        with self._connect() as conn:
            columns = self._relationship_columns(conn)
            key_column = "character_id" if "character_id" in columns else "character_name"
            row = conn.execute(
                f"""
                SELECT
                    mood, user_emotion, trust, affection, familiarity,
                    current_scene, last_topic, updated_at
                FROM relationship_states
                WHERE user_id = ? AND {key_column} = ?
                LIMIT 1
                """,
                (user_id, character_id),
            ).fetchone()
            if row:
                return RelationshipState(**dict(row))
            state = RelationshipState()
            insert_columns = [
                "id",
                "user_id",
                "mood",
                "user_emotion",
                "trust",
                "affection",
                "familiarity",
                "current_scene",
                "last_topic",
                "updated_at",
            ]
            insert_values = [
                new_id("relation"),
                user_id,
                state.mood,
                state.user_emotion,
                state.trust,
                state.affection,
                state.familiarity,
                state.current_scene,
                state.last_topic,
                state.updated_at,
            ]
            if "character_id" in columns:
                insert_columns.insert(2, "character_id")
                insert_values.insert(2, character_id)
            if "character_name" in columns:
                insert_columns.insert(2, "character_name")
                insert_values.insert(2, character_id)
            placeholders = ", ".join(["?"] * len(insert_columns))
            conn.execute(
                f"""
                INSERT INTO relationship_states ({", ".join(insert_columns)})
                VALUES ({placeholders})
                """,
                insert_values,
            )
        return state

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
        current = self.get_or_create_relationship_state(
            user_id=user_id,
            character_id=character_id,
        )
        updated = RelationshipState(
            mood=mood,
            user_emotion=user_emotion,
            trust=_clamp_stat(current.trust + trust_delta),
            affection=_clamp_stat(current.affection + affection_delta),
            familiarity=_clamp_stat(current.familiarity + familiarity_delta),
            current_scene=current.current_scene,
            last_topic=last_topic,
            updated_at=utc_now(),
        )
        with self._connect() as conn:
            columns = self._relationship_columns(conn)
            key_column = "character_id" if "character_id" in columns else "character_name"
            conn.execute(
                f"""
                UPDATE relationship_states
                SET mood = ?, user_emotion = ?, trust = ?, affection = ?, familiarity = ?,
                    current_scene = ?, last_topic = ?, updated_at = ?
                WHERE user_id = ? AND {key_column} = ?
                """,
                (
                    updated.mood,
                    updated.user_emotion,
                    updated.trust,
                    updated.affection,
                    updated.familiarity,
                    updated.current_scene,
                    updated.last_topic,
                    updated.updated_at,
                    user_id,
                    character_id,
                ),
            )
        return updated

    def resolve_or_reserve(
        self,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            session_row = self._stream_require_owned_session(
                conn,
                session_id=request.session_id,
                user_id=request.user_id,
                active_character_id=request.active_character_id,
            )
            if session_row["status"] == "archived":
                raise SessionArchivedError("stream_turn_session_archived")
            record_row = self._stream_find_by_scope(
                conn,
                user_id=request.user_id,
                session_id=request.session_id,
                client_message_id=request.client_message_id,
            )
            if session_row["status"] == "ended":
                if (
                    record_row is not None
                    and self._stream_matches_fingerprint(record_row, request)
                    and record_row["status"] in {"completed", "failed"}
                ):
                    outcome = self._stream_replay_outcome(
                        conn,
                        self._stream_record_from_row(record_row),
                    )
                else:
                    raise SessionEndedError("stream_turn_session_ended")
            elif record_row is None:
                outcome = self._stream_reserve(conn, request=request)
            elif not self._stream_matches_fingerprint(record_row, request):
                outcome = StreamTurnResolveOutcome(
                    kind="conflict",
                    record=self._stream_record_from_row(record_row),
                )
            elif record_row["status"] == "in_progress":
                if record_row["owner_runtime_id"] != request.owner_runtime_id:
                    self._stream_mark_failed(
                        conn,
                        record_id=record_row["id"],
                        error_code="CHAT_STREAM_INTERRUPTED",
                        retryable=False,
                        reason="runtime_orphaned",
                    )
                    record_row = self._stream_require_record(
                        conn,
                        record_id=record_row["id"],
                        turn_id=record_row["turn_id"],
                        user_id=record_row["user_id"],
                        session_id=record_row["session_id"],
                    )
                    outcome = StreamTurnResolveOutcome(
                        kind="replay_failed",
                        record=self._stream_record_from_row(record_row),
                    )
                else:
                    outcome = StreamTurnResolveOutcome(
                        kind="in_progress",
                        record=self._stream_record_from_row(record_row),
                    )
            else:
                outcome = self._stream_replay_outcome(
                    conn,
                    self._stream_record_from_row(record_row),
                )
            conn.commit()
            return outcome
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def complete(self, *, request: StreamTurnComplete) -> StreamRequestRecord:
        if not request.assistant_response.strip():
            raise ValueError("stream_turn_assistant_response_blank")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record_row = self._stream_require_record(
                conn,
                record_id=request.record_id,
                turn_id=request.turn_id,
                user_id=request.user_id,
                session_id=request.session_id,
            )
            if (
                record_row["status"] != "in_progress"
                or record_row["owner_runtime_id"] != request.owner_runtime_id
            ):
                result = self._stream_record_from_row(record_row)
            else:
                session_row = self._stream_require_owned_session(
                    conn,
                    session_id=request.session_id,
                    user_id=request.user_id,
                    active_character_id=record_row["active_character_id"],
                )
                if session_row["status"] != "active":
                    self._stream_mark_failed(
                        conn,
                        record_id=record_row["id"],
                        error_code=(
                            "SESSION_ARCHIVED"
                            if session_row["status"] == "archived"
                            else "SESSION_ENDED"
                        ),
                        retryable=False,
                        reason=(
                            "session_archived"
                            if session_row["status"] == "archived"
                            else "session_ended"
                        ),
                    )
                else:
                    self._stream_complete_active_record(
                        conn,
                        record_row=record_row,
                        request=request,
                    )
                result = self._stream_record_from_row(
                    self._stream_require_record(
                        conn,
                        record_id=record_row["id"],
                        turn_id=record_row["turn_id"],
                        user_id=record_row["user_id"],
                        session_id=record_row["session_id"],
                    )
                )
            conn.commit()
            return result
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def fail(self, *, request: StreamTurnFail) -> StreamRequestRecord:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record_row = self._stream_require_record(
                conn,
                record_id=request.record_id,
                turn_id=request.turn_id,
                user_id=request.user_id,
                session_id=request.session_id,
            )
            if (
                record_row["status"] == "in_progress"
                and record_row["owner_runtime_id"] == request.owner_runtime_id
            ):
                self._stream_mark_failed(
                    conn,
                    record_id=record_row["id"],
                    error_code=request.terminal_error_code,
                    retryable=request.terminal_retryable,
                    reason=request.terminal_reason,
                )
                record_row = self._stream_require_record(
                    conn,
                    record_id=record_row["id"],
                    turn_id=record_row["turn_id"],
                    user_id=record_row["user_id"],
                    session_id=record_row["session_id"],
                )
            result = self._stream_record_from_row(record_row)
            conn.commit()
            return result
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def fail_orphan_if_owned_by_other_runtime(
        self,
        *,
        owner_runtime_id: str,
        limit: int = 100,
    ) -> int:
        if not isinstance(owner_runtime_id, str) or not owner_runtime_id:
            raise ValueError("stream_turn_owner_runtime_id_invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("stream_turn_orphan_limit_invalid")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id
                FROM chat_request_idempotency
                WHERE status = 'in_progress' AND owner_runtime_id != ?
                ORDER BY updated_at, id
                LIMIT ?
                """,
                (owner_runtime_id, limit),
            ).fetchall()
            for row in rows:
                self._stream_mark_failed(
                    conn,
                    record_id=row["id"],
                    error_code="CHAT_STREAM_INTERRUPTED",
                    retryable=False,
                    reason="runtime_orphaned",
                )
            conn.commit()
            return len(rows)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _stream_reserve(
        self,
        conn: sqlite3.Connection,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, user_id, role, content, created_at, emotion_label, importance_score
            ) VALUES (?, ?, ?, 'user', ?, ?, NULL, NULL)
            """,
            (
                request.user_message_id,
                request.session_id,
                request.user_id,
                request.user_message_content,
                now,
            ),
        )
        record_id = new_id("stream_request")
        conn.execute(
            """
            INSERT INTO chat_request_idempotency (
                id, user_id, session_id, client_message_id, active_character_id,
                request_fingerprint_version, request_fingerprint, turn_id, user_message_id,
                assistant_message_id, status, owner_runtime_id, warnings_json, state_delta_json,
                terminal_error_code, terminal_retryable, terminal_reason, created_at, updated_at,
                completed_at, failed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'in_progress', ?, '[]', '{}',
                      NULL, NULL, NULL, ?, ?, NULL, NULL)
            """,
            (
                record_id,
                request.user_id,
                request.session_id,
                request.client_message_id,
                request.active_character_id,
                request.request_fingerprint_version,
                request.request_fingerprint,
                request.turn_id,
                request.user_message_id,
                request.owner_runtime_id,
                now,
                now,
            ),
        )
        return StreamTurnResolveOutcome(
            kind="reserved",
            record=self._stream_record_from_row(
                self._stream_require_record(
                    conn,
                    record_id=record_id,
                    turn_id=request.turn_id,
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
            ),
        )

    def _stream_complete_active_record(
        self,
        conn: sqlite3.Connection,
        *,
        record_row: sqlite3.Row,
        request: StreamTurnComplete,
    ) -> None:
        now = utc_now()
        assistant_message_id = new_id("message")
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, user_id, role, content, created_at, emotion_label, importance_score
            ) VALUES (?, ?, ?, 'assistant', ?, ?, NULL, NULL)
            """,
            (
                assistant_message_id,
                record_row["session_id"],
                record_row["user_id"],
                request.assistant_response,
                now,
            ),
        )

        columns = self._relationship_columns(conn)
        key_column = "character_id" if "character_id" in columns else "character_name"
        relationship_row = conn.execute(
            f"""
            SELECT mood, user_emotion, trust, affection, familiarity,
                   current_scene, last_topic, updated_at
            FROM relationship_states
            WHERE user_id = ? AND {key_column} = ?
            LIMIT 1
            """,
            (record_row["user_id"], record_row["active_character_id"]),
        ).fetchone()
        current = self._stream_relationship_from_row(relationship_row)
        updated = apply_stream_turn_state_update(
            current=current,
            update=request.state_update,
            updated_at=now,
        )
        if relationship_row is None:
            insert_columns = [
                "id",
                "user_id",
                "mood",
                "user_emotion",
                "trust",
                "affection",
                "familiarity",
                "current_scene",
                "last_topic",
                "updated_at",
            ]
            insert_values: list[object] = [
                new_id("relation"),
                record_row["user_id"],
                updated.mood,
                updated.user_emotion,
                updated.trust,
                updated.affection,
                updated.familiarity,
                updated.current_scene,
                updated.last_topic,
                updated.updated_at,
            ]
            if "character_id" in columns:
                insert_columns.insert(2, "character_id")
                insert_values.insert(2, record_row["active_character_id"])
            if "character_name" in columns:
                insert_columns.insert(2, "character_name")
                insert_values.insert(2, record_row["active_character_id"])
            placeholders = ", ".join("?" for _ in insert_columns)
            conn.execute(
                f"INSERT INTO relationship_states ({', '.join(insert_columns)}) "
                f"VALUES ({placeholders})",
                insert_values,
            )
        else:
            conn.execute(
                f"""
                UPDATE relationship_states
                SET mood = ?, user_emotion = ?, trust = ?, affection = ?, familiarity = ?,
                    current_scene = ?, last_topic = ?, updated_at = ?
                WHERE user_id = ? AND {key_column} = ?
                """,
                (
                    updated.mood,
                    updated.user_emotion,
                    updated.trust,
                    updated.affection,
                    updated.familiarity,
                    updated.current_scene,
                    updated.last_topic,
                    updated.updated_at,
                    record_row["user_id"],
                    record_row["active_character_id"],
                ),
            )
        conn.execute(
            """
            UPDATE chat_request_idempotency
            SET assistant_message_id = ?, status = 'completed', warnings_json = ?,
                state_delta_json = ?, terminal_error_code = NULL, terminal_retryable = NULL,
                terminal_reason = NULL, updated_at = ?, completed_at = ?, failed_at = NULL
            WHERE id = ? AND status = 'in_progress'
            """,
            (
                assistant_message_id,
                stream_turn_warnings_json(request.warnings),
                stream_turn_state_delta_json(request.state_update),
                now,
                now,
                record_row["id"],
            ),
        )

    def _stream_mark_failed(
        self,
        conn: sqlite3.Connection,
        *,
        record_id: str,
        error_code: str,
        retryable: bool,
        reason: str,
    ) -> None:
        now = utc_now()
        conn.execute(
            """
            UPDATE chat_request_idempotency
            SET assistant_message_id = NULL, status = 'failed', warnings_json = '[]',
                state_delta_json = '{}', terminal_error_code = ?, terminal_retryable = ?,
                terminal_reason = ?, updated_at = ?, completed_at = NULL, failed_at = ?
            WHERE id = ? AND status = 'in_progress'
            """,
            (error_code, int(retryable), reason, now, now, record_id),
        )

    def _stream_find_by_scope(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        session_id: str,
        client_message_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT *
            FROM chat_request_idempotency
            WHERE user_id = ? AND session_id = ? AND client_message_id = ?
            LIMIT 1
            """,
            (user_id, session_id, client_message_id),
        ).fetchone()

    def _stream_require_record(
        self,
        conn: sqlite3.Connection,
        *,
        record_id: str,
        turn_id: str,
        user_id: str,
        session_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT *
            FROM chat_request_idempotency
            WHERE id = ? AND turn_id = ? AND user_id = ? AND session_id = ?
            LIMIT 1
            """,
            (record_id, turn_id, user_id, session_id),
        ).fetchone()
        if row is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_not_found")
        return row

    def _stream_require_owned_session(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        user_id: str,
        active_character_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, user_id, character_id, status FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("stream_turn_session_not_found")
        if row["user_id"] != user_id or row["character_id"] != active_character_id:
            raise SessionOwnershipError("stream_turn_session_ownership_invalid")
        return row

    def _stream_replay_outcome(
        self,
        conn: sqlite3.Connection,
        record: StreamRequestRecord,
    ) -> StreamTurnResolveOutcome:
        if record.status == "failed":
            return StreamTurnResolveOutcome(kind="replay_failed", record=record)
        if record.status != "completed" or record.assistant_message_id is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_replay_record_invalid")
        row = conn.execute(
            """
            SELECT id, session_id, user_id, role, content, created_at,
                   emotion_label, importance_score
            FROM messages
            WHERE id = ? AND session_id = ? AND user_id = ? AND role = 'assistant'
            LIMIT 1
            """,
            (record.assistant_message_id, record.session_id, record.user_id),
        ).fetchone()
        if row is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_replay_assistant_invalid")
        return StreamTurnResolveOutcome(
            kind="replay_completed",
            record=record,
            assistant_message=MessageRecord(**dict(row)),
        )

    def _stream_matches_fingerprint(
        self,
        row: sqlite3.Row,
        request: StreamTurnResolveOrReserve,
    ) -> bool:
        return (
            row["request_fingerprint_version"] == request.request_fingerprint_version
            and row["request_fingerprint"] == request.request_fingerprint
        )

    def _stream_record_from_row(self, row: sqlite3.Row) -> StreamRequestRecord:
        status = row["status"]
        if status not in {"in_progress", "completed", "failed"}:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_status_invalid")
        try:
            warnings = parse_stream_turn_warnings(row["warnings_json"])
            state_delta = parse_stream_turn_state_delta(
                row["state_delta_json"],
                status=status,
            )
        except (TypeError, ValueError) as exc:
            raise StorageMigrationRequiredError(
                reason="streaming_chat_record_payload_invalid"
            ) from exc
        if row["terminal_retryable"] not in {None, 0, 1}:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_terminal_invalid")
        return StreamRequestRecord(
            id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            client_message_id=row["client_message_id"],
            active_character_id=row["active_character_id"],
            request_fingerprint_version=row["request_fingerprint_version"],
            request_fingerprint=row["request_fingerprint"],
            turn_id=row["turn_id"],
            user_message_id=row["user_message_id"],
            assistant_message_id=row["assistant_message_id"],
            status=status,
            owner_runtime_id=row["owner_runtime_id"],
            warnings=warnings,
            state_delta=state_delta,
            terminal_error_code=row["terminal_error_code"],
            terminal_retryable=(
                bool(row["terminal_retryable"])
                if row["terminal_retryable"] is not None
                else None
            ),
            terminal_reason=row["terminal_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            failed_at=row["failed_at"],
        )

    def _stream_relationship_from_row(self, row: sqlite3.Row | None) -> RelationshipState | None:
        if row is None:
            return None
        return RelationshipState(
            mood=row["mood"] or "calm",
            user_emotion=row["user_emotion"] or "neutral",
            trust=row["trust"] if row["trust"] is not None else 50,
            affection=row["affection"] if row["affection"] is not None else 50,
            familiarity=row["familiarity"] if row["familiarity"] is not None else 50,
            current_scene=row["current_scene"] or "default_chat",
            last_topic=row["last_topic"],
            updated_at=row["updated_at"],
        )
