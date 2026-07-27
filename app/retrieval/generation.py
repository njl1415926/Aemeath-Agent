from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Mapping, Sequence

from app.embeddings.artifacts import (
    EmbeddingArtifactWrite,
    canonicalize_embedding_text,
    embedding_content_hash,
)
from app.embeddings.config import load_embedding_space_profile
from app.embeddings.errors import EmbeddingError
from app.embeddings.ports import EmbeddingProviderPort
from app.embeddings.schemas import EmbeddingModelSignature, EmbeddingRequest
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort
from app.observability.schemas import AppLogRecord
from app.retrieval.documents import ChunkRecord
from app.retrieval.errors import (
    RagGenerationStoreError,
    RagIndexBuildError,
    RagIndexConfigError,
    RagIndexError,
    RagIndexManifestError,
)
from app.retrieval.index_lock import CrossProcessFileLock
from app.retrieval.index_manifest import (
    DEFAULT_RAG_INDEX_ID,
    DEFAULT_RAG_INDEX_ROOT,
    INDEX_FORMAT_VERSION,
    INDEX_MANIFEST_SCHEMA_VERSION,
    INVENTORY_SCHEMA_VERSION,
    RETRIEVAL_METADATA_SCHEMA_VERSION,
    ActiveGenerationReference,
    BuildCheckpoint,
    CanonicalInventoryEntry,
    CorpusInventory,
    GenerationManifest,
    RagIndexFileRepository,
    build_corpus_inventory,
    build_plan_fingerprint,
    config_fingerprint,
    corpus_fingerprint_for_entries,
    manifest_fingerprint,
    namespace_counts,
    utc_now,
    validate_index_id,
)
from app.retrieval.vector_index import RagChunkExpectation, SqliteVectorStore

BuildMode = Literal["full", "incremental"]
AvailabilityReason = Literal[
    "available",
    "no_ready_generation",
    "manifest_invalid",
    "signature_mismatch",
    "profile_not_approved",
    "index_unavailable",
    "index_corrupt",
]


@dataclass(frozen=True)
class RagIndexConfig:
    index_root: Path = DEFAULT_RAG_INDEX_ROOT
    index_id: str = DEFAULT_RAG_INDEX_ID

    def __post_init__(self) -> None:
        validate_index_id(self.index_id)
        if not isinstance(self.index_root, Path):
            raise RagIndexConfigError("rag_index_root_invalid")

    @classmethod
    def from_environment(
        cls,
        *,
        index_id: str = DEFAULT_RAG_INDEX_ID,
    ) -> RagIndexConfig:
        configured = os.getenv("AEMEATH_RAG_INDEX_ROOT")
        if configured is not None and not configured.strip():
            raise RagIndexConfigError("rag_index_root_invalid")
        return cls(
            index_root=Path(configured) if configured is not None else DEFAULT_RAG_INDEX_ROOT,
            index_id=index_id,
        )


@dataclass(frozen=True)
class RagBuildSpec:
    parser_version: str = "wuwa_gamedata_parser_v1"
    chunker_version: str = "wuwa_gamedata_chunker_v1"
    chunk_id_scheme_version: str = "wuwa_chunk_id_v1"
    namespace_policy_version: str = "wuwa_namespace_v1"
    parser_config: Mapping[str, object] = field(default_factory=dict)
    chunker_config: Mapping[str, object] = field(default_factory=dict)

    def validate(self) -> None:
        versions = (
            self.parser_version,
            self.chunker_version,
            self.chunk_id_scheme_version,
            self.namespace_policy_version,
        )
        if any(not isinstance(item, str) or not item.strip() for item in versions):
            raise RagIndexConfigError("rag_build_version_invalid")


@dataclass(frozen=True)
class RagBuildResult:
    generation_id: str
    status: str
    build_mode: BuildMode
    activated: bool
    resumed: bool
    no_op: bool
    embedded_chunk_count: int
    removed_chunk_count: int


@dataclass(frozen=True)
class ReadyGenerationHandle:
    manifest: GenerationManifest
    database_path: Path
    store: SqliteVectorStore


@dataclass(frozen=True)
class ReadyGenerationResolution:
    handle: ReadyGenerationHandle | None
    availability_reason: AvailabilityReason


class ReadyGenerationValidator:
    def __init__(self, repository: RagIndexFileRepository) -> None:
        self.repository = repository

    def validate_inventory(
        self,
        manifest: GenerationManifest,
        *,
        expected_inventory: Sequence[CanonicalInventoryEntry] | None = None,
    ) -> tuple[CanonicalInventoryEntry, ...]:
        inventory = self.repository.read_inventory(manifest.generation_id)
        if corpus_fingerprint_for_entries(inventory) != manifest.corpus_fingerprint:
            raise RagIndexBuildError("rag_generation_inventory_mismatch")
        if len(inventory) != manifest.chunk_count:
            raise RagIndexBuildError("rag_generation_inventory_mismatch")
        counts: dict[str, int] = {}
        for entry in inventory:
            counts[entry.namespace] = counts.get(entry.namespace, 0) + 1
        if dict(sorted(counts.items())) != manifest.namespace_counts:
            raise RagIndexBuildError("rag_generation_inventory_mismatch")
        if expected_inventory is not None and tuple(expected_inventory) != inventory:
            raise RagIndexBuildError("rag_generation_inventory_mismatch")
        return inventory

    def validate_ready_generation(
        self,
        manifest: GenerationManifest,
        *,
        expected_inventory: Sequence[CanonicalInventoryEntry] | None = None,
    ) -> ReadyGenerationHandle:
        if manifest.status != "ready":
            raise RagIndexBuildError("rag_generation_not_ready")
        inventory = self.validate_inventory(
            manifest,
            expected_inventory=expected_inventory,
        )
        database_path = self.repository.database_path(manifest.generation_id)
        wal_path = Path(f"{database_path}-wal")
        if wal_path.exists() and wal_path.stat().st_size > 0:
            raise RagIndexBuildError("rag_generation_wal_not_empty")
        store = SqliteVectorStore.open_read_only(database_path)
        self._validate_sqlite_integrity(database_path)
        coverage = store.inspect_exact_signature_coverage(
            signature=manifest.signature,
            expected_chunks=[
                RagChunkExpectation(
                    chunk_id=entry.chunk_id,
                    namespace=entry.namespace,
                    source_type=entry.source_type,
                    content_hash=entry.canonical_content_hash,
                )
                for entry in inventory
            ],
        )
        if not coverage.ready_eligible or coverage.diagnostics.issues:
            raise RagIndexBuildError("rag_generation_coverage_incomplete")
        return ReadyGenerationHandle(
            manifest=manifest,
            database_path=database_path,
            store=store,
        )

    def _validate_sqlite_integrity(self, path: Path) -> None:
        try:
            database_uri = f"{path.absolute().as_uri()}?mode=ro"
            connection = sqlite3.connect(database_uri, uri=True)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise RagIndexBuildError("rag_generation_sqlite_validation_failed") from exc
        if integrity != [("ok",)]:
            raise RagIndexBuildError("rag_generation_integrity_failed")


class ReadyGenerationRegistry:
    def __init__(self, config: RagIndexConfig) -> None:
        self.config = config
        self.repository = RagIndexFileRepository(
            root=config.index_root,
            index_id=config.index_id,
        )
        self.validator = ReadyGenerationValidator(self.repository)

    def resolve(self, signature: EmbeddingModelSignature) -> ReadyGenerationResolution:
        try:
            approved = load_embedding_space_profile(signature.profile_id)
        except EmbeddingError:
            return ReadyGenerationResolution(None, "profile_not_approved")
        if approved.signature.canonical() != signature.canonical():
            return ReadyGenerationResolution(None, "signature_mismatch")
        try:
            active = self.repository.read_active(now=utc_now())
            reference = active.active_by_signature.get(signature.fingerprint())
            if reference is None:
                return ReadyGenerationResolution(None, "no_ready_generation")
            manifest = self.repository.read_generation_manifest(reference.generation_id)
            if manifest.status != "ready":
                return ReadyGenerationResolution(None, "no_ready_generation")
            if (
                manifest.embedding_signature_json != signature.canonical()
                or manifest.embedding_signature_fingerprint != signature.fingerprint()
            ):
                return ReadyGenerationResolution(None, "signature_mismatch")
            if manifest_fingerprint(manifest) != reference.manifest_fingerprint:
                return ReadyGenerationResolution(None, "manifest_invalid")
            handle = self.validator.validate_ready_generation(manifest)
            return ReadyGenerationResolution(
                handle,
                "available",
            )
        except RagIndexManifestError:
            return ReadyGenerationResolution(None, "manifest_invalid")
        except RagGenerationStoreError:
            return ReadyGenerationResolution(None, "index_unavailable")
        except RagIndexError:
            return ReadyGenerationResolution(None, "index_corrupt")
        except (OSError, sqlite3.Error):
            return ReadyGenerationResolution(None, "index_corrupt")


class RagGenerationBuilder:
    def __init__(
        self,
        *,
        config: RagIndexConfig,
        embedding_provider: EmbeddingProviderPort,
        build_spec: RagBuildSpec | None = None,
        write_batch_size: int = 64,
        lock_timeout_seconds: float = 5.0,
        allow_hash: bool = False,
        app_logger: AppLoggerPort | None = None,
    ) -> None:
        if (
            isinstance(write_batch_size, bool)
            or not isinstance(write_batch_size, int)
            or write_batch_size <= 0
        ):
            raise RagIndexConfigError("rag_write_batch_size_invalid")
        self.config = config
        self.embedding_provider = embedding_provider
        self.build_spec = build_spec or RagBuildSpec()
        self.build_spec.validate()
        self.write_batch_size = write_batch_size
        self.lock_timeout_seconds = lock_timeout_seconds
        self.allow_hash = allow_hash
        self.app_logger = app_logger or NoopAppLogger()
        self.repository = RagIndexFileRepository(
            root=config.index_root,
            index_id=config.index_id,
        )
        self.ready_validator = ReadyGenerationValidator(self.repository)

    def build(
        self,
        chunks: Sequence[ChunkRecord],
        *,
        activate: bool = True,
    ) -> RagBuildResult:
        signature = self._validated_provider_signature()
        inventory = build_corpus_inventory(chunks)
        plan_fingerprint = self._plan_fingerprint(signature, inventory)
        self.repository.ensure_layout()
        lock = CrossProcessFileLock(
            self.repository.lock_path,
            timeout_seconds=self.lock_timeout_seconds,
        )
        with lock:
            return self._build_locked(
                signature=signature,
                inventory=inventory,
                plan_fingerprint=plan_fingerprint,
                activate=activate,
            )

    def _build_locked(
        self,
        *,
        signature: EmbeddingModelSignature,
        inventory: CorpusInventory,
        plan_fingerprint: str,
        activate: bool,
    ) -> RagBuildResult:
        active_manifest = self._active_manifest(signature)
        if active_manifest is not None:
            target_matches = self._manifest_matches_target(
                active_manifest,
                inventory=inventory,
                plan_fingerprint=plan_fingerprint,
            )
            try:
                self.ready_validator.validate_ready_generation(
                    active_manifest,
                    expected_inventory=inventory.entries if target_matches else None,
                )
            except (OSError, sqlite3.Error, RagGenerationStoreError, RagIndexError) as exc:
                self._log_invalid_generation(active_manifest.generation_id, exc)
                active_manifest = None
            else:
                if target_matches:
                    return RagBuildResult(
                        generation_id=active_manifest.generation_id,
                        status="ready",
                        build_mode=active_manifest.build_mode,  # type: ignore[arg-type]
                        activated=True,
                        resumed=False,
                        no_op=True,
                        embedded_chunk_count=0,
                        removed_chunk_count=0,
                    )

        reusable = self._find_existing_plan(
            plan_fingerprint,
            expected_inventory=inventory.entries,
        )
        if reusable is not None and reusable.status == "ready":
            activated = self._activate(reusable) if activate else False
            return RagBuildResult(
                generation_id=reusable.generation_id,
                status="ready",
                build_mode=reusable.build_mode,  # type: ignore[arg-type]
                activated=activated,
                resumed=True,
                no_op=False,
                embedded_chunk_count=0,
                removed_chunk_count=0,
            )

        base_manifest, base_entries = self._incremental_base(
            signature=signature,
            active_manifest=active_manifest,
        )
        manifest = reusable or self._create_generation(
            signature=signature,
            inventory=inventory,
            plan_fingerprint=plan_fingerprint,
            base_manifest=base_manifest,
        )
        resumed = reusable is not None
        try:
            result = self._populate_and_stage(
                manifest=manifest,
                inventory=inventory,
                base_entries=base_entries if manifest.build_mode == "incremental" else (),
            )
            ready = self._validate_and_mark_ready(result.manifest, inventory)
        except Exception as exc:
            self._mark_failed(manifest.generation_id, exc)
            self._log(
                "rag.index_build_failed",
                level="error",
                generation_id=manifest.generation_id,
                signature_fingerprint=signature.fingerprint(),
                error_code=self._safe_reason(exc),
            )
            raise

        activated = False
        if activate:
            activated = self._activate(ready)
        self._log(
            "rag.index_build_completed",
            generation_id=ready.generation_id,
            signature_fingerprint=signature.fingerprint(),
            build_mode=ready.build_mode,
            chunk_count=ready.chunk_count,
            activated=activated,
        )
        return RagBuildResult(
            generation_id=ready.generation_id,
            status="ready",
            build_mode=ready.build_mode,  # type: ignore[arg-type]
            activated=activated,
            resumed=resumed,
            no_op=False,
            embedded_chunk_count=result.embedded_chunk_count,
            removed_chunk_count=result.removed_chunk_count,
        )

    def _validated_provider_signature(self) -> EmbeddingModelSignature:
        signature = self.embedding_provider.signature
        try:
            approved = load_embedding_space_profile(signature.profile_id)
        except EmbeddingError as exc:
            raise RagIndexBuildError("rag_embedding_profile_not_approved") from exc
        if approved.signature.canonical() != signature.canonical():
            raise RagIndexBuildError("rag_embedding_signature_not_canonical")
        if signature.provider == "deterministic_hash" and not self.allow_hash:
            raise RagIndexBuildError("rag_hash_not_explicitly_allowed")
        return signature

    def _plan_fingerprint(
        self,
        signature: EmbeddingModelSignature,
        inventory: CorpusInventory,
    ) -> str:
        return build_plan_fingerprint(
            signature_fingerprint=signature.fingerprint(),
            corpus_fingerprint=inventory.corpus_fingerprint,
            parser_version=self.build_spec.parser_version,
            parser_config_fingerprint=config_fingerprint(self.build_spec.parser_config),
            chunker_version=self.build_spec.chunker_version,
            chunker_config_fingerprint=config_fingerprint(self.build_spec.chunker_config),
            chunk_id_scheme_version=self.build_spec.chunk_id_scheme_version,
            namespace_policy_version=self.build_spec.namespace_policy_version,
            write_batch_size=self.write_batch_size,
        )

    def _active_manifest(
        self,
        signature: EmbeddingModelSignature,
    ) -> GenerationManifest | None:
        active = self.repository.read_active(now=utc_now())
        reference = active.active_by_signature.get(signature.fingerprint())
        if reference is None:
            return None
        try:
            manifest = self.repository.read_generation_manifest(reference.generation_id)
        except RagIndexManifestError as exc:
            self._log_invalid_generation(reference.generation_id, exc)
            return None
        if (
            manifest.status != "ready"
            or manifest_fingerprint(manifest) != reference.manifest_fingerprint
        ):
            self._log_invalid_generation(
                reference.generation_id,
                RagIndexBuildError("rag_active_generation_invalid"),
            )
            return None
        if manifest.embedding_signature_json != signature.canonical():
            self._log_invalid_generation(
                reference.generation_id,
                RagIndexBuildError("rag_active_signature_mismatch"),
            )
            return None
        return manifest

    def _find_existing_plan(
        self,
        plan_fingerprint: str,
        *,
        expected_inventory: Sequence[CanonicalInventoryEntry],
    ) -> GenerationManifest | None:
        scan = self.repository.scan_generation_manifests()
        for diagnostic in scan.diagnostics:
            self._log(
                "rag.generation_isolated",
                level="warning",
                generation_id=diagnostic.generation_id,
                error_code=diagnostic.reason,
            )
        candidates = [
            manifest
            for manifest in scan.manifests
            if manifest.build_plan_fingerprint == plan_fingerprint
            and manifest.status in {"building", "staging", "ready"}
        ]
        for manifest in reversed(candidates):
            try:
                if manifest.status == "ready":
                    self.ready_validator.validate_ready_generation(
                        manifest,
                        expected_inventory=expected_inventory,
                    )
                else:
                    self.ready_validator.validate_inventory(
                        manifest,
                        expected_inventory=expected_inventory,
                    )
            except (OSError, sqlite3.Error, RagGenerationStoreError, RagIndexError) as exc:
                self._log_invalid_generation(manifest.generation_id, exc)
                self._mark_failed(manifest.generation_id, exc)
                continue
            return manifest
        return None

    def _incremental_base(
        self,
        *,
        signature: EmbeddingModelSignature,
        active_manifest: GenerationManifest | None,
    ) -> tuple[GenerationManifest | None, tuple[CanonicalInventoryEntry, ...]]:
        if active_manifest is None:
            return None, ()
        compatible = (
            active_manifest.embedding_signature_json == signature.canonical()
            and active_manifest.parser_version == self.build_spec.parser_version
            and active_manifest.parser_config_fingerprint
            == config_fingerprint(self.build_spec.parser_config)
            and active_manifest.chunker_version == self.build_spec.chunker_version
            and active_manifest.chunker_config_fingerprint
            == config_fingerprint(self.build_spec.chunker_config)
            and active_manifest.chunk_id_scheme_version
            == self.build_spec.chunk_id_scheme_version
            and active_manifest.namespace_policy_version
            == self.build_spec.namespace_policy_version
            and active_manifest.index_format_version == INDEX_FORMAT_VERSION
        )
        if not compatible:
            return None, ()
        try:
            entries = self.repository.read_inventory(active_manifest.generation_id)
        except RagIndexManifestError:
            return None, ()
        if corpus_fingerprint_for_entries(entries) != active_manifest.corpus_fingerprint:
            return None, ()
        return active_manifest, entries

    def _create_generation(
        self,
        *,
        signature: EmbeddingModelSignature,
        inventory: CorpusInventory,
        plan_fingerprint: str,
        base_manifest: GenerationManifest | None,
    ) -> GenerationManifest:
        number = self.repository.next_generation_number()
        generation_id = f"g{number:06d}-{uuid.uuid4().hex[:12]}"
        now = utc_now()
        source_ids = {
            str(chunk.metadata["source_id"])
            for chunk in inventory.managed_chunks
            if "source_id" in chunk.metadata
        }
        manifest = GenerationManifest(
            schema_version=INDEX_MANIFEST_SCHEMA_VERSION,
            index_format_version=INDEX_FORMAT_VERSION,
            index_id=self.config.index_id,
            generation_id=generation_id,
            generation_number=number,
            status="building",
            embedding_signature_json=signature.canonical(),
            embedding_signature_fingerprint=signature.fingerprint(),
            embedding_purpose="document",
            corpus_fingerprint=inventory.corpus_fingerprint,
            inventory_schema_version=INVENTORY_SCHEMA_VERSION,
            retrieval_metadata_schema_version=RETRIEVAL_METADATA_SCHEMA_VERSION,
            parser_version=self.build_spec.parser_version,
            parser_config_fingerprint=config_fingerprint(self.build_spec.parser_config),
            chunker_version=self.build_spec.chunker_version,
            chunker_config_fingerprint=config_fingerprint(self.build_spec.chunker_config),
            chunk_id_scheme_version=self.build_spec.chunk_id_scheme_version,
            namespace_policy_version=self.build_spec.namespace_policy_version,
            chunk_count=len(inventory.entries),
            document_count=len(source_ids),
            namespace_counts=namespace_counts(inventory.managed_chunks),
            build_plan_fingerprint=plan_fingerprint,
            build_mode="incremental" if base_manifest is not None else "full",
            base_generation_id=base_manifest.generation_id if base_manifest is not None else None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            failure_reason=None,
        )
        self.repository.write_generation_manifest(manifest)
        self.repository.write_inventory(generation_id, inventory.entries)
        self.repository.write_checkpoint(
            generation_id,
            BuildCheckpoint(
                schema_version="rag-index-checkpoint/v1",
                build_plan_fingerprint=plan_fingerprint,
                phase="embedding",
                next_batch_index=0,
                updated_at=now,
            ),
        )
        self._log(
            "rag.index_build_started",
            generation_id=generation_id,
            signature_fingerprint=signature.fingerprint(),
            build_mode=manifest.build_mode,
            chunk_count=manifest.chunk_count,
        )
        return manifest

    @dataclass(frozen=True)
    class _PopulationResult:
        manifest: GenerationManifest
        embedded_chunk_count: int
        removed_chunk_count: int

    def _populate_and_stage(
        self,
        *,
        manifest: GenerationManifest,
        inventory: CorpusInventory,
        base_entries: Sequence[CanonicalInventoryEntry],
    ) -> _PopulationResult:
        self.ready_validator.validate_inventory(
            manifest,
            expected_inventory=inventory.entries,
        )
        if manifest.status == "staging":
            return self._PopulationResult(manifest, 0, 0)
        if manifest.status != "building":
            raise RagIndexBuildError("rag_generation_not_resumable")

        database_path = self.repository.database_path(manifest.generation_id)
        if not database_path.exists():
            if manifest.build_mode == "incremental":
                if manifest.base_generation_id is None:
                    raise RagIndexBuildError("rag_incremental_base_missing")
                base_store = SqliteVectorStore.open_read_only(
                    self.repository.database_path(manifest.base_generation_id)
                )
                base_store.backup_to(database_path)
            else:
                SqliteVectorStore(database_path)
            checkpoint = BuildCheckpoint(
                schema_version="rag-index-checkpoint/v1",
                build_plan_fingerprint=manifest.build_plan_fingerprint,
                phase="embedding",
                next_batch_index=0,
                updated_at=utc_now(),
            )
            self.repository.write_checkpoint(manifest.generation_id, checkpoint)
        else:
            checkpoint = self.repository.read_checkpoint(manifest.generation_id)
            if checkpoint.build_plan_fingerprint != manifest.build_plan_fingerprint:
                raise RagIndexBuildError("rag_generation_checkpoint_mismatch")

        target_by_id = {
            entry.chunk_id: (entry, chunk)
            for entry, chunk in zip(inventory.entries, inventory.managed_chunks)
        }
        base_by_id = {entry.chunk_id: entry for entry in base_entries}
        if manifest.build_mode == "incremental":
            work_chunks = [
                chunk
                for chunk_id, (entry, chunk) in target_by_id.items()
                if base_by_id.get(chunk_id) != entry
            ]
            stale_ids = sorted(set(base_by_id) - set(target_by_id))
        else:
            work_chunks = [item[1] for item in target_by_id.values()]
            stale_ids = []

        store = SqliteVectorStore(database_path)
        embedded_count = 0
        if checkpoint.phase == "embedding":
            batches = list(self._batches(work_chunks))
            if checkpoint.next_batch_index > len(batches):
                raise RagIndexBuildError("rag_generation_checkpoint_index_invalid")
            for batch_index in range(checkpoint.next_batch_index, len(batches)):
                batch = batches[batch_index]
                self._embed_batch(store, manifest, batch)
                embedded_count += len(batch)
                self.repository.write_checkpoint(
                    manifest.generation_id,
                    replace(
                        checkpoint,
                        next_batch_index=batch_index + 1,
                        updated_at=utc_now(),
                    ),
                )
            checkpoint = replace(
                checkpoint,
                phase="stale_cleanup",
                next_batch_index=len(batches),
                updated_at=utc_now(),
            )
            self.repository.write_checkpoint(manifest.generation_id, checkpoint)

        removed_count = 0
        if checkpoint.phase == "stale_cleanup":
            deletion = store.delete_chunks(chunk_ids=stale_ids)
            removed_count = deletion.deleted_chunk_count
            checkpoint = replace(checkpoint, phase="validation", updated_at=utc_now())
            self.repository.write_checkpoint(manifest.generation_id, checkpoint)

        staging = manifest.with_status("staging", now=utc_now())
        self.repository.write_generation_manifest(staging)
        return self._PopulationResult(staging, embedded_count, removed_count)

    def _batches(self, chunks: Sequence[ChunkRecord]) -> list[list[ChunkRecord]]:
        return [
            list(chunks[start : start + self.write_batch_size])
            for start in range(0, len(chunks), self.write_batch_size)
        ]

    def _embed_batch(
        self,
        store: SqliteVectorStore,
        manifest: GenerationManifest,
        chunks: Sequence[ChunkRecord],
    ) -> None:
        canonical_texts = tuple(canonicalize_embedding_text(chunk.content) for chunk in chunks)
        result = self.embedding_provider.embed(
            EmbeddingRequest(texts=canonical_texts, purpose="document")
        )
        if result.signature.canonical() != manifest.embedding_signature_json:
            raise RagIndexBuildError("rag_embedding_result_signature_mismatch")
        if result.vectors.shape[0] != len(chunks):
            raise RagIndexBuildError("rag_embedding_result_count_mismatch")
        artifacts = [
            EmbeddingArtifactWrite(
                signature=result.signature,
                content_hash=embedding_content_hash(text),
                vector=result.vectors[index],
            )
            for index, text in enumerate(canonical_texts)
        ]
        store.upsert_chunk_embedding_artifacts(chunks=list(chunks), artifacts=artifacts)

    def _validate_and_mark_ready(
        self,
        manifest: GenerationManifest,
        inventory: CorpusInventory,
    ) -> GenerationManifest:
        if manifest.status != "staging":
            raise RagIndexBuildError("rag_generation_not_staging")
        database_path = self.repository.database_path(manifest.generation_id)
        self._seal_sqlite(database_path)
        store = SqliteVectorStore.open_read_only(database_path)
        coverage = store.inspect_exact_signature_coverage(
            signature=manifest.signature,
            expected_chunks=[
                RagChunkExpectation.from_chunk(chunk) for chunk in inventory.managed_chunks
            ],
        )
        if not coverage.ready_eligible:
            raise RagIndexBuildError("rag_generation_coverage_incomplete")
        ready = manifest.with_status("ready", now=utc_now())
        self.repository.write_generation_manifest(ready)
        self.ready_validator.validate_ready_generation(
            ready,
            expected_inventory=inventory.entries,
        )
        return ready

    def _seal_sqlite(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise RagIndexBuildError("rag_generation_wal_checkpoint_busy")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise RagIndexBuildError("rag_generation_integrity_failed")
        except sqlite3.Error as exc:
            raise RagIndexBuildError("rag_generation_sqlite_validation_failed") from exc
        finally:
            connection.close()
        wal_path = Path(f"{path}-wal")
        if wal_path.exists() and wal_path.stat().st_size > 0:
            raise RagIndexBuildError("rag_generation_wal_not_empty")

    def _activate(self, manifest: GenerationManifest) -> bool:
        self.ready_validator.validate_ready_generation(manifest)
        now = utc_now()
        active = self.repository.read_active(now=now)
        mapping = dict(active.active_by_signature)
        mapping[manifest.embedding_signature_fingerprint] = ActiveGenerationReference(
            generation_id=manifest.generation_id,
            manifest_fingerprint=manifest_fingerprint(manifest),
        )
        updated = replace(active, active_by_signature=mapping, updated_at=now)
        self.repository.write_active(updated)
        return True

    def _log_invalid_generation(self, generation_id: str, exc: Exception) -> None:
        self._log(
            "rag.generation_validation_failed",
            level="warning",
            generation_id=generation_id,
            error_code=self._safe_reason(exc),
        )

    def _mark_failed(self, generation_id: str, exc: Exception) -> None:
        try:
            current = self.repository.read_generation_manifest(generation_id)
            if current.status not in {"building", "staging"}:
                return
            failed = current.with_status(
                "failed",
                now=utc_now(),
                failure_reason=self._safe_reason(exc),
            )
            self.repository.write_generation_manifest(failed)
        except Exception:
            return

    def _manifest_matches_target(
        self,
        manifest: GenerationManifest,
        *,
        inventory: CorpusInventory,
        plan_fingerprint: str,
    ) -> bool:
        return (
            manifest.status == "ready"
            and manifest.corpus_fingerprint == inventory.corpus_fingerprint
            and manifest.build_plan_fingerprint == plan_fingerprint
        )

    def _safe_reason(self, exc: Exception) -> str:
        if isinstance(exc, RagIndexError):
            return exc.reason
        if isinstance(exc, RagGenerationStoreError):
            return exc.reason
        if isinstance(exc, EmbeddingError):
            return "rag_embedding_provider_failed"
        return "rag_generation_build_failed"

    def _log(self, event_name: str, *, level: str = "info", **payload: object) -> None:
        try:
            self.app_logger.record(
                AppLogRecord(
                    event_name=event_name,
                    level=level,  # type: ignore[arg-type]
                    payload={
                        "index_id": self.config.index_id,
                        **payload,
                    },
                )
            )
        except Exception:
            return
