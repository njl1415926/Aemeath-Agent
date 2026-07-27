from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import numpy as np
import pytest

from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_store import MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION, SQLiteStore
from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest
from app.memory.schemas import MemoryArtifactEligibility


def _create_memory(store, *, user_id: str = "user_1", content: str = "artifact memory") -> str:
    return store.create_memory(
        user_id=user_id,
        scope="user_global",
        scope_character_id=None,
        scope_session_id=None,
        namespace="user_profile",
        memory_type="preference",
        content=content,
        importance=8,
        confidence=0.9,
        source="test",
        source_message_id=None,
        source_session_id=None,
        source_job_id=None,
        source_message_ids=[],
        related_memory_ids=[],
    )


def _artifact(*, provider: DeterministicHashEmbeddingProvider, text: str, signature=None):
    canonical_text = canonicalize_embedding_text(text)
    result = provider.embed(EmbeddingRequest(texts=(canonical_text,), purpose="document"))
    return EmbeddingArtifactWrite(
        signature=signature or result.signature,
        content_hash=embedding_content_hash(canonical_text),
        vector=result.vectors[0],
    )


def _query(
    store,
    *,
    provider: DeterministicHashEmbeddingProvider,
    text: str,
    user_id: str = "user_1",
    signature=None,
):
    result = provider.embed(EmbeddingRequest(texts=(text,), purpose="query"))
    return store.search_memory_embedding_artifacts(
        eligibility=MemoryArtifactEligibility(
            user_id=user_id,
            character_id="aemeath",
            session_id="session_a",
        ),
        query_signature=signature or result.signature,
        query_embedding=result.vectors[0],
        limit=4,
    )


def _issue_counts(outcome) -> dict[str, int]:
    return {issue.reason: issue.count for issue in outcome.diagnostics.issues}


def _update_memory_content(store, *, memory_id: str, content: str) -> None:
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE long_term_memories SET content = ? WHERE id = ?",
            (content, memory_id),
        )


def _corrupt_vector(store, *, memory_id: str, fingerprint: str) -> None:
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            UPDATE long_term_memory_embedding_artifacts
            SET embedding = ?
            WHERE memory_id = ? AND embedding_signature_fingerprint = ?
            """,
            (b"invalid", memory_id, fingerprint),
        )


def test_embedding_artifacts_isolate_exact_signatures_and_preserve_spaces(store):
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    memory_id = _create_memory(store, content="same dimension different space")
    first = _artifact(provider=provider, text="same dimension different space")
    second_signature = replace(first.signature, profile_id="deterministic_hash_v1_other_space")
    second = _artifact(
        provider=provider,
        text="same dimension different space",
        signature=second_signature,
    )

    assert store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=first,
    ).status == "created"
    assert store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=second,
    ).status == "created"

    first_outcome = _query(provider=provider, store=store, text="same dimension different space")
    assert [hit.memory_id for hit in first_outcome.hits] == [memory_id]
    assert _issue_counts(first_outcome) == {"signature_mismatch": 1}

    second_outcome = _query(
        provider=provider,
        store=store,
        text="same dimension different space",
        signature=second_signature,
    )
    assert [hit.memory_id for hit in second_outcome.hits] == [memory_id]
    assert _issue_counts(second_outcome) == {"signature_mismatch": 1}


def test_embedding_artifact_same_content_preserves_existing_vector(store):
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    memory_id = _create_memory(store, content="stable vector source")
    first = _artifact(provider=provider, text="stable vector source")
    incoming_vector = np.roll(first.vector, 1).astype("<f4")
    incoming = EmbeddingArtifactWrite(
        signature=first.signature,
        content_hash=first.content_hash,
        vector=incoming_vector,
    )

    assert store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=first,
    ).status == "created"
    reused = store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=incoming,
    )
    assert reused.status == "reused"
    assert [(item.reason, item.count) for item in reused.diagnostics] == [
        ("incoming_vector_differs", 1)
    ]

    outcome = _query(store=store, provider=provider, text="stable vector source")
    assert outcome.hits[0].memory_id == memory_id
    assert outcome.hits[0].score == pytest.approx(1.0)


def test_legacy_bge_remains_unverified_alongside_canonical_hash_artifact(store):
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    memory_id = _create_memory(store, content="mixed legacy and canonical spaces")
    store.upsert_memory_embedding(
        memory_id=memory_id,
        user_id="user_1",
        embedding_model="BAAI/bge-m3",
        embedding=provider.embed_query("mixed legacy and canonical spaces"),
    )
    artifact = _artifact(provider=provider, text="mixed legacy and canonical spaces")
    assert store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=artifact,
    ).status == "created"

    outcome = _query(
        store=store,
        provider=provider,
        text="mixed legacy and canonical spaces",
    )
    assert [hit.memory_id for hit in outcome.hits] == [memory_id]
    assert _issue_counts(outcome) == {"legacy_unverified": 1}


def test_embedding_artifact_replaces_only_after_target_content_changes(store):
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    memory_id = _create_memory(store, content="before content update")
    first = _artifact(provider=provider, text="before content update")
    store.upsert_memory_embedding_artifact(memory_id=memory_id, user_id="user_1", artifact=first)

    _update_memory_content(store, memory_id=memory_id, content="after content update")
    replacement = _artifact(provider=provider, text="after content update")
    outcome = store.upsert_memory_embedding_artifact(
        memory_id=memory_id,
        user_id="user_1",
        artifact=replacement,
    )
    assert outcome.status == "replaced"
    search_outcome = _query(store=store, provider=provider, text="after content update")
    assert [hit.memory_id for hit in search_outcome.hits] == [memory_id]


def test_embedding_artifact_reports_stale_and_corrupt_visible_rows_only(store):
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    stale_id = _create_memory(store, content="stale content")
    corrupt_id = _create_memory(store, content="corrupt content")
    other_user_id = _create_memory(store, user_id="user_2", content="other user corrupt content")
    stale = _artifact(provider=provider, text="stale content")
    corrupt = _artifact(provider=provider, text="corrupt content")
    other = _artifact(provider=provider, text="other user corrupt content")
    for memory_id, user_id, artifact in (
        (stale_id, "user_1", stale),
        (corrupt_id, "user_1", corrupt),
        (other_user_id, "user_2", other),
    ):
        store.upsert_memory_embedding_artifact(
            memory_id=memory_id,
            user_id=user_id,
            artifact=artifact,
        )
    _update_memory_content(store, memory_id=stale_id, content="stale content changed")
    _corrupt_vector(store, memory_id=corrupt_id, fingerprint=corrupt.signature.fingerprint())
    _corrupt_vector(store, memory_id=other_user_id, fingerprint=other.signature.fingerprint())

    outcome = _query(store=store, provider=provider, text="stale content")
    assert outcome.hits == []
    assert outcome.diagnostics.scanned_count == 2
    assert _issue_counts(outcome) == {"content_hash_stale": 1, "vector_corrupt": 1}


def test_raw_sqlite_migrates_only_verified_legacy_hash_without_blocking_other_rows(tmp_path):
    path = tmp_path / "raw_legacy.db"
    store = SQLiteStore(path)
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    good_id = _create_memory(store, content="verified legacy hash")
    unknown_id = _create_memory(store, content="unverified legacy bge")
    store.upsert_memory_embedding(
        memory_id=good_id,
        user_id="user_1",
        embedding_model="deterministic_hash_v1",
        embedding=provider.embed_query("verified legacy hash"),
    )
    store.upsert_memory_embedding(
        memory_id=unknown_id,
        user_id="user_1",
        embedding_model="BAAI/bge-m3",
        embedding=provider.embed_query("unverified legacy bge"),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
        )

    migrated = SQLiteStore(path)
    outcome = _query(store=migrated, provider=provider, text="verified legacy hash")
    assert [hit.memory_id for hit in outcome.hits] == [good_id]
    assert _issue_counts(outcome)["legacy_unverified"] == 1


def test_raw_sqlite_legacy_row_errors_do_not_block_artifact_migration(tmp_path):
    path = tmp_path / "raw_legacy_row_errors.db"
    store = SQLiteStore(path)
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    good_id = _create_memory(store, content="verified despite bad legacy rows")
    blank_id = _create_memory(store, content="will become blank")
    text_blob_id = _create_memory(store, content="legacy text blob")
    for memory_id, text in (
        (good_id, "verified despite bad legacy rows"),
        (blank_id, "will become blank"),
        (text_blob_id, "legacy text blob"),
    ):
        store.upsert_memory_embedding(
            memory_id=memory_id,
            user_id="user_1",
            embedding_model="deterministic_hash_v1",
            embedding=provider.embed_query(text),
        )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE long_term_memories SET content = '' WHERE id = ?",
            (blank_id,),
        )
        connection.execute(
            "UPDATE long_term_memory_embeddings SET embedding = ? WHERE memory_id = ?",
            ("not-a-blob", text_blob_id),
        )
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
        )

    migrated = SQLiteStore(path)
    outcome = _query(
        store=migrated,
        provider=provider,
        text="verified despite bad legacy rows",
    )
    assert [hit.memory_id for hit in outcome.hits] == [good_id]
    assert _issue_counts(outcome) == {"legacy_unverified": 2}
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION,),
        ).fetchone()


def test_embedding_artifact_upserts_are_serialized_per_identity(store):
    profile = load_embedding_space_profile("deterministic_hash_v1")
    provider = DeterministicHashEmbeddingProvider(profile=profile)
    memory_id = _create_memory(store, content="concurrent artifact write")
    artifact = _artifact(provider=provider, text="concurrent artifact write")
    first_store = type(store)(store.db_path)
    second_store = type(store)(store.db_path)
    barrier = Barrier(2)

    def write(concurrent_store):
        barrier.wait()
        return concurrent_store.upsert_memory_embedding_artifact(
            memory_id=memory_id,
            user_id="user_1",
            artifact=artifact,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, (first_store, second_store)))

    assert sorted(outcome.status for outcome in outcomes) == ["created", "reused"]


def test_sqlalchemy_legacy_experimental_database_requires_manual_rebuild(tmp_path):
    path = tmp_path / "sqlalchemy_legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE long_term_memory_embeddings (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                vector_dim INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )
    with pytest.raises(StorageMigrationRequiredError) as raised:
        SqlAlchemyStore(path)
    assert raised.value.code.value == "STORAGE_MIGRATION_REQUIRED"


def test_sqlalchemy_incomplete_artifact_table_requires_manual_rebuild(tmp_path):
    path = tmp_path / "sqlalchemy_incomplete_artifact.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE long_term_memory_embedding_artifacts (
                memory_id TEXT PRIMARY KEY
            )
            """
        )

    with pytest.raises(StorageMigrationRequiredError):
        SqlAlchemyStore(path)
