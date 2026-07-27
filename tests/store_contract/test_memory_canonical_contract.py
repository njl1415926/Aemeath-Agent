from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from app.db.errors import StorageMigrationRequiredError
from app.db.sqlalchemy_store import SqlAlchemyStore
from app.db.sqlite_index_contract import _normalize_sql
from app.db.sqlite_store import (
    MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION,
    SQLiteStore,
)
from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.schemas import EmbeddingRequest
from app.memory.schemas import MemoryArtifactEligibility


def _memory_kwargs(
    *,
    user_id: str = "user_1",
    scope: str = "user_global",
    scope_character_id: str | None = None,
    scope_session_id: str | None = None,
    namespace: str = "user_profile",
    memory_type: str = "preference",
    content: str = "canonical memory",
    importance: int = 8,
    confidence: float = 0.9,
    source: str = "test",
) -> dict[str, object]:
    return {
        "user_id": user_id,
        "scope": scope,
        "scope_character_id": scope_character_id,
        "scope_session_id": scope_session_id,
        "namespace": namespace,
        "memory_type": memory_type,
        "content": content,
        "importance": importance,
        "confidence": confidence,
        "source": source,
        "source_message_id": "message_1",
        "source_session_id": "session_source",
        "source_job_id": "job_1",
        "source_message_ids": ["message_1"],
        "related_memory_ids": ["memory_related"],
    }


def _create_memory(store, **overrides: object) -> str:
    return store.create_memory(**_memory_kwargs(**overrides))


def _create_or_get(store, **overrides: object):
    return store.create_or_get_exact_active_memory(**_memory_kwargs(**overrides))


def _artifact(
    provider: DeterministicHashEmbeddingProvider,
    text: str,
    *,
    signature=None,
) -> EmbeddingArtifactWrite:
    canonical_text = canonicalize_embedding_text(text)
    result = provider.embed(EmbeddingRequest(texts=(canonical_text,), purpose="document"))
    return EmbeddingArtifactWrite(
        signature=signature or result.signature,
        content_hash=embedding_content_hash(canonical_text),
        vector=result.vectors[0],
    )


def _provider() -> DeterministicHashEmbeddingProvider:
    return DeterministicHashEmbeddingProvider(
        profile=load_embedding_space_profile("deterministic_hash_v1")
    )


def test_create_or_get_exact_active_memory_is_atomic_and_preserves_first_write(store):
    first = _create_or_get(
        store,
        content="first write wins",
        namespace="first_namespace",
        importance=9,
        confidence=0.8,
        source="first_source",
    )
    reused = _create_or_get(
        store,
        content="first write wins",
        namespace="second_namespace",
        importance=1,
        confidence=0.1,
        source="second_source",
    )

    assert first.created is True
    assert reused == type(first)(memory_id=first.memory_id, created=False)
    memory = store.get_memory(memory_id=first.memory_id)
    assert memory is not None
    assert memory.namespace == "first_namespace"
    assert memory.importance == 9
    assert memory.confidence == 0.8
    assert memory.source == "first_source"
    assert memory.source_message_id == "message_1"
    assert memory.source_session_id == "session_source"
    assert memory.source_job_id == "job_1"
    assert memory.source_message_ids == ["message_1"]


def test_create_or_get_exact_active_memory_identity_separates_scopes_and_inactive_rows(store):
    content = "same content, distinct identities"
    identities = (
        {"scope": "user_global"},
        {"scope": "character", "scope_character_id": "character_a"},
        {
            "scope": "session",
            "scope_character_id": "character_a",
            "scope_session_id": "session_a",
        },
        {
            "scope": "session",
            "scope_character_id": "character_a",
            "scope_session_id": "session_b",
        },
    )
    created = [_create_or_get(store, content=content, **identity) for identity in identities]
    assert all(result.created for result in created)
    assert len({result.memory_id for result in created}) == len(identities)
    assert all(
        not _create_or_get(store, content=content, **identity).created
        for identity in identities
    )

    active_id = created[0].memory_id
    assert store.archive_memory(memory_id=active_id, user_id="user_1") is True
    after_archive = _create_or_get(store, content=content)
    assert after_archive.created is True
    assert store.disable_memory(memory_id=after_archive.memory_id, user_id="user_1") is True
    after_disable = _create_or_get(store, content=content)
    assert after_disable.created is True


def test_create_or_get_exact_active_memory_serializes_two_store_instances(store):
    first_store = type(store)(store.db_path)
    second_store = type(store)(store.db_path)
    barrier = Barrier(2)

    def create(concurrent_store):
        barrier.wait()
        return _create_or_get(concurrent_store, content="concurrent active identity")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, (first_store, second_store)))

    assert sorted(result.created for result in results) == [False, True]
    assert len({result.memory_id for result in results}) == 1


def test_embedding_search_reports_eligible_count_with_all_visibility_filters(store):
    provider = _provider()
    allowed = (
        _create_memory(store, content="eligible global"),
        _create_memory(
            store,
            content="eligible character",
            scope="character",
            scope_character_id="character_a",
        ),
        _create_memory(
            store,
            content="eligible session",
            scope="session",
            scope_character_id="character_a",
            scope_session_id="session_a",
        ),
    )
    excluded = (
        _create_memory(store, content="excluded namespace", namespace="other"),
        _create_memory(store, content="excluded type", memory_type="fact"),
        _create_memory(store, content="excluded confidence", confidence=0.49),
        _create_memory(
            store,
            content="excluded character",
            scope="character",
            scope_character_id="character_b",
        ),
        _create_memory(
            store,
            content="excluded session",
            scope="session",
            scope_character_id="character_a",
            scope_session_id="session_b",
        ),
        _create_memory(store, user_id="user_2", content="excluded owner"),
    )
    for memory_id, text, user_id in (
        *((memory_id, text, "user_1") for memory_id, text in zip(allowed, (
            "eligible global",
            "eligible character",
            "eligible session",
        ), strict=True)),
        *((memory_id, text, "user_1") for memory_id, text in zip(excluded[:-1], (
            "excluded namespace",
            "excluded type",
            "excluded confidence",
            "excluded character",
            "excluded session",
        ), strict=True)),
        (excluded[-1], "excluded owner", "user_2"),
    ):
        store.upsert_memory_embedding_artifact(
            memory_id=memory_id,
            user_id=user_id,
            artifact=_artifact(provider, text),
        )

    query = provider.embed(EmbeddingRequest(texts=("eligible query",), purpose="query"))
    outcome = store.search_memory_embedding_artifacts(
        eligibility=MemoryArtifactEligibility(
            user_id="user_1",
            character_id="character_a",
            session_id="session_a",
            namespaces=("user_profile",),
            memory_types=("preference",),
            min_confidence=0.5,
        ),
        query_signature=query.signature,
        query_embedding=query.vectors[0],
        limit=10,
    )

    assert outcome.eligible_memory_count == 3
    assert {hit.memory_id for hit in outcome.hits} == set(allowed)


def test_embedding_artifact_gaps_classify_exact_space_without_cross_space_damage(store):
    provider = _provider()
    signature = _artifact(provider, "signature source").signature
    healthy_id = _create_memory(store, content="healthy")
    stale_id = _create_memory(store, content="stale before update")
    corrupt_id = _create_memory(store, content="vector corrupt")
    signature_corrupt_id = _create_memory(store, content="signature corrupt")
    legacy_id = _create_memory(store, content="legacy only")
    other_space_id = _create_memory(store, content="other canonical space")
    missing_id = _create_memory(store, content="missing")
    other_user_id = _create_memory(store, user_id="user_2", content="other user missing")

    for memory_id, text in (
        (healthy_id, "healthy"),
        (stale_id, "stale before update"),
        (corrupt_id, "vector corrupt"),
        (signature_corrupt_id, "signature corrupt"),
    ):
        store.upsert_memory_embedding_artifact(
            memory_id=memory_id,
            user_id="user_1",
            artifact=_artifact(provider, text),
        )
    other_signature = replace(signature, profile_id="deterministic_hash_v1_other_space")
    store.upsert_memory_embedding_artifact(
        memory_id=other_space_id,
        user_id="user_1",
        artifact=_artifact(provider, "other canonical space", signature=other_signature),
    )
    store.upsert_memory_embedding(
        memory_id=legacy_id,
        user_id="user_1",
        embedding_model="BAAI/bge-m3",
        embedding=provider.embed_query("legacy only"),
    )

    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE long_term_memories SET content = ? WHERE id = ?",
            ("stale after update", stale_id),
        )
        connection.execute(
            "UPDATE long_term_memory_embedding_artifacts SET embedding = ? WHERE memory_id = ?",
            (b"not-a-vector", corrupt_id),
        )
        connection.execute(
            """
            UPDATE long_term_memory_embedding_artifacts
            SET embedding_signature_json = ?
            WHERE memory_id = ?
            """,
            ("{}", signature_corrupt_id),
        )

    owner_page = store.list_memory_embedding_artifact_gaps(
        query_signature=signature,
        user_id="user_1",
        limit=20,
    )
    reasons = {gap.memory_id: gap.reason for gap in owner_page.gaps}
    assert healthy_id not in reasons
    assert reasons[missing_id] == "missing"
    assert reasons[other_space_id] == "missing"
    assert reasons[legacy_id] == "legacy_only"
    assert reasons[stale_id] == "content_stale"
    assert reasons[corrupt_id] == "vector_corrupt"
    assert reasons[signature_corrupt_id] == "signature_corrupt"

    all_users_page = store.list_memory_embedding_artifact_gaps(
        query_signature=signature,
        limit=20,
    )
    assert other_user_id in {gap.memory_id for gap in all_users_page.gaps}
    assert other_user_id not in reasons


def test_embedding_artifact_gaps_page_at_last_checked_memory_without_skips(store):
    provider = _provider()
    signature = _artifact(provider, "pagination signature").signature
    memory_ids = [
        _create_memory(store, content=f"missing page {index}") for index in range(5)
    ]
    with sqlite3.connect(store.db_path) as connection:
        for index, memory_id in enumerate(memory_ids):
            connection.execute(
                "UPDATE long_term_memories SET created_at = ? WHERE id = ?",
                (f"2026-07-26T00:00:0{index}Z", memory_id),
            )

    cursor = None
    collected: list[str] = []
    first_page = True
    while True:
        page = store.list_memory_embedding_artifact_gaps(
            query_signature=signature,
            user_id="user_1",
            cursor=cursor,
            limit=2,
        )
        if first_page:
            assert page.scanned_memory_count == 2
            assert page.next_cursor is not None
            first_page = False
        collected.extend(gap.memory_id for gap in page.gaps)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert collected == memory_ids
    assert len(collected) == len(set(collected))


def test_raw_duplicate_active_identity_requires_manual_migration_without_data_changes(tmp_path):
    path = tmp_path / "raw_duplicate_active_identity.db"
    store = SQLiteStore(path)
    memory_id = _create_memory(store, content="duplicate identity content")
    columns = (
        "user_id, scope, scope_character_id, scope_session_id, namespace, memory_type, "
        "content, importance, confidence, source, source_message_id, source_session_id, "
        "source_job_id, source_message_ids_json, related_memory_ids, version, status, "
        "last_used_at, use_count, decay_score, created_at, updated_at, expiry, is_active"
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX uq_memories_active_exact_identity")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION,),
        )
        connection.execute(
            f"""
            INSERT INTO long_term_memories (id, {columns})
            SELECT ?, {columns}
            FROM long_term_memories
            WHERE id = ?
            """,
            ("memory_duplicate_active_identity", memory_id),
        )
        before = connection.execute(
            "SELECT id, status, is_active FROM long_term_memories ORDER BY id"
        ).fetchall()

    with pytest.raises(StorageMigrationRequiredError) as raised:
        SQLiteStore(path)
    assert raised.value.metadata["reason"] == "raw_active_memory_identity_duplicates"
    assert "duplicate identity content" not in str(raised.value)
    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT id, status, is_active FROM long_term_memories ORDER BY id"
        ).fetchall()
        marker = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION,),
        ).fetchone()
    assert after == before
    assert marker is None


@pytest.mark.parametrize("wrong_index", [False, True])
def test_sqlalchemy_rejects_missing_or_wrong_active_identity_index(tmp_path, wrong_index):
    path = tmp_path / f"sqlalchemy_identity_index_{wrong_index}.db"
    SqlAlchemyStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX uq_memories_active_exact_identity")
        if wrong_index:
            connection.execute(
                """
                CREATE UNIQUE INDEX uq_memories_active_exact_identity
                ON long_term_memories (user_id, scope, content)
                WHERE status = 'active' AND is_active = 1
                """
            )

    with pytest.raises(StorageMigrationRequiredError) as raised:
        SqlAlchemyStore(path)
    assert raised.value.metadata["reason"] == "sqlalchemy_active_memory_identity_index_invalid"


@pytest.mark.parametrize(
    ("store_class", "reason"),
    (
        (SQLiteStore, "raw_active_memory_identity_index_invalid"),
        (SqlAlchemyStore, "sqlalchemy_active_memory_identity_index_invalid"),
    ),
)
@pytest.mark.parametrize(
    "predicate",
    (
        "status = 'active' AND is_active = 1 OR 1 = 1",
        "status = 'active'",
    ),
)
def test_active_identity_index_rejects_expanded_or_narrowed_predicate(
    tmp_path,
    store_class,
    reason,
    predicate,
):
    path = tmp_path / f"identity_predicate_{store_class.__name__}_{len(predicate)}.db"
    store_class(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX uq_memories_active_exact_identity")
        connection.execute(
            f"""
            CREATE UNIQUE INDEX uq_memories_active_exact_identity
            ON long_term_memories (
                user_id,
                scope,
                COALESCE(scope_character_id, ''),
                COALESCE(scope_session_id, ''),
                content
            )
            WHERE {predicate}
            """
        )

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store_class(path)
    assert raised.value.metadata["reason"] == reason


def test_active_identity_index_normalizer_preserves_single_quoted_literals():
    assert _normalize_sql(
        " STATUS = 'ACTIVE ' AND note = 'it''s' AND COALESCE(value, ' ') = '' "
    ) == "status='ACTIVE 'andnote='it''s'andcoalesce(value,' ')=''"


@pytest.mark.parametrize(
    ("store_class", "reason"),
    (
        (SQLiteStore, "raw_active_memory_identity_index_invalid"),
        (SqlAlchemyStore, "sqlalchemy_active_memory_identity_index_invalid"),
    ),
)
@pytest.mark.parametrize(
    ("scope_character_default", "predicate"),
    (
        ("''", "status = 'active ' AND is_active = 1"),
        ("''", "status = 'ACTIVE' AND is_active = 1"),
        ("' '", "status = 'active' AND is_active = 1"),
    ),
    ids=("literal_trailing_space", "literal_uppercase", "coalesce_space"),
)
def test_active_identity_index_rejects_literal_mutations(
    tmp_path,
    store_class,
    reason,
    scope_character_default,
    predicate,
):
    path = tmp_path / f"identity_literal_{store_class.__name__}_{len(predicate)}.db"
    store_class(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX uq_memories_active_exact_identity")
        connection.execute(
            f"""
            CREATE UNIQUE INDEX uq_memories_active_exact_identity
            ON long_term_memories (
                user_id,
                scope,
                COALESCE(scope_character_id, {scope_character_default}),
                COALESCE(scope_session_id, ''),
                content
            )
            WHERE {predicate}
            """
        )

    with pytest.raises(StorageMigrationRequiredError) as raised:
        store_class(path)
    assert raised.value.metadata["reason"] == reason


@pytest.mark.parametrize("store_class", (SQLiteStore, SqlAlchemyStore))
def test_archived_memory_identity_can_be_recreated_after_restart(tmp_path, store_class):
    path = tmp_path / f"archive_recreate_{store_class.__name__}.db"
    store = store_class(path)
    first = _create_or_get(store, content="archive then recreate")
    assert store.archive_memory(memory_id=first.memory_id, user_id="user_1") is True

    restarted = store_class(path)
    replacement = _create_or_get(restarted, content="archive then recreate")
    assert replacement.created is True
    assert replacement.memory_id != first.memory_id
