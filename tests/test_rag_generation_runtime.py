from __future__ import annotations

import sqlite3
from dataclasses import replace

import numpy as np
import pytest

from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingInvalidResponseError
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.query import DirectQueryEmbeddingSource
from app.embeddings.runtime import EmbeddingProviderRuntime
from app.embeddings.schemas import EmbeddingRequest, ResolvedQueryEmbedding
from app.retrieval.canonical_retriever import CanonicalRagRetriever
from app.retrieval.documents import ChunkRecord
from app.retrieval.errors import (
    RagIndexBuildError,
    RagIndexLockError,
    RagIndexManifestError,
)
from app.retrieval.generation import (
    RagBuildSpec,
    RagGenerationBuilder,
    RagIndexConfig,
    ReadyGenerationRegistry,
)
from app.retrieval.index_lock import CrossProcessFileLock
from app.retrieval.index_manifest import (
    ActiveGenerationReference,
    BuildCheckpoint,
    GenerationManifest,
    RagIndexFileRepository,
    build_corpus_inventory,
    manifest_fingerprint,
    sanitize_source_id,
    utc_now,
)
from app.retrieval.manage_rag_index import _with_provider_runtime
from app.retrieval.manage_rag_index import main as rag_index_main
from app.retrieval.vector_index import SqliteVectorStore


def _chunk(
    chunk_id: str,
    content: str,
    *,
    namespace: str = "canon:lore",
    title: str = "Lore title",
) -> ChunkRecord:
    return ChunkRecord(
        id=chunk_id,
        namespace=namespace,
        source_type="flow_scene_window" if namespace != "canon:voice" else "favor_word",
        content=content,
        metadata={
            "source_path": "C:/private/source/path.json",
            "generated_at": "unstable",
            "state_hash": "abc123def0",
            "source_order_start": 10,
            "source_order_end": 20,
            "chunk_index": 0,
            "title": title,
        },
    )


class RecordingProvider:
    def __init__(self, *, interrupt_on_call: int | None = None, fail: bool = False) -> None:
        profile = load_embedding_space_profile("deterministic_hash_v1")
        self.delegate = DeterministicHashEmbeddingProvider(profile=profile)
        self.requests: list[EmbeddingRequest] = []
        self.interrupt_on_call = interrupt_on_call
        self.fail = fail

    @property
    def signature(self):
        return self.delegate.signature

    def embed(self, request: EmbeddingRequest):
        self.requests.append(request)
        if self.interrupt_on_call == len(self.requests):
            raise KeyboardInterrupt("simulated process interruption")
        if self.fail:
            raise EmbeddingInvalidResponseError("forced_test_failure")
        return self.delegate.embed(request)


class CountingQuerySource:
    def __init__(self) -> None:
        profile = load_embedding_space_profile("deterministic_hash_v1")
        self.provider = DeterministicHashEmbeddingProvider(profile=profile)
        self.resolve_count = 0

    @property
    def signature(self):
        return self.provider.signature

    def resolve(self, *, query_text: str):
        self.resolve_count += 1
        return DirectQueryEmbeddingSource(self.provider).resolve(query_text=query_text)


def _builder(
    tmp_path,
    provider,
    *,
    build_spec: RagBuildSpec | None = None,
    batch_size: int = 2,
):
    config = RagIndexConfig(index_root=tmp_path / "indexes", index_id="wuwa_canon")
    return RagGenerationBuilder(
        config=config,
        embedding_provider=provider,
        build_spec=build_spec,
        write_batch_size=batch_size,
        allow_hash=True,
    )


def test_corpus_inventory_is_order_stable_and_uses_metadata_allowlist():
    first = _chunk("chunk-a", "same content", title="First title")
    with_other_path = replace(
        first,
        metadata={**first.metadata, "source_path": "D:/another/private/path.json"},
    )
    second = _chunk("chunk-b", "second content")

    left = build_corpus_inventory([first, second])
    reordered = build_corpus_inventory([second, with_other_path])
    retitled = build_corpus_inventory(
        [replace(first, metadata={**first.metadata, "title": "Changed title"}), second]
    )

    assert left.corpus_fingerprint == reordered.corpus_fingerprint
    assert left.corpus_fingerprint != retitled.corpus_fingerprint
    assert [entry.chunk_id for entry in left.entries] == ["chunk-a", "chunk-b"]
    assert set(left.entries[0].to_dict()) == {
        "chunk_id",
        "namespace",
        "source_type",
        "canonical_content_hash",
        "retrieval_metadata_fingerprint",
    }
    assert "source_path" not in left.managed_chunks[0].metadata
    assert "generated_at" not in left.managed_chunks[0].metadata


def test_cross_process_lock_has_bounded_second_acquisition(tmp_path):
    first = CrossProcessFileLock(tmp_path / "index.lock", timeout_seconds=0)
    second = CrossProcessFileLock(tmp_path / "index.lock", timeout_seconds=0)
    with first:
        with pytest.raises(RagIndexLockError) as exc_info:
            second.acquire()
    assert exc_info.value.reason == "rag_index_build_in_progress"


def test_full_generation_build_and_canonical_query_use_safe_metadata(tmp_path):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider, batch_size=1)
    chunks = [
        _chunk("lore-1", "Aemeath lore evidence with enough content for retrieval."),
        _chunk(
            "voice-1",
            "Aemeath voice evidence with enough content for retrieval.",
            namespace="canon:voice",
        ),
    ]

    result = builder.build(chunks)

    assert result.status == "ready"
    assert result.build_mode == "full"
    assert result.activated is True
    assert all(request.purpose == "document" for request in provider.requests)
    registry = ReadyGenerationRegistry(builder.config)
    resolution = registry.resolve(provider.signature)
    assert resolution.availability_reason == "available"
    assert resolution.handle is not None
    assert resolution.handle.store.count_by_namespace() == {
        "canon:lore": 1,
        "canon:voice": 1,
    }

    query_provider = RecordingProvider()
    retriever = CanonicalRagRetriever(
        config=builder.config,
        query_source=DirectQueryEmbeddingSource(query_provider),
    )
    outcome = retriever.search_with_outcome(
        query="Aemeath lore",
        namespaces=["canon:lore"],
        limit=2,
    )

    assert outcome.availability_reason == "available"
    assert len(outcome.hits) == 1
    assert query_provider.requests == [
        EmbeddingRequest(texts=("Aemeath lore",), purpose="query")
    ]
    assert set(outcome.hits[0].metadata) == {
        "generation_id",
        "retriever",
        "raw_score",
        "namespace",
        "source_type",
        "signature_fingerprint",
        "source_id",
    }
    assert "private" not in str(outcome.hits[0].metadata)

    malformed_query = ResolvedQueryEmbedding(
        signature=query_provider.signature,
        vector=np.zeros(query_provider.signature.dimension, dtype=np.float32),
    )
    malformed_outcome = retriever.search_resolved(resolved_query=malformed_query)
    assert malformed_outcome.availability_reason == "index_corrupt"


def test_incremental_build_reuses_unchanged_chunks_and_removes_stale(tmp_path):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    first_chunks = [
        _chunk("keep", "unchanged content"),
        _chunk("remove", "stale content"),
    ]
    first = builder.build(first_chunks)
    provider.requests.clear()

    second_chunks = [
        _chunk("keep", "unchanged content"),
        _chunk("new", "new content"),
    ]
    second = builder.build(second_chunks)

    assert second.generation_id != first.generation_id
    assert second.build_mode == "incremental"
    assert second.embedded_chunk_count == 1
    assert second.removed_chunk_count == 1
    assert sum(len(request.texts) for request in provider.requests) == 1
    provider.requests.clear()
    no_op = builder.build(second_chunks)
    assert no_op.no_op is True
    assert provider.requests == []


def test_parser_change_forces_full_rebuild(tmp_path):
    provider = RecordingProvider()
    chunks = [_chunk("a", "alpha"), _chunk("b", "beta")]
    first_builder = _builder(tmp_path, provider)
    first_builder.build(chunks)
    provider.requests.clear()
    changed_builder = _builder(
        tmp_path,
        provider,
        build_spec=RagBuildSpec(parser_version="wuwa_gamedata_parser_v2"),
    )

    result = changed_builder.build(chunks)

    assert result.build_mode == "full"
    assert result.embedded_chunk_count == 2
    assert sum(len(request.texts) for request in provider.requests) == 2


def test_failed_generation_preserves_old_ready_generation(tmp_path):
    initial_provider = RecordingProvider()
    initial_builder = _builder(tmp_path, initial_provider)
    initial = initial_builder.build([_chunk("old", "old ready content")])
    failing_provider = RecordingProvider(fail=True)
    failing_builder = _builder(tmp_path, failing_provider)

    with pytest.raises(EmbeddingInvalidResponseError):
        failing_builder.build([_chunk("new", "new content that fails")])

    resolution = ReadyGenerationRegistry(initial_builder.config).resolve(
        initial_provider.signature
    )
    assert resolution.handle is not None
    assert resolution.handle.manifest.generation_id == initial.generation_id
    manifests = failing_builder.repository.list_generation_manifests()
    assert manifests[-1].status == "failed"
    assert manifests[-1].failure_reason == "rag_embedding_provider_failed"


def test_interrupted_build_resumes_after_last_committed_batch(tmp_path):
    interrupting = RecordingProvider(interrupt_on_call=2)
    builder = _builder(tmp_path, interrupting, batch_size=1)
    chunks = [
        _chunk("a", "alpha"),
        _chunk("b", "beta"),
        _chunk("c", "gamma"),
    ]

    with pytest.raises(KeyboardInterrupt):
        builder.build(chunks)
    manifests = builder.repository.list_generation_manifests()
    assert manifests[-1].status == "building"
    checkpoint = builder.repository.read_checkpoint(manifests[-1].generation_id)
    assert checkpoint.next_batch_index == 1

    resumed_provider = RecordingProvider()
    resumed = _builder(tmp_path, resumed_provider, batch_size=1).build(chunks)

    assert resumed.resumed is True
    assert resumed.status == "ready"
    assert sum(len(request.texts) for request in resumed_provider.requests) == 2


def test_no_ready_generation_does_not_resolve_query_embedding(tmp_path):
    config = RagIndexConfig(index_root=tmp_path / "indexes", index_id="wuwa_canon")
    source = CountingQuerySource()
    retriever = CanonicalRagRetriever(config=config, query_source=source)

    outcome = retriever.search_with_outcome(query="unused", limit=2)

    assert outcome.availability_reason == "no_ready_generation"
    assert outcome.hits == []
    assert source.resolve_count == 0


def test_hash_build_requires_explicit_allow_flag(tmp_path):
    provider = RecordingProvider()
    config = RagIndexConfig(index_root=tmp_path / "indexes", index_id="wuwa_canon")
    builder = RagGenerationBuilder(config=config, embedding_provider=provider)

    with pytest.raises(RagIndexBuildError) as exc_info:
        builder.build([_chunk("a", "alpha")])

    assert exc_info.value.reason == "rag_hash_not_explicitly_allowed"
    assert not config.index_root.exists()


def test_active_update_preserves_other_signature_mapping(tmp_path):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    first = builder.build([_chunk("a", "alpha")])
    repository = RagIndexFileRepository(
        root=builder.config.index_root,
        index_id=builder.config.index_id,
    )
    active = repository.read_active(now=utc_now())
    first_manifest = repository.read_generation_manifest(first.generation_id)
    other_fingerprint = "sha256:" + "a" * 64
    mapping = {
        **active.active_by_signature,
        other_fingerprint: ActiveGenerationReference(
            generation_id=first.generation_id,
            manifest_fingerprint=manifest_fingerprint(first_manifest),
        ),
    }
    repository.write_active(replace(active, active_by_signature=mapping, updated_at=utc_now()))

    builder.build([_chunk("b", "beta")])

    updated = repository.read_active(now=utc_now())
    assert other_fingerprint in updated.active_by_signature


def test_unsafe_source_id_and_malformed_runtime_files_fail_safely(tmp_path):
    sanitized = sanitize_source_id("C:/private/source.json", fallback="unsafe")
    assert sanitized.startswith("chunk:")
    assert "private" not in sanitized
    with pytest.raises(RagIndexManifestError) as checkpoint_error:
        BuildCheckpoint.from_dict(
            {
                "schema_version": "rag-index-checkpoint/v1",
                "build_plan_fingerprint": "sha256:" + "a" * 64,
                "phase": "embedding",
                "next_batch_index": "1",
                "updated_at": "now",
            }
        )
    assert getattr(checkpoint_error.value, "reason", None) == "rag_checkpoint_index_invalid"

    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    built = builder.build([_chunk("a", "alpha")])
    manifest = builder.repository.read_generation_manifest(built.generation_id)
    malformed = manifest.to_dict()
    malformed["namespace_counts"] = {"canon:lore": "1"}
    with pytest.raises(RagIndexManifestError) as manifest_error:
        GenerationManifest.from_dict(malformed)
    assert getattr(manifest_error.value, "reason", None) == (
        "rag_generation_namespace_counts_invalid"
    )
    malformed_signature = manifest.to_dict()
    malformed_signature["embedding_signature_json"] = "{}"
    with pytest.raises(RagIndexManifestError) as signature_error:
        GenerationManifest.from_dict(malformed_signature)
    assert signature_error.value.reason == "rag_generation_signature_invalid"


def test_managed_index_cli_build_status_and_query(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "app.retrieval.manage_rag_index.load_wuwa_gamedata_chunks",
        lambda _source_dir: [_chunk("cli-lore", "Aemeath canonical CLI evidence")],
    )
    common = ["--index-root", str(tmp_path / "indexes"), "--index-id", "wuwa_canon"]
    provider_args = ["--profile", "deterministic_hash_v1", "--allow-hash"]

    assert rag_index_main([*common, "build", *provider_args]) == 0
    build_output = capsys.readouterr().out
    assert "status=ready" in build_output
    assert "activated=True" in build_output

    assert rag_index_main([*common, "status"]) == 0
    status_output = capsys.readouterr().out
    assert "status=ready" in status_output
    assert "active sha256:" in status_output

    assert rag_index_main([*common, "query", *provider_args, "Aemeath canonical"]) == 0
    query_output = capsys.readouterr().out
    assert "availability=available" in query_output
    assert "Aemeath canonical CLI evidence" in query_output


def test_managed_index_cli_does_not_implicitly_allow_hash(tmp_path, capsys):
    result = rag_index_main(
        [
            "--index-root",
            str(tmp_path / "indexes"),
            "query",
            "--profile",
            "deterministic_hash_v1",
            "test query",
        ]
    )

    assert result == 2
    assert "hash_provider_not_explicitly_allowed" in capsys.readouterr().err


def test_corrupt_ready_generation_is_not_reused_or_reactivated(tmp_path):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    chunks = [_chunk("ready-corrupt", "canonical content that must stay covered")]
    first = builder.build(chunks)
    database_path = builder.repository.database_path(first.generation_id)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM chunk_embedding_artifacts WHERE chunk_id = ?",
            (chunks[0].id,),
        )

    broken_resolution = ReadyGenerationRegistry(builder.config).resolve(provider.signature)
    assert broken_resolution.handle is None
    assert broken_resolution.availability_reason == "index_corrupt"

    provider.requests.clear()
    rebuilt = builder.build(chunks)

    assert rebuilt.generation_id != first.generation_id
    assert rebuilt.build_mode == "full"
    assert rebuilt.resumed is False
    assert rebuilt.activated is True
    assert sum(len(request.texts) for request in provider.requests) == 1
    ready_resolution = ReadyGenerationRegistry(builder.config).resolve(provider.signature)
    assert ready_resolution.availability_reason == "available"
    assert ready_resolution.handle is not None
    assert ready_resolution.handle.manifest.generation_id == rebuilt.generation_id


def test_incomplete_generation_directory_is_isolated_and_number_is_not_reused(
    tmp_path,
    capsys,
):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    builder.repository.ensure_layout()
    orphan_id = "g000041-deadbeefcafe"
    builder.repository.generation_dir(orphan_id).mkdir()

    result = builder.build([_chunk("valid", "valid generation content")])

    assert result.generation_id.startswith("g000042-")
    scan = builder.repository.scan_generation_manifests()
    assert [manifest.generation_id for manifest in scan.manifests] == [result.generation_id]
    assert [(item.generation_id, item.reason) for item in scan.diagnostics] == [
        (orphan_id, "rag_index_json_read_failed")
    ]
    assert rag_index_main(
        ["--index-root", str(builder.config.index_root), "status"]
    ) == 0
    output = capsys.readouterr().out
    assert f"diagnostic generation={orphan_id}" in output
    assert "reason=rag_index_json_read_failed" in output


def test_changed_write_batch_size_starts_new_generation_instead_of_resuming(tmp_path):
    chunks = [
        _chunk("batch-a", "alpha"),
        _chunk("batch-b", "beta"),
        _chunk("batch-c", "gamma"),
    ]
    interrupting = RecordingProvider(interrupt_on_call=2)
    first_builder = _builder(tmp_path, interrupting, batch_size=1)
    with pytest.raises(KeyboardInterrupt):
        first_builder.build(chunks)
    interrupted = first_builder.repository.list_generation_manifests()[-1]
    assert interrupted.status == "building"

    replacement_provider = RecordingProvider()
    replacement = _builder(tmp_path, replacement_provider, batch_size=2).build(chunks)

    assert replacement.generation_id != interrupted.generation_id
    assert replacement.resumed is False
    assert replacement.build_mode == "full"
    assert replacement.status == "ready"
    assert sum(len(request.texts) for request in replacement_provider.requests) == 3


def test_staging_resume_requires_inventory_before_ready(tmp_path, monkeypatch):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    chunks = [_chunk("staging", "staging inventory must remain authoritative")]

    def interrupt_after_staging(_manifest, _inventory):
        raise KeyboardInterrupt("stop before ready validation")

    monkeypatch.setattr(builder, "_validate_and_mark_ready", interrupt_after_staging)
    with pytest.raises(KeyboardInterrupt):
        builder.build(chunks)
    staging = builder.repository.list_generation_manifests()[-1]
    assert staging.status == "staging"
    builder.repository.inventory_path(staging.generation_id).unlink()
    damaged_scan = builder.repository.scan_generation_manifests()
    assert [(item.generation_id, item.reason) for item in damaged_scan.diagnostics] == [
        (staging.generation_id, "rag_inventory_read_failed")
    ]

    resumed_builder = _builder(tmp_path, RecordingProvider())
    rebuilt = resumed_builder.build(chunks)

    assert rebuilt.generation_id != staging.generation_id
    assert rebuilt.status == "ready"
    assert rebuilt.resumed is False
    failed = resumed_builder.repository.read_generation_manifest(staging.generation_id)
    assert failed.status == "failed"


def test_active_reopen_verification_failure_restores_previous_bytes(
    tmp_path,
    monkeypatch,
):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    builder.build([_chunk("active", "active rollback content")])
    repository = builder.repository
    active = repository.read_active(now=utc_now())
    previous_bytes = repository.active_path.read_bytes()

    def fail_reopen(_path):
        return {"schema_version": "invalid"}

    monkeypatch.setattr(repository, "_read_json", fail_reopen)
    with pytest.raises(RagIndexManifestError) as exc_info:
        repository.write_active(replace(active, updated_at="2099-01-01T00:00:00+00:00"))

    assert exc_info.value.reason == "rag_active_reopen_verify_failed"
    assert repository.active_path.read_bytes() == previous_bytes


def test_canonical_retriever_rejects_late_artifact_diagnostics(tmp_path):
    provider = RecordingProvider()
    builder = _builder(tmp_path, provider)
    chunk = _chunk("late-diagnostic", "late artifact diagnostic content")
    builder.build([chunk])
    real_registry = ReadyGenerationRegistry(builder.config)

    class CorruptAfterValidationRegistry:
        def resolve(self, signature):
            resolution = real_registry.resolve(signature)
            assert resolution.handle is not None
            legacy_vector = provider.delegate.embed(
                EmbeddingRequest(texts=(chunk.content,), purpose="document")
            ).vectors[0]
            SqliteVectorStore(resolution.handle.database_path).upsert_chunks(
                chunks=[chunk],
                embeddings=[legacy_vector],
                embedding_model="BAAI/bge-m3",
            )
            return resolution

    query_provider = RecordingProvider()
    retriever = CanonicalRagRetriever(
        config=builder.config,
        query_source=DirectQueryEmbeddingSource(query_provider),
        registry=CorruptAfterValidationRegistry(),
    )

    outcome = retriever.search_with_outcome(query="late artifact", limit=2)

    assert outcome.availability_reason == "index_corrupt"
    assert outcome.hits == []
    assert outcome.diagnostics is not None
    assert [issue.reason for issue in outcome.diagnostics.issues] == ["legacy_unverified"]


def test_rag_runtime_closes_bundle_on_success():
    closed = []
    runtime = EmbeddingProviderRuntime(
        provider=object(),  # type: ignore[arg-type]
        _close=lambda: closed.append(True),
    )
    assert _with_provider_runtime(runtime, lambda provider: provider) is runtime.provider
    assert closed == [True]


def test_rag_runtime_close_failure_does_not_replace_primary_error():
    def close():
        raise RuntimeError("close failure secret")

    runtime = EmbeddingProviderRuntime(provider=object(), _close=close)  # type: ignore[arg-type]
    primary = RagIndexBuildError("primary_failure")
    with pytest.raises(RagIndexBuildError) as raised:
        _with_provider_runtime(runtime, lambda _provider: (_ for _ in ()).throw(primary))
    assert raised.value is primary
