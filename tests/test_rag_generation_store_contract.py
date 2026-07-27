from __future__ import annotations

import sqlite3
from dataclasses import replace

import numpy as np
import pytest

from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest
from app.retrieval.documents import ChunkRecord
from app.retrieval.errors import (
    RagGenerationStoreBackupError,
    RagGenerationStoreError,
    RagGenerationStoreOpenError,
    RagGenerationStoreReadOnlyError,
)
from app.retrieval.vector_index import RagChunkExpectation, SqliteVectorStore


def _provider() -> DeterministicHashEmbeddingProvider:
    return DeterministicHashEmbeddingProvider(
        profile=load_embedding_space_profile("deterministic_hash_v1")
    )


def _chunk(
    chunk_id: str,
    content: str,
    *,
    namespace: str = "canon:lore",
    source_type: str = "test",
) -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        namespace=namespace,
        source_type=source_type,
        content=content,
        metadata={"source": "generation-contract"},
    )


def _artifact(
    provider: DeterministicHashEmbeddingProvider,
    content: str,
    *,
    signature=None,
) -> EmbeddingArtifactWrite:
    canonical_text = canonicalize_embedding_text(content)
    result = provider.embed(EmbeddingRequest(texts=(canonical_text,), purpose="document"))
    return EmbeddingArtifactWrite(
        signature=signature or result.signature,
        content_hash=embedding_content_hash(canonical_text),
        vector=result.vectors[0],
    )


def _write_canonical(
    store: SqliteVectorStore,
    provider: DeterministicHashEmbeddingProvider,
    chunks: list[ChunkRecord],
) -> None:
    outcomes = store.upsert_chunk_embedding_artifacts(
        chunks=chunks,
        artifacts=[_artifact(provider, chunk.content) for chunk in chunks],
    )
    assert [outcome.status for outcome in outcomes] == ["created"] * len(chunks)


def _query(store: SqliteVectorStore, provider: DeterministicHashEmbeddingProvider, text: str):
    result = provider.embed(EmbeddingRequest(texts=(text,), purpose="query"))
    return store.search_embedding_artifacts(
        query_signature=result.signature,
        query_embedding=result.vectors[0],
        namespaces=["canon:lore"],
        limit=4,
    )


def test_read_only_open_does_not_initialize_or_mutate_and_rejects_writes(tmp_path):
    missing_path = tmp_path / "missing-parent" / "generation.sqlite"
    with pytest.raises(RagGenerationStoreOpenError) as raised:
        SqliteVectorStore.open_read_only(missing_path)
    assert raised.value.reason == "rag_generation_store_database_missing"
    assert not missing_path.parent.exists()

    path = tmp_path / "readonly.sqlite"
    store = SqliteVectorStore(path)
    provider = _provider()
    chunk = _chunk("readonly", "read-only canonical content")
    _write_canonical(store, provider, [chunk])
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE chunks_legacy_embedding_v0 (id TEXT PRIMARY KEY)")
    before = path.read_bytes()

    read_only = SqliteVectorStore.open_read_only(path)
    assert [hit.chunk.id for hit in _query(read_only, provider, chunk.content).hits] == [chunk.id]
    assert read_only.count_by_namespace() == {"canon:lore": 1}
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'chunks_legacy_embedding_v0'"
        ).fetchone()

    with pytest.raises(RagGenerationStoreReadOnlyError):
        read_only.reset()
    with pytest.raises(RagGenerationStoreReadOnlyError):
        read_only.upsert_chunks(chunks=[], embeddings=[], embedding_model="test")
    with pytest.raises(RagGenerationStoreReadOnlyError):
        read_only.upsert_chunk_embedding_artifacts(chunks=[], artifacts=[])
    with pytest.raises(RagGenerationStoreReadOnlyError):
        read_only.delete_chunks(chunk_ids=[])


def test_read_only_open_rejects_incomplete_schema_without_repairing_it(tmp_path):
    path = tmp_path / "incomplete.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY)")
    before = path.read_bytes()

    with pytest.raises(RagGenerationStoreOpenError) as raised:
        SqliteVectorStore.open_read_only(path)
    assert raised.value.reason == "rag_generation_store_schema_incompatible"
    assert path.read_bytes() == before


def test_backup_to_uses_consistent_clone_and_never_overwrites_target(tmp_path):
    source_path = tmp_path / "source.sqlite"
    target_path = tmp_path / "clone.sqlite"
    store = SqliteVectorStore(source_path)
    provider = _provider()
    canonical = _chunk("canonical", "canonical backup content")
    legacy = _chunk("legacy", "legacy backup content", namespace="canon:voice")
    with sqlite3.connect(source_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    _write_canonical(store, provider, [canonical])
    store.upsert_chunks(
        chunks=[legacy],
        embeddings=[provider.embed_query(legacy.content)],
        embedding_model="BAAI/bge-m3",
    )

    read_only = SqliteVectorStore.open_read_only(source_path)
    read_only.backup_to(target_path)
    assert read_only.count_by_namespace() == {"canon:lore": 1, "canon:voice": 1}

    clone = SqliteVectorStore(target_path)
    assert clone.count_by_namespace() == {"canon:lore": 1, "canon:voice": 1}
    assert [hit.chunk.id for hit in _query(clone, provider, canonical.content).hits] == [
        canonical.id
    ]
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        assert source.execute(
            "SELECT id, namespace, source_type, content, metadata_json FROM chunks ORDER BY id"
        ).fetchall() == target.execute(
            "SELECT id, namespace, source_type, content, metadata_json FROM chunks ORDER BY id"
        ).fetchall()
        assert source.execute(
            """
            SELECT chunk_id, embedding_signature_json, embedding_signature_fingerprint,
                   embedding_purpose, embedding_content_hash, embedding_vector_format,
                   vector_dim, embedding, embedding_vector_sha256
            FROM chunk_embedding_artifacts
            """
        ).fetchall() == target.execute(
            """
            SELECT chunk_id, embedding_signature_json, embedding_signature_fingerprint,
                   embedding_purpose, embedding_content_hash, embedding_vector_format,
                   vector_dim, embedding, embedding_vector_sha256
            FROM chunk_embedding_artifacts
            """
        ).fetchall()
        legacy_query = (
            "SELECT chunk_id, embedding_model, vector_dim, embedding "
            "FROM legacy_chunk_embedding_artifacts"
        )
        assert source.execute(legacy_query).fetchall() == target.execute(
            legacy_query
        ).fetchall()
    assert not list(tmp_path.glob(".clone.*.backup.tmp"))

    target_path.write_bytes(b"do-not-overwrite")
    with pytest.raises(RagGenerationStoreBackupError) as raised:
        read_only.backup_to(target_path)
    assert raised.value.reason == "rag_generation_store_backup_target_exists"
    assert target_path.read_bytes() == b"do-not-overwrite"
    assert not list(tmp_path.glob(".clone.*.backup.tmp"))

    missing_parent_target = tmp_path / "missing-parent" / "clone.sqlite"
    with pytest.raises(RagGenerationStoreBackupError) as raised:
        read_only.backup_to(missing_parent_target)
    assert raised.value.reason == "rag_generation_store_backup_parent_missing"
    assert not missing_parent_target.parent.exists()


def test_delete_chunks_is_transactional_deduplicated_and_large_batch_safe(tmp_path):
    store = SqliteVectorStore(tmp_path / "delete.sqlite")
    provider = _provider()
    first = _chunk("first", "first delete content")
    second = _chunk("second", "second stays content")
    _write_canonical(store, provider, [first, second])
    second_signature = replace(
        _artifact(provider, first.content).signature,
        profile_id="deterministic_hash_v1_second_space",
    )
    store.upsert_chunk_embedding_artifacts(
        chunks=[first],
        artifacts=[_artifact(provider, first.content, signature=second_signature)],
    )
    store.upsert_chunks(
        chunks=[first, second],
        embeddings=[provider.embed_query(first.content), provider.embed_query(second.content)],
        embedding_model="BAAI/bge-m3",
    )

    result = store.delete_chunks(chunk_ids=[first.id, first.id, "missing"])
    assert result.requested_chunk_count == 2
    assert result.deleted_chunk_count == 1
    assert result.deleted_exact_artifact_count == 2
    assert result.deleted_legacy_artifact_count == 1
    assert store.existing_ids() == {second.id}
    assert store.delete_chunks(chunk_ids=[]).deleted_chunk_count == 0

    large_chunks = [
        _chunk(f"large-{index}", f"large delete content {index}") for index in range(1001)
    ]
    store.upsert_chunks(
        chunks=large_chunks,
        embeddings=[np.array([1.0], dtype=np.float32)] * len(large_chunks),
        embedding_model="legacy-test",
    )
    large_result = store.delete_chunks(chunk_ids=[chunk.id for chunk in large_chunks])
    assert large_result.deleted_chunk_count == len(large_chunks)
    assert large_result.deleted_legacy_artifact_count == len(large_chunks)


def test_delete_chunks_rolls_back_all_tables_on_database_failure(tmp_path):
    path = tmp_path / "delete_rollback.sqlite"
    store = SqliteVectorStore(path)
    provider = _provider()
    allowed = _chunk("allowed", "allowed content")
    blocked = _chunk("blocked", "blocked content")
    _write_canonical(store, provider, [allowed, blocked])
    store.upsert_chunks(
        chunks=[allowed, blocked],
        embeddings=[provider.embed_query(allowed.content), provider.embed_query(blocked.content)],
        embedding_model="legacy-test",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER block_generation_delete
            BEFORE DELETE ON chunks
            WHEN OLD.id = 'blocked'
            BEGIN
                SELECT RAISE(ABORT, 'forced rollback');
            END
            """
        )

    with pytest.raises(RagGenerationStoreError) as raised:
        store.delete_chunks(chunk_ids=[allowed.id, blocked.id])
    assert raised.value.reason == "rag_generation_store_chunk_delete_failed"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM chunk_embedding_artifacts"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_chunk_embedding_artifacts"
        ).fetchone()[0] == 2


def test_exact_signature_coverage_reports_aggregate_generation_readiness(tmp_path):
    path = tmp_path / "coverage.sqlite"
    store = SqliteVectorStore(path)
    provider = _provider()
    valid = _chunk("valid", "valid content")
    mismatch = _chunk("mismatch", "mismatch content")
    missing_artifact = _chunk("missing-artifact", "missing artifact content")
    stale_original = _chunk("stale", "stale original content")
    stale_current = _chunk("stale", "stale current content")
    vector_corrupt = _chunk("vector-corrupt", "vector corrupt content")
    signature_corrupt = _chunk("signature-corrupt", "signature corrupt content")
    other_signature = _chunk("other-signature", "other signature content")
    legacy = _chunk("legacy", "legacy content")
    unexpected = _chunk("unexpected", "unexpected content")
    canonical_chunks = [
        valid,
        mismatch,
        stale_original,
        vector_corrupt,
        signature_corrupt,
        legacy,
    ]
    _write_canonical(store, provider, canonical_chunks)
    store.upsert_chunk_embedding_artifacts(
        chunks=[other_signature],
        artifacts=[
            _artifact(
                provider,
                other_signature.content,
                signature=replace(
                    _artifact(provider, other_signature.content).signature,
                    profile_id="deterministic_hash_v1_other_space",
                ),
            )
        ],
    )
    store.upsert_chunks(
        chunks=[missing_artifact, unexpected],
        embeddings=[
            np.array([1.0], dtype=np.float32),
            np.array([1.0], dtype=np.float32),
        ],
        embedding_model="legacy-test",
    )
    store.upsert_chunks(
        chunks=[legacy],
        embeddings=[provider.embed_query(legacy.content)],
        embedding_model="BAAI/bge-m3",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE chunks SET content = ? WHERE id = ?",
            (stale_current.content, stale_current.id),
        )
        connection.execute(
            "UPDATE chunk_embedding_artifacts SET embedding = ? WHERE chunk_id = ?",
            (b"invalid", vector_corrupt.id),
        )
        connection.execute(
            """
            UPDATE chunk_embedding_artifacts
            SET embedding_signature_json = ?
            WHERE chunk_id = ?
            """,
            ("{}", signature_corrupt.id),
        )

    missing_chunk = RagChunkExpectation(
        chunk_id="missing-chunk",
        namespace="canon:lore",
        source_type="test",
        content_hash="sha256:" + "0" * 64,
    )
    expectations = [
        RagChunkExpectation.from_chunk(valid),
        RagChunkExpectation.from_chunk(
            replace(mismatch, namespace="canon:expected")
        ),
        RagChunkExpectation.from_chunk(missing_artifact),
        RagChunkExpectation.from_chunk(stale_current),
        RagChunkExpectation.from_chunk(vector_corrupt),
        RagChunkExpectation.from_chunk(signature_corrupt),
        RagChunkExpectation.from_chunk(other_signature),
        RagChunkExpectation.from_chunk(legacy),
        missing_chunk,
    ]
    signature = _artifact(provider, valid.content).signature
    coverage = store.inspect_exact_signature_coverage(
        signature=signature,
        expected_chunks=expectations,
    )

    assert coverage.database_chunk_count == 9
    assert coverage.expected_chunk_count == 9
    assert coverage.exact_valid_artifact_count == 3
    assert coverage.missing_chunk_count == 1
    assert coverage.unexpected_chunk_count == 1
    assert coverage.chunk_contract_mismatch_count == 1
    assert coverage.missing_artifact_count == 2
    assert coverage.content_stale_count == 1
    assert coverage.vector_corrupt_count == 1
    assert coverage.signature_corrupt_count == 1
    assert coverage.incompatible_signature_artifact_count == 1
    assert coverage.legacy_artifact_count == 3
    assert coverage.ready_eligible is False
    assert not {"content", "chunk_id", "embedding"} & set(coverage.__dict__)


def test_exact_signature_coverage_is_ready_only_for_a_clean_exact_inventory(tmp_path):
    path = tmp_path / "coverage_ready.sqlite"
    store = SqliteVectorStore(path)
    provider = _provider()
    chunks = [_chunk("one", "one"), _chunk("two", "two")]
    _write_canonical(store, provider, chunks)
    signature = _artifact(provider, chunks[0].content).signature

    read_only = SqliteVectorStore.open_read_only(path)
    coverage = read_only.inspect_exact_signature_coverage(
        signature=signature,
        expected_chunks=[RagChunkExpectation.from_chunk(chunk) for chunk in chunks],
    )
    assert coverage.ready_eligible is True
    assert coverage.exact_valid_artifact_count == len(chunks)
    assert coverage.diagnostics.issues == ()
