from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from app.embeddings.artifacts import (
    EmbeddingArtifactDiagnostic,
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
from app.retrieval.documents import ChunkRecord
from app.retrieval.errors import (
    RagGenerationStoreBackupError,
    RagGenerationStoreError,
    RagGenerationStoreOpenError,
    RagGenerationStoreReadOnlyError,
)

RAG_EMBEDDING_ARTIFACT_MIGRATION_VERSION = "rag_embedding_artifact_v1"
_SQLITE_IN_BATCH_SIZE = 900


@dataclass(frozen=True)
class VectorSearchResult:
    chunk: ChunkRecord
    raw_score: float


@dataclass(frozen=True)
class ArtifactVectorSearchOutcome:
    hits: list[VectorSearchResult]
    diagnostics: EmbeddingArtifactDiagnostics


@dataclass(frozen=True)
class RagChunkExpectation:
    """Manifest-independent, content-safe inventory fact for coverage checks."""

    chunk_id: str
    namespace: str
    source_type: str
    content_hash: str

    @classmethod
    def from_chunk(cls, chunk: ChunkRecord) -> RagChunkExpectation:
        return cls(
            chunk_id=chunk.id,
            namespace=chunk.namespace,
            source_type=chunk.source_type,
            content_hash=canonical_content_hash(chunk.content),
        )


@dataclass(frozen=True)
class ChunkDeletionResult:
    requested_chunk_count: int
    deleted_chunk_count: int
    deleted_exact_artifact_count: int
    deleted_legacy_artifact_count: int


@dataclass(frozen=True)
class ExactSignatureCoverage:
    database_chunk_count: int
    expected_chunk_count: int
    exact_valid_artifact_count: int
    missing_chunk_count: int
    unexpected_chunk_count: int
    chunk_contract_mismatch_count: int
    missing_artifact_count: int
    content_stale_count: int
    vector_corrupt_count: int
    signature_corrupt_count: int
    incompatible_signature_artifact_count: int
    legacy_artifact_count: int
    ready_eligible: bool
    diagnostics: EmbeddingArtifactDiagnostics


class SqliteVectorStore:
    """RAG SQLite storage with canonical artifacts and a one-release legacy shim."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._read_only = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @classmethod
    def open_read_only(cls, db_path: Path) -> SqliteVectorStore:
        """Open an existing current-schema database without any initialization work."""

        if not db_path.is_file():
            raise RagGenerationStoreOpenError("rag_generation_store_database_missing")
        store = cls.__new__(cls)
        store.db_path = db_path
        store._read_only = True
        store._validate_read_only_schema()
        return store

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            database_uri = f"{self.db_path.absolute().as_uri()}?mode=ro"
            connection = sqlite3.connect(database_uri, uri=True)
        else:
            connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise RagGenerationStoreReadOnlyError()

    def _validate_read_only_schema(self) -> None:
        required_columns = {
            "rag_schema_migrations": {"version", "applied_at"},
            "chunks": {"id", "namespace", "source_type", "content", "metadata_json"},
            "chunk_embedding_artifacts": {
                "chunk_id",
                "embedding_signature_json",
                "embedding_signature_fingerprint",
                "embedding_purpose",
                "embedding_content_hash",
                "embedding_vector_format",
                "vector_dim",
                "embedding",
                "embedding_vector_sha256",
                "created_at",
                "updated_at",
            },
            "legacy_chunk_embedding_artifacts": {
                "chunk_id",
                "embedding_model",
                "vector_dim",
                "embedding",
                "created_at",
            },
        }
        try:
            conn = self._connect()
            try:
                for table_name, required in required_columns.items():
                    columns = {
                        row["name"]
                        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    }
                    if not required.issubset(columns):
                        raise RagGenerationStoreOpenError(
                            "rag_generation_store_schema_incompatible"
                        )
                artifact_columns = conn.execute(
                    "PRAGMA table_info(chunk_embedding_artifacts)"
                ).fetchall()
                artifact_primary_key = [
                    row["name"]
                    for row in sorted(artifact_columns, key=lambda row: row["pk"])
                    if row["pk"] > 0
                ]
                if artifact_primary_key != [
                    "chunk_id",
                    "embedding_signature_fingerprint",
                    "embedding_purpose",
                ]:
                    raise RagGenerationStoreOpenError(
                        "rag_generation_store_schema_incompatible"
                    )
            finally:
                conn.close()
        except RagGenerationStoreOpenError:
            raise
        except sqlite3.Error as exc:
            raise RagGenerationStoreOpenError(
                "rag_generation_store_open_failed"
            ) from exc

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            if self._has_legacy_chunk_layout(conn):
                self._migrate_legacy_chunk_layout(conn)
            self._create_current_tables(conn)
            self._migrate_verified_legacy_hash_artifacts(conn)

    def _has_legacy_chunk_layout(self, conn: sqlite3.Connection) -> bool:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone()
        if exists is None:
            return False
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        return {"embedding_model", "vector_dim", "embedding"}.issubset(columns)

    def _create_current_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                source_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunk_embedding_artifacts (
                chunk_id TEXT NOT NULL,
                embedding_signature_json TEXT NOT NULL,
                embedding_signature_fingerprint TEXT NOT NULL,
                embedding_purpose TEXT NOT NULL CHECK (embedding_purpose = 'document'),
                embedding_content_hash TEXT NOT NULL,
                embedding_vector_format TEXT NOT NULL CHECK (embedding_vector_format = 'f32le-v1'),
                vector_dim INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                embedding_vector_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (chunk_id, embedding_signature_fingerprint, embedding_purpose)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS legacy_chunk_embedding_artifacts (
                chunk_id TEXT PRIMARY KEY,
                embedding_model TEXT NOT NULL,
                vector_dim INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_chunks_namespace ON chunks(namespace)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_chunks_source_type ON chunks(source_type)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chunk_embedding_artifacts_signature
            ON chunk_embedding_artifacts (
                embedding_signature_fingerprint, embedding_purpose
            )
            """
        )

    def _migrate_legacy_chunk_layout(self, conn: sqlite3.Connection) -> None:
        applied = conn.execute(
            "SELECT 1 FROM rag_schema_migrations WHERE version = ?",
            (RAG_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE chunks RENAME TO chunks_legacy_embedding_v0")
            self._create_current_tables(conn)
            conn.execute(
                """
                INSERT INTO chunks (id, namespace, source_type, content, metadata_json)
                SELECT id, namespace, source_type, content, metadata_json
                FROM chunks_legacy_embedding_v0
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO legacy_chunk_embedding_artifacts (
                    chunk_id, embedding_model, vector_dim, embedding, created_at
                )
                SELECT id, embedding_model, vector_dim, embedding, datetime('now')
                FROM chunks_legacy_embedding_v0
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate_verified_legacy_hash_artifacts(self, conn: sqlite3.Connection) -> None:
        applied = conn.execute(
            "SELECT 1 FROM rag_schema_migrations WHERE version = ?",
            (RAG_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
        ).fetchone()
        if applied is not None:
            conn.execute("DROP TABLE IF EXISTS chunks_legacy_embedding_v0")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            profile = load_embedding_space_profile("deterministic_hash_v1")
            provider = DeterministicHashEmbeddingProvider(profile=profile)
            rows = conn.execute(
                """
                SELECT legacy.chunk_id, legacy.embedding_model, legacy.vector_dim,
                       legacy.embedding, chunks.content
                FROM legacy_chunk_embedding_artifacts AS legacy
                INNER JOIN chunks ON chunks.id = legacy.chunk_id
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
                    # A malformed legacy row is retained as legacy evidence. It
                    # must not prevent valid rows or the store itself from opening.
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO chunk_embedding_artifacts (
                        chunk_id, embedding_signature_json, embedding_signature_fingerprint,
                        embedding_purpose, embedding_content_hash, embedding_vector_format,
                        vector_dim, embedding, embedding_vector_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL)
                    """,
                    (
                        row["chunk_id"],
                        artifact.signature_json,
                        artifact.signature_fingerprint,
                        artifact.purpose,
                        artifact.content_hash,
                        artifact.vector_format,
                        artifact.vector_dim,
                        artifact.vector_bytes,
                        artifact.vector_sha256,
                    ),
                )
            conn.execute(
                """
                INSERT INTO rag_schema_migrations (version, applied_at)
                VALUES (?, datetime('now'))
                """,
                (RAG_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
            )
            conn.execute("DROP TABLE IF EXISTS chunks_legacy_embedding_v0")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def reset(self) -> None:
        self._ensure_writable()
        with self._connect() as conn:
            conn.execute("DROP TABLE IF EXISTS chunk_embedding_artifacts")
            conn.execute("DROP TABLE IF EXISTS legacy_chunk_embedding_artifacts")
            conn.execute("DROP TABLE IF EXISTS chunks")
            conn.execute("DROP TABLE IF EXISTS chunks_legacy_embedding_v0")
            conn.execute("DROP TABLE IF EXISTS rag_schema_migrations")
        self._init_schema()

    def backup_to(self, target_path: Path) -> None:
        """Create a no-overwrite, SQLite-consistent clone at ``target_path``."""

        if target_path.exists():
            raise RagGenerationStoreBackupError("rag_generation_store_backup_target_exists")
        if not target_path.parent.is_dir():
            raise RagGenerationStoreBackupError("rag_generation_store_backup_parent_missing")

        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target_path.stem}.",
                suffix=".backup.tmp",
                dir=target_path.parent,
            )
            os.close(file_descriptor)
        except OSError as exc:
            raise RagGenerationStoreBackupError(
                "rag_generation_store_backup_temp_create_failed"
            ) from exc
        temporary_path = Path(temporary_name)
        try:
            source: sqlite3.Connection | None = None
            target: sqlite3.Connection | None = None
            try:
                source = self._connect()
                target = sqlite3.connect(temporary_path)
                source.backup(target)
            finally:
                if target is not None:
                    target.close()
                if source is not None:
                    source.close()
            try:
                os.link(temporary_path, target_path)
            except FileExistsError as exc:
                raise RagGenerationStoreBackupError(
                    "rag_generation_store_backup_target_exists"
                ) from exc
            temporary_path.unlink()
        except RagGenerationStoreBackupError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise RagGenerationStoreBackupError("rag_generation_store_backup_failed") from exc
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def existing_ids(self, *, ids: list[str] | None = None) -> set[str]:
        with self._connect() as conn:
            if ids is None:
                rows = conn.execute("SELECT id FROM chunks").fetchall()
                return {row[0] for row in rows}
            if not ids:
                return set()
            existing: set[str] = set()
            for start in range(0, len(ids), _SQLITE_IN_BATCH_SIZE):
                batch = ids[start : start + _SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id FROM chunks WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
                existing.update(row[0] for row in rows)
            return existing

    def delete_chunks(self, *, chunk_ids: list[str]) -> ChunkDeletionResult:
        """Delete selected chunks and every persisted vector fact in one transaction."""

        self._ensure_writable()
        if any(not isinstance(chunk_id, str) for chunk_id in chunk_ids):
            raise ValueError("chunk_id_invalid")
        unique_chunk_ids = list(dict.fromkeys(chunk_ids))
        if not unique_chunk_ids:
            return ChunkDeletionResult(
                requested_chunk_count=0,
                deleted_chunk_count=0,
                deleted_exact_artifact_count=0,
                deleted_legacy_artifact_count=0,
            )

        deleted_artifacts = 0
        deleted_legacy_artifacts = 0
        deleted_chunks = 0
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            for start in range(0, len(unique_chunk_ids), _SQLITE_IN_BATCH_SIZE):
                batch = unique_chunk_ids[start : start + _SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                deleted_artifacts += conn.execute(
                    f"DELETE FROM chunk_embedding_artifacts WHERE chunk_id IN ({placeholders})",
                    batch,
                ).rowcount
                deleted_legacy_artifacts += conn.execute(
                    "DELETE FROM legacy_chunk_embedding_artifacts "
                    f"WHERE chunk_id IN ({placeholders})",
                    batch,
                ).rowcount
                deleted_chunks += conn.execute(
                    f"DELETE FROM chunks WHERE id IN ({placeholders})",
                    batch,
                ).rowcount
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise RagGenerationStoreError("rag_generation_store_chunk_delete_failed") from exc
        finally:
            if conn is not None:
                conn.close()
        return ChunkDeletionResult(
            requested_chunk_count=len(unique_chunk_ids),
            deleted_chunk_count=deleted_chunks,
            deleted_exact_artifact_count=deleted_artifacts,
            deleted_legacy_artifact_count=deleted_legacy_artifacts,
        )

    def upsert_chunks(
        self,
        *,
        chunks: list[ChunkRecord],
        embeddings: list[np.ndarray],
        embedding_model: str,
    ) -> None:
        """Legacy compatibility writer. New callers use artifact writes below."""
        self._ensure_writable()
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        with self._connect() as conn:
            for chunk, embedding in zip(chunks, embeddings):
                vector = np.asarray(embedding, dtype=np.float32)
                metadata = dict(chunk.metadata)
                metadata["embedding_model"] = embedding_model
                conn.execute(
                    """
                    INSERT INTO chunks (id, namespace, source_type, content, metadata_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        namespace = excluded.namespace,
                        source_type = excluded.source_type,
                        content = excluded.content,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        chunk.id,
                        chunk.namespace,
                        chunk.source_type,
                        chunk.content,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO legacy_chunk_embedding_artifacts (
                        chunk_id, embedding_model, vector_dim, embedding, created_at
                    ) VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        embedding_model = excluded.embedding_model,
                        vector_dim = excluded.vector_dim,
                        embedding = excluded.embedding,
                        created_at = excluded.created_at
                    """,
                    (chunk.id, embedding_model, int(vector.shape[0]), vector.tobytes()),
                )

    def upsert_chunk_embedding_artifacts(
        self,
        *,
        chunks: list[ChunkRecord],
        artifacts: list[EmbeddingArtifactWrite],
    ) -> list[EmbeddingArtifactUpsertOutcome]:
        self._ensure_writable()
        if len(chunks) != len(artifacts):
            raise ValueError("chunks and embedding artifacts length mismatch")
        prepared = [prepare_embedding_artifact(artifact) for artifact in artifacts]
        for chunk, artifact in zip(chunks, prepared):
            if canonical_content_hash(chunk.content) != artifact.content_hash:
                raise EmbeddingArtifactValidationError("content_hash_target_mismatch")
        outcomes: list[EmbeddingArtifactUpsertOutcome] = []
        with self._connect() as conn:
            for chunk, artifact in zip(chunks, prepared):
                conn.execute(
                    """
                    INSERT INTO chunks (id, namespace, source_type, content, metadata_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        namespace = excluded.namespace,
                        source_type = excluded.source_type,
                        content = excluded.content,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        chunk.id,
                        chunk.namespace,
                        chunk.source_type,
                        chunk.content,
                        json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                current_chunk = conn.execute(
                    "SELECT content FROM chunks WHERE id = ?",
                    (chunk.id,),
                ).fetchone()
                if (
                    current_chunk is None
                    or canonical_content_hash(current_chunk["content"])
                    != artifact.content_hash
                ):
                    raise EmbeddingArtifactValidationError("content_hash_target_mismatch")
                existing = conn.execute(
                    """
                    SELECT embedding_signature_json, embedding_content_hash,
                           embedding_vector_sha256
                    FROM chunk_embedding_artifacts
                    WHERE chunk_id = ?
                      AND embedding_signature_fingerprint = ?
                      AND embedding_purpose = ?
                    """,
                    (chunk.id, artifact.signature_fingerprint, artifact.purpose),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO chunk_embedding_artifacts (
                            chunk_id, embedding_signature_json, embedding_signature_fingerprint,
                            embedding_purpose, embedding_content_hash, embedding_vector_format,
                            vector_dim, embedding, embedding_vector_sha256, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL)
                        """,
                        (
                            chunk.id,
                            artifact.signature_json,
                            artifact.signature_fingerprint,
                            artifact.purpose,
                            artifact.content_hash,
                            artifact.vector_format,
                            artifact.vector_dim,
                            artifact.vector_bytes,
                            artifact.vector_sha256,
                        ),
                    )
                    outcomes.append(EmbeddingArtifactUpsertOutcome(status="created"))
                    continue
                if existing["embedding_signature_json"] != artifact.signature_json:
                    raise EmbeddingArtifactValidationError("signature_fingerprint_collision")
                if existing["embedding_content_hash"] == artifact.content_hash:
                    diagnostics = ()
                    if existing["embedding_vector_sha256"] != artifact.vector_sha256:
                        diagnostics = diagnostics_from_reasons(["incoming_vector_differs"])
                    outcomes.append(
                        EmbeddingArtifactUpsertOutcome(status="reused", diagnostics=diagnostics)
                    )
                    continue
                conn.execute(
                    """
                    UPDATE chunk_embedding_artifacts
                    SET embedding_content_hash = ?,
                        embedding_vector_format = ?,
                        vector_dim = ?,
                        embedding = ?,
                        embedding_vector_sha256 = ?,
                        updated_at = datetime('now')
                    WHERE chunk_id = ?
                      AND embedding_signature_fingerprint = ?
                      AND embedding_purpose = ?
                    """,
                    (
                        artifact.content_hash,
                        artifact.vector_format,
                        artifact.vector_dim,
                        artifact.vector_bytes,
                        artifact.vector_sha256,
                        chunk.id,
                        artifact.signature_fingerprint,
                        artifact.purpose,
                    ),
                )
                outcomes.append(EmbeddingArtifactUpsertOutcome(status="replaced"))
        return outcomes

    def search(
        self,
        *,
        query_embedding: np.ndarray,
        namespaces: list[str] | None,
        limit: int,
        candidate_limit: int | None = None,
    ) -> list[VectorSearchResult]:
        """Legacy compatibility search; it intentionally does not claim signature safety."""
        candidate_limit = candidate_limit or max(limit * 4, limit + 10)
        query = np.asarray(query_embedding, dtype=np.float32)
        rows = self._load_legacy_rows(namespaces=namespaces)
        results: list[VectorSearchResult] = []
        for row in rows:
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
            if vector.shape != query.shape:
                continue
            results.append(
                VectorSearchResult(
                    chunk=self._chunk_from_row(row),
                    raw_score=float(np.dot(query, vector)),
                )
            )
        results.sort(key=lambda item: item.raw_score, reverse=True)
        return results[:candidate_limit]

    def search_embedding_artifacts(
        self,
        *,
        query_signature: EmbeddingModelSignature,
        query_embedding: np.ndarray,
        namespaces: list[str] | None,
        limit: int,
        candidate_limit: int | None = None,
    ) -> ArtifactVectorSearchOutcome:
        candidate_limit = candidate_limit or max(limit * 4, limit + 10)
        query = prepare_embedding_query(signature=query_signature, vector=query_embedding)
        legacy_hash_signature = deterministic_hash_v1_legacy_signature()
        rows = self._load_matching_artifact_rows(
            namespaces=namespaces,
            signature_fingerprint=query.signature_fingerprint,
        )
        artifact_count, incompatible_signature_count = self._artifact_diagnostic_counts(
            namespaces=namespaces,
            signature_fingerprint=query.signature_fingerprint,
        )
        legacy_rows = self._legacy_diagnostic_rows(
            namespaces=namespaces,
            legacy_hash_signature=legacy_hash_signature,
        )
        reasons: list[str] = ["signature_mismatch"] * incompatible_signature_count
        results: list[VectorSearchResult] = []
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
                VectorSearchResult(
                    chunk=self._chunk_from_row(row),
                    raw_score=float(np.dot(query.vector, vector)),
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
        results.sort(key=lambda item: item.raw_score, reverse=True)
        return ArtifactVectorSearchOutcome(
            hits=results[:candidate_limit],
            diagnostics=EmbeddingArtifactDiagnostics(
                scanned_count=artifact_count + len(legacy_rows),
                compatible_count=compatible_count,
                issues=diagnostics_from_reasons(reasons),
            ),
        )

    def inspect_exact_signature_coverage(
        self,
        *,
        signature: EmbeddingModelSignature,
        expected_chunks: Sequence[RagChunkExpectation],
    ) -> ExactSignatureCoverage:
        """Return aggregate readiness facts for one exact document-vector space."""

        expected_by_id: dict[str, RagChunkExpectation] = {}
        for expectation in expected_chunks:
            if not isinstance(expectation, RagChunkExpectation):
                raise ValueError("rag_chunk_expectation_invalid")
            if expectation.chunk_id in expected_by_id:
                raise ValueError("rag_chunk_expectation_duplicate")
            expected_by_id[expectation.chunk_id] = expectation

        signature_json = signature.canonical()
        signature_fingerprint = signature.fingerprint()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            chunk_rows = conn.execute(
                "SELECT id, namespace, source_type, content FROM chunks"
            ).fetchall()
            exact_artifact_rows = conn.execute(
                """
                SELECT chunk_id, embedding_signature_json,
                       embedding_signature_fingerprint, embedding_purpose,
                       embedding_content_hash, embedding_vector_format,
                       vector_dim, embedding, embedding_vector_sha256
                FROM chunk_embedding_artifacts
                WHERE embedding_signature_fingerprint = ?
                  AND embedding_purpose = 'document'
                """,
                (signature_fingerprint,),
            ).fetchall()
            incompatible_signature_artifact_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM chunk_embedding_artifacts
                    WHERE embedding_purpose = 'document'
                      AND embedding_signature_fingerprint != ?
                    """,
                    (signature_fingerprint,),
                ).fetchone()[0]
            )
            legacy_artifact_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM legacy_chunk_embedding_artifacts"
                ).fetchone()[0]
            )
            conn.rollback()
        finally:
            conn.close()

        database_by_id = {row["id"]: row for row in chunk_rows}
        exact_artifact_by_chunk_id = {
            row["chunk_id"]: row for row in exact_artifact_rows
        }
        expected_ids = set(expected_by_id)
        database_ids = set(database_by_id)
        missing_chunk_count = len(expected_ids - database_ids)
        unexpected_chunk_count = len(database_ids - expected_ids)

        current_content_hashes: dict[str, str | None] = {}
        chunk_contract_mismatch_count = 0
        for chunk_id, expectation in expected_by_id.items():
            row = database_by_id.get(chunk_id)
            if row is None:
                continue
            try:
                current_content_hash = canonical_content_hash(row["content"])
            except (
                EmbeddingArtifactValidationError,
                TypeError,
                UnicodeError,
                ValueError,
                OverflowError,
            ):
                current_content_hash = None
            current_content_hashes[chunk_id] = current_content_hash
            if (
                row["namespace"] != expectation.namespace
                or row["source_type"] != expectation.source_type
                or current_content_hash != expectation.content_hash
            ):
                chunk_contract_mismatch_count += 1

        exact_valid_artifact_count = 0
        missing_artifact_count = 0
        content_stale_count = 0
        vector_corrupt_count = 0
        signature_corrupt_count = 0
        for chunk_id in expected_ids:
            if chunk_id not in database_by_id:
                continue
            artifact = exact_artifact_by_chunk_id.get(chunk_id)
            if artifact is None:
                missing_artifact_count += 1
                continue
            if artifact["embedding_signature_json"] != signature_json:
                signature_corrupt_count += 1
                continue
            try:
                validate_stored_embedding_artifact(
                    signature_json=artifact["embedding_signature_json"],
                    signature_fingerprint=artifact["embedding_signature_fingerprint"],
                    content_hash=artifact["embedding_content_hash"],
                    purpose=artifact["embedding_purpose"],
                    vector_format=artifact["embedding_vector_format"],
                    vector_dim=artifact["vector_dim"],
                    vector_bytes=artifact["embedding"],
                    vector_sha256=artifact["embedding_vector_sha256"],
                )
            except (
                EmbeddingArtifactValidationError,
                TypeError,
                UnicodeError,
                ValueError,
                OverflowError,
            ) as exc:
                if isinstance(exc, EmbeddingArtifactValidationError) and exc.reason.startswith(
                    "signature_"
                ):
                    signature_corrupt_count += 1
                else:
                    vector_corrupt_count += 1
                continue
            if current_content_hashes.get(chunk_id) != artifact["embedding_content_hash"]:
                content_stale_count += 1
                continue
            exact_valid_artifact_count += 1

        diagnostic_counts = {
            "chunk_contract_mismatch": chunk_contract_mismatch_count,
            "content_hash_stale": content_stale_count,
            "legacy_unverified": legacy_artifact_count,
            "missing_artifact": missing_artifact_count,
            "missing_chunk": missing_chunk_count,
            "signature_corrupt": signature_corrupt_count,
            "signature_mismatch": incompatible_signature_artifact_count,
            "unexpected_chunk": unexpected_chunk_count,
            "vector_corrupt": vector_corrupt_count,
        }
        diagnostics = EmbeddingArtifactDiagnostics(
            scanned_count=(
                len(exact_artifact_rows)
                + incompatible_signature_artifact_count
                + legacy_artifact_count
            ),
            compatible_count=exact_valid_artifact_count,
            issues=tuple(
                EmbeddingArtifactDiagnostic(reason=reason, count=count)
                for reason, count in sorted(diagnostic_counts.items())
                if count
            ),
        )
        ready_eligible = (
            len(chunk_rows) == len(expected_by_id)
            and exact_valid_artifact_count == len(expected_by_id)
            and missing_chunk_count == 0
            and unexpected_chunk_count == 0
            and chunk_contract_mismatch_count == 0
            and missing_artifact_count == 0
            and content_stale_count == 0
            and vector_corrupt_count == 0
            and signature_corrupt_count == 0
            and incompatible_signature_artifact_count == 0
            and legacy_artifact_count == 0
        )
        return ExactSignatureCoverage(
            database_chunk_count=len(chunk_rows),
            expected_chunk_count=len(expected_by_id),
            exact_valid_artifact_count=exact_valid_artifact_count,
            missing_chunk_count=missing_chunk_count,
            unexpected_chunk_count=unexpected_chunk_count,
            chunk_contract_mismatch_count=chunk_contract_mismatch_count,
            missing_artifact_count=missing_artifact_count,
            content_stale_count=content_stale_count,
            vector_corrupt_count=vector_corrupt_count,
            signature_corrupt_count=signature_corrupt_count,
            incompatible_signature_artifact_count=incompatible_signature_artifact_count,
            legacy_artifact_count=legacy_artifact_count,
            ready_eligible=ready_eligible,
            diagnostics=diagnostics,
        )

    def _load_legacy_rows(self, *, namespaces: list[str] | None) -> list[sqlite3.Row]:
        return self._load_rows(
            """
            SELECT chunks.*, legacy.embedding
            FROM chunks
            INNER JOIN legacy_chunk_embedding_artifacts AS legacy ON legacy.chunk_id = chunks.id
            """,
            namespaces=namespaces,
        )

    def _load_matching_artifact_rows(
        self,
        *,
        namespaces: list[str] | None,
        signature_fingerprint: str,
    ) -> list[sqlite3.Row]:
        query = """
            SELECT chunks.*, artifact.embedding_signature_json,
                   artifact.embedding_signature_fingerprint, artifact.embedding_purpose,
                   artifact.embedding_content_hash, artifact.embedding_vector_format,
                   artifact.vector_dim, artifact.embedding, artifact.embedding_vector_sha256
            FROM chunks
            INNER JOIN chunk_embedding_artifacts AS artifact ON artifact.chunk_id = chunks.id
            WHERE artifact.embedding_signature_fingerprint = ?
              AND artifact.embedding_purpose = 'document'
        """
        params: list[object] = [signature_fingerprint]
        if namespaces:
            placeholders = ", ".join("?" for _ in namespaces)
            query += f" AND chunks.namespace IN ({placeholders})"
            params.extend(namespaces)
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def _artifact_diagnostic_counts(
        self,
        *,
        namespaces: list[str] | None,
        signature_fingerprint: str,
    ) -> tuple[int, int]:
        query = """
            SELECT
                COUNT(*) AS artifact_count,
                COALESCE(SUM(
                    CASE WHEN artifact.embedding_signature_fingerprint != ? THEN 1 ELSE 0 END
                ), 0) AS incompatible_signature_count
            FROM chunks
            INNER JOIN chunk_embedding_artifacts AS artifact ON artifact.chunk_id = chunks.id
            WHERE artifact.embedding_purpose = 'document'
        """
        params: list[object] = [signature_fingerprint]
        if namespaces:
            placeholders = ", ".join("?" for _ in namespaces)
            query += f" AND chunks.namespace IN ({placeholders})"
            params.extend(namespaces)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["artifact_count"]), int(row["incompatible_signature_count"])

    def _legacy_diagnostic_rows(
        self,
        *,
        namespaces: list[str] | None,
        legacy_hash_signature: EmbeddingModelSignature,
    ) -> list[sqlite3.Row]:
        query = """
            SELECT legacy.chunk_id, legacy.embedding_model, legacy.vector_dim,
                   legacy.embedding AS legacy_embedding, chunks.content,
                   artifact.embedding_signature_json AS artifact_signature_json,
                   artifact.embedding_signature_fingerprint AS artifact_signature_fingerprint,
                   artifact.embedding_purpose AS artifact_purpose,
                   artifact.embedding_content_hash AS artifact_content_hash,
                   artifact.embedding_vector_format AS artifact_vector_format,
                   artifact.vector_dim AS artifact_vector_dim,
                   artifact.embedding AS artifact_embedding,
                   artifact.embedding_vector_sha256 AS artifact_vector_sha256
            FROM chunks
            INNER JOIN legacy_chunk_embedding_artifacts AS legacy ON legacy.chunk_id = chunks.id
            LEFT JOIN chunk_embedding_artifacts AS artifact
                ON artifact.chunk_id = legacy.chunk_id
               AND artifact.embedding_signature_fingerprint = ?
               AND artifact.embedding_purpose = 'document'
        """
        params: list[object] = [legacy_hash_signature.fingerprint()]
        if namespaces:
            placeholders = ", ".join("?" for _ in namespaces)
            query += f" WHERE chunks.namespace IN ({placeholders})"
            params.extend(namespaces)
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def _load_rows(self, query: str, *, namespaces: list[str] | None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if namespaces:
                placeholders = ", ".join("?" for _ in namespaces)
                cursor = conn.execute(
                    f"{query} WHERE chunks.namespace IN ({placeholders})",
                    namespaces,
                )
            else:
                cursor = conn.execute(query)
            return cursor.fetchall()

    def _chunk_from_row(self, row: sqlite3.Row) -> ChunkRecord:
        return ChunkRecord(
            id=row["id"],
            namespace=row["namespace"],
            source_type=row["source_type"],
            content=row["content"],
            metadata=json.loads(row["metadata_json"]),
        )

    def count_by_namespace(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT namespace, COUNT(*) FROM chunks GROUP BY namespace"
            ).fetchall()
        return {namespace: count for namespace, count in rows}
