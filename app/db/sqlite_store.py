from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.schemas import MemoryRecord, MessageRecord, RelationshipState
from app.utils.ids import new_id, utc_now


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

    def create_memory(
        self,
        *,
        user_id: str,
        namespace: str,
        memory_type: str,
        content: str,
        importance: int,
        confidence: float,
        source: str,
        source_message_id: str | None,
        related_memory_ids: list[str] | None = None,
        expiry: str | None = None,
    ) -> str:
        memory_id = new_id("memory")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO long_term_memories
                (
                    id, user_id, namespace, memory_type, content, importance, confidence, source,
                    source_message_id, related_memory_ids, version, status, last_used_at,
                    use_count, decay_score, created_at, updated_at, expiry, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', NULL, 0, NULL, ?, NULL, ?, 1)
                """,
                (
                    memory_id,
                    user_id,
                    namespace,
                    memory_type,
                    content,
                    importance,
                    confidence,
                    source,
                    source_message_id,
                    json.dumps(related_memory_ids or [], ensure_ascii=True),
                    utc_now(),
                    expiry,
                ),
            )
        return memory_id

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
        return [MemoryRecord(**dict(row)) for row in rows]

    def search_memories_keyword(
        self,
        *,
        user_id: str,
        keywords: list[str],
        limit: int = 8,
    ) -> list[MemoryRecord]:
        tokens = [token.strip() for token in keywords if token.strip()]
        if not tokens:
            return []
        conditions = " OR ".join(["content LIKE ?"] * len(tokens))
        params = [user_id, *[f"%{token}%" for token in tokens], limit]
        query = f"""
            SELECT *
            FROM long_term_memories
            WHERE user_id = ?
              AND status = 'active'
              AND is_active = 1
              AND ({conditions})
            ORDER BY importance DESC, use_count DESC, created_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [MemoryRecord(**dict(row)) for row in rows]

    def has_exact_active_memory(self, *, user_id: str, content: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM long_term_memories
                WHERE user_id = ? AND content = ? AND status = 'active' AND is_active = 1
                LIMIT 1
                """,
                (user_id, content),
            ).fetchone()
        return row is not None

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

    def archive_memory(self, *, memory_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE long_term_memories
                SET status = 'archived', is_active = 0, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), memory_id),
            )

    def disable_memory(self, *, memory_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE long_term_memories
                SET is_active = 0, updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), memory_id),
            )

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
