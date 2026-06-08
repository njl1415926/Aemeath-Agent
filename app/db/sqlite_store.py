from __future__ import annotations

import json
import sqlite3
import warnings
from pathlib import Path
from typing import Any

import numpy as np

from app.memory.schemas import (
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
from app.session.schemas import SessionRecord, SessionStatus
from app.utils.ids import new_id, utc_now

MEMORY_SCOPE_MIGRATION_VERSION = "memory_scope_provenance_v1"


def _clamp_stat(value: int) -> int:
    return max(0, min(100, value))


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        schema_sql = schema_path.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.executescript(schema_sql)
            self._migrate_relationship_states(conn)
            self._migrate_session_columns(conn)
            self._backfill_sessions(conn)
            conn.commit()
            self._migrate_memory_scope_provenance(conn)

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
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                self._session_select_sql()
                + f"""
                WHERE {" AND ".join(conditions)}
                GROUP BY s.id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def end_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        with self._connect() as conn:
            session = self._require_owned_session(
                conn,
                session_id=session_id,
                user_id=user_id,
            )
            if session.status == "archived":
                raise SessionArchivedError(f"session {session_id!r} is archived")
            if session.status == "active":
                now = utc_now()
                conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'ended', ended_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, session_id),
                )
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
            session = self._require_owned_session(
                conn,
                session_id=session_id,
                user_id=user_id,
                character_id=character_id,
            )
            if session.status == "archived":
                raise SessionArchivedError(f"session {session_id!r} is archived")
            if session.status != "ended":
                raise SessionTransitionError(
                    f"session {session_id!r} is not ended and cannot be resumed"
                )
            now = utc_now()
            conn.execute(
                """
                UPDATE sessions
                SET status = 'active', ended_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            updated = self._get_session_with_conn(conn, session_id=session_id)
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

    def archive_session(self, *, session_id: str, user_id: str) -> SessionRecord:
        with self._connect() as conn:
            session = self._require_owned_session(
                conn,
                session_id=session_id,
                user_id=user_id,
            )
            if session.status != "archived":
                now = utc_now()
                conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'archived', archived_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, session_id),
                )
            updated = self._get_session_with_conn(conn, session_id=session_id)
        if updated is None:
            raise SessionNotFoundError(f"session {session_id!r} was not found")
        return updated

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
                  AND scope_character_id IS ?
                  AND scope_session_id IS ?
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
