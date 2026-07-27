from __future__ import annotations

import sqlite3
from dataclasses import replace

import numpy as np

from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest
from app.retrieval.documents import ChunkRecord
from app.retrieval.vector_index import SqliteVectorStore


def _chunk(*, chunk_id: str = "chunk_1", content: str = "artifact chunk") -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        namespace="canon:lore",
        source_type="test",
        content=content,
        metadata={"source": "test"},
    )


def _artifact(provider: DeterministicHashEmbeddingProvider, text: str, *, signature=None):
    canonical_text = canonicalize_embedding_text(text)
    result = provider.embed(EmbeddingRequest(texts=(canonical_text,), purpose="document"))
    return EmbeddingArtifactWrite(
        signature=signature or result.signature,
        content_hash=embedding_content_hash(canonical_text),
        vector=result.vectors[0],
    )


def _query(
    store: SqliteVectorStore,
    provider: DeterministicHashEmbeddingProvider,
    text: str,
    *,
    signature=None,
):
    result = provider.embed(EmbeddingRequest(texts=(text,), purpose="query"))
    return store.search_embedding_artifacts(
        query_signature=signature or result.signature,
        query_embedding=result.vectors[0],
        namespaces=["canon:lore"],
        limit=4,
    )


def _issues(outcome) -> dict[str, int]:
    return {issue.reason: issue.count for issue in outcome.diagnostics.issues}


def test_rag_artifacts_support_multiple_signatures_and_reuse_existing_vector(tmp_path):
    store = SqliteVectorStore(tmp_path / "rag_artifacts.sqlite")
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    chunk = _chunk(content="same chunk across spaces")
    first = _artifact(provider, chunk.content)
    other_signature = replace(first.signature, profile_id="deterministic_hash_v1_other_space")
    second = _artifact(provider, chunk.content, signature=other_signature)

    assert [item.status for item in store.upsert_chunk_embedding_artifacts(
        chunks=[chunk],
        artifacts=[first],
    )] == ["created"]
    assert [item.status for item in store.upsert_chunk_embedding_artifacts(
        chunks=[chunk],
        artifacts=[second],
    )] == ["created"]
    incoming = EmbeddingArtifactWrite(
        signature=first.signature,
        content_hash=first.content_hash,
        vector=np.roll(first.vector, 1).astype("<f4"),
    )
    reused = store.upsert_chunk_embedding_artifacts(chunks=[chunk], artifacts=[incoming])[0]
    assert reused.status == "reused"
    assert reused.diagnostics[0].reason == "incoming_vector_differs"

    first_outcome = _query(store, provider, chunk.content)
    assert [hit.chunk.id for hit in first_outcome.hits] == [chunk.id]
    assert _issues(first_outcome) == {"signature_mismatch": 1}
    second_outcome = _query(store, provider, chunk.content, signature=other_signature)
    assert [hit.chunk.id for hit in second_outcome.hits] == [chunk.id]


def test_rag_legacy_bge_remains_unverified_alongside_canonical_hash_artifact(tmp_path):
    store = SqliteVectorStore(tmp_path / "mixed_rag_artifacts.sqlite")
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    chunk = _chunk(content="mixed legacy and canonical RAG spaces")
    store.upsert_chunks(
        chunks=[chunk],
        embeddings=[provider.embed_query(chunk.content)],
        embedding_model="BAAI/bge-m3",
    )
    artifact = _artifact(provider, chunk.content)
    outcomes = store.upsert_chunk_embedding_artifacts(
        chunks=[chunk],
        artifacts=[artifact],
    )
    assert outcomes[0].status == "created"

    outcome = _query(store, provider, chunk.content)
    assert [hit.chunk.id for hit in outcome.hits] == [chunk.id]
    assert _issues(outcome) == {"legacy_unverified": 1}


def test_rag_artifact_migration_promotes_only_verified_hash_rows(tmp_path):
    path = tmp_path / "legacy_rag.sqlite"
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    chunk = _chunk(content="verified old RAG hash")
    vector = provider.embed_query(chunk.content)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE chunks (
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
        connection.execute(
            """
            INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.id,
                chunk.namespace,
                chunk.source_type,
                chunk.content,
                "{}",
                "deterministic_hash_v1",
                vector.shape[0],
                vector.tobytes(),
            ),
        )

    store = SqliteVectorStore(path)
    outcome = _query(store, provider, chunk.content)
    assert [hit.chunk.id for hit in outcome.hits] == [chunk.id]
    assert _issues(outcome) == {}
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'chunks_legacy_embedding_v0'"
        ).fetchone() is None


def test_rag_legacy_row_errors_do_not_block_artifact_migration(tmp_path):
    path = tmp_path / "legacy_rag_row_errors.sqlite"
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    good_chunk = _chunk(chunk_id="good", content="verified despite bad legacy chunks")
    blank_chunk = _chunk(chunk_id="blank", content="")
    text_blob_chunk = _chunk(chunk_id="text", content="legacy text blob")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE chunks (
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
        for chunk, embedding in (
            (good_chunk, provider.embed_query(good_chunk.content).tobytes()),
            (blank_chunk, provider.embed_query("legacy blank source").tobytes()),
            (text_blob_chunk, "not-a-blob"),
        ):
            connection.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.id,
                    chunk.namespace,
                    chunk.source_type,
                    chunk.content,
                    "{}",
                    "deterministic_hash_v1",
                    384,
                    embedding,
                ),
            )

    store = SqliteVectorStore(path)
    outcome = _query(store, provider, good_chunk.content)
    assert [hit.chunk.id for hit in outcome.hits] == [good_chunk.id]
    assert _issues(outcome) == {"legacy_unverified": 2}


def test_rag_marker_cleanup_removes_a_leftover_temporary_legacy_table(tmp_path):
    path = tmp_path / "rag_marker_cleanup.sqlite"
    SqliteVectorStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE chunks_legacy_embedding_v0 (id TEXT PRIMARY KEY)"
        )

    SqliteVectorStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'chunks_legacy_embedding_v0'"
        ).fetchone() is None
