from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.retrieval.documents import ChunkRecord


@dataclass(frozen=True)
class VectorSearchResult:
    chunk: ChunkRecord
    raw_score: float


class SqliteVectorStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    vector_dim INTEGER NOT NULL,
                    embedding BLOB NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_namespace ON chunks(namespace)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_source_type ON chunks(source_type)"
            )

    def reset(self) -> None:
        with self._connect() as conn:
            conn.execute("DROP TABLE IF EXISTS chunks")
        self._init_schema()

    def existing_ids(self, *, ids: list[str] | None = None) -> set[str]:
        with self._connect() as conn:
            if ids is None:
                rows = conn.execute("SELECT id FROM chunks").fetchall()
                return {row[0] for row in rows}
            if not ids:
                return set()
            existing: set[str] = set()
            batch_size = 900
            for start in range(0, len(ids), batch_size):
                batch = ids[start:start + batch_size]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id FROM chunks WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
                existing.update(row[0] for row in rows)
            return existing

    def upsert_chunks(
        self,
        *,
        chunks: list[ChunkRecord],
        embeddings: list[np.ndarray],
        embedding_model: str,
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        rows = []
        for chunk, embedding in zip(chunks, embeddings):
            vector = np.asarray(embedding, dtype=np.float32)
            metadata = dict(chunk.metadata)
            metadata["embedding_model"] = embedding_model
            rows.append(
                (
                    chunk.id,
                    chunk.namespace,
                    chunk.source_type,
                    chunk.content,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    embedding_model,
                    int(vector.shape[0]),
                    vector.tobytes(),
                )
            )
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO chunks (
                    id,
                    namespace,
                    source_type,
                    content,
                    metadata_json,
                    embedding_model,
                    vector_dim,
                    embedding
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def search(
        self,
        *,
        query_embedding: np.ndarray,
        namespaces: list[str] | None,
        limit: int,
        candidate_limit: int | None = None,
    ) -> list[VectorSearchResult]:
        candidate_limit = candidate_limit or max(limit * 4, limit + 10)
        rows = self._load_rows(namespaces=namespaces)
        query = np.asarray(query_embedding, dtype=np.float32)
        results: list[VectorSearchResult] = []
        for row in rows:
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
            if vector.shape != query.shape:
                continue
            raw_score = float(np.dot(query, vector))
            metadata = json.loads(row["metadata_json"])
            chunk = ChunkRecord(
                id=row["id"],
                namespace=row["namespace"],
                source_type=row["source_type"],
                content=row["content"],
                metadata=metadata,
            )
            results.append(VectorSearchResult(chunk=chunk, raw_score=raw_score))
        results.sort(key=lambda item: item.raw_score, reverse=True)
        return results[:candidate_limit]

    def _load_rows(self, *, namespaces: list[str] | None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if namespaces:
                placeholders = ", ".join("?" for _ in namespaces)
                cursor = conn.execute(
                    f"SELECT * FROM chunks WHERE namespace IN ({placeholders})",
                    namespaces,
                )
            else:
                cursor = conn.execute("SELECT * FROM chunks")
            return [dict(row) for row in cursor.fetchall()]

    def count_by_namespace(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT namespace, COUNT(*) FROM chunks GROUP BY namespace"
            ).fetchall()
        return {namespace: count for namespace, count in rows}
