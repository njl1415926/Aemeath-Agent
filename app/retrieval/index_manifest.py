from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence

from app.embeddings.artifacts import (
    EmbeddingArtifactValidationError,
    canonical_content_hash,
    deserialize_signature,
)
from app.embeddings.schemas import EmbeddingModelSignature
from app.retrieval.documents import ChunkRecord
from app.retrieval.errors import RagIndexConfigError, RagIndexError, RagIndexManifestError

INDEX_MANIFEST_SCHEMA_VERSION = "rag-index-generation/v1"
ACTIVE_MANIFEST_SCHEMA_VERSION = "rag-index-active/v1"
INVENTORY_SCHEMA_VERSION = "rag-corpus-inventory/v1"
RETRIEVAL_METADATA_SCHEMA_VERSION = "rag-retrieval-metadata/v1"
INDEX_FORMAT_VERSION = "sqlite-canonical-artifact/v1"
DEFAULT_RAG_INDEX_ROOT = Path("data/rag_indexes")
DEFAULT_RAG_INDEX_ID = "wuwa_canon"

_INDEX_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_GENERATION_ID_RE = re.compile(r"^g[0-9]{6}-[a-f0-9]{12}$")
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_METADATA_KEYS = (
    "title",
    "section_title",
    "record_id",
    "scene_id",
    "state_hash",
    "chunk_index",
    "source_order_start",
    "source_order_end",
    "line_start",
    "line_end",
    "derived_view",
    "speaker_name",
    "speakers",
    "contains_aemeath",
    "aemeath_line_count",
    "field_name",
    "field_key",
)
_MANIFEST_STATUSES = {"building", "staging", "ready", "failed"}

GenerationStatus = Literal["building", "staging", "ready", "failed"]


def validate_index_id(index_id: str) -> str:
    if not isinstance(index_id, str) or not _INDEX_ID_RE.fullmatch(index_id):
        raise RagIndexConfigError("rag_index_id_invalid")
    return index_id


def validate_generation_id(generation_id: str) -> str:
    if not isinstance(generation_id, str) or not _GENERATION_ID_RE.fullmatch(generation_id):
        raise RagIndexManifestError("rag_generation_id_invalid")
    return generation_id


def _sha256(domain: bytes, value: bytes) -> str:
    return f"sha256:{hashlib.sha256(domain + value).hexdigest()}"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_metadata_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, (list, tuple)):
        return [_normalize_metadata_value(item) for item in value]
    raise RagIndexManifestError("rag_retrieval_metadata_value_invalid")


def safe_source_id(chunk: ChunkRecord) -> str:
    explicit = chunk.metadata.get("source_id")
    if isinstance(explicit, str) and _SAFE_SOURCE_ID_RE.fullmatch(explicit):
        return explicit
    source_file = chunk.metadata.get("original_source_file")
    record_id = chunk.metadata.get("record_id")
    chunk_index = chunk.metadata.get("chunk_index", 0)
    if isinstance(source_file, str) and record_id is not None:
        source_name = Path(source_file).stem
        candidate = f"favor:{source_name}:{record_id}:{chunk_index}"
        if _SAFE_SOURCE_ID_RE.fullmatch(candidate):
            return candidate
    state_hash = chunk.metadata.get("state_hash")
    source_start = chunk.metadata.get("source_order_start")
    source_end = chunk.metadata.get("source_order_end")
    if isinstance(state_hash, str) and source_start is not None and source_end is not None:
        candidate = f"flow:{state_hash}:{source_start}-{source_end}:{chunk_index}"
        if _SAFE_SOURCE_ID_RE.fullmatch(candidate):
            return candidate
    return sanitize_source_id(None, fallback=chunk.id)


def sanitize_source_id(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_SOURCE_ID_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:24]
    return f"chunk:{digest}"


def canonical_retrieval_metadata(chunk: ChunkRecord) -> dict[str, object]:
    metadata: dict[str, object] = {"source_id": safe_source_id(chunk)}
    for key in _METADATA_KEYS:
        if key in chunk.metadata:
            metadata[key] = _normalize_metadata_value(chunk.metadata[key])
    return metadata


def managed_chunk(chunk: ChunkRecord) -> ChunkRecord:
    return ChunkRecord(
        id=chunk.id,
        namespace=chunk.namespace,
        source_type=chunk.source_type,
        content=chunk.content,
        metadata=canonical_retrieval_metadata(chunk),
    )


@dataclass(frozen=True)
class CanonicalInventoryEntry:
    chunk_id: str
    namespace: str
    source_type: str
    canonical_content_hash: str
    retrieval_metadata_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CanonicalInventoryEntry:
        expected = {
            "chunk_id",
            "namespace",
            "source_type",
            "canonical_content_hash",
            "retrieval_metadata_fingerprint",
        }
        if set(value) != expected:
            raise RagIndexManifestError("rag_inventory_entry_fields_invalid")
        try:
            entry = cls(**value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RagIndexManifestError("rag_inventory_entry_invalid") from exc
        identity_fields = (entry.chunk_id, entry.namespace, entry.source_type)
        if not all(isinstance(item, str) and item for item in identity_fields):
            raise RagIndexManifestError("rag_inventory_entry_invalid")
        hashes = (
            entry.canonical_content_hash,
            entry.retrieval_metadata_fingerprint,
        )
        if any(not isinstance(item, str) or not _SHA256_RE.fullmatch(item) for item in hashes):
            raise RagIndexManifestError("rag_inventory_hash_invalid")
        return entry


@dataclass(frozen=True)
class CorpusInventory:
    entries: tuple[CanonicalInventoryEntry, ...]
    corpus_fingerprint: str
    managed_chunks: tuple[ChunkRecord, ...]


def build_corpus_inventory(chunks: Sequence[ChunkRecord]) -> CorpusInventory:
    prepared: list[tuple[CanonicalInventoryEntry, ChunkRecord]] = []
    seen_ids: set[str] = set()
    for raw_chunk in chunks:
        chunk = managed_chunk(raw_chunk)
        if not chunk.id or chunk.id in seen_ids:
            reason = "rag_chunk_id_duplicate" if chunk.id in seen_ids else "rag_chunk_id_invalid"
            raise RagIndexManifestError(reason)
        seen_ids.add(chunk.id)
        metadata_bytes = _canonical_json_bytes(chunk.metadata)
        entry = CanonicalInventoryEntry(
            chunk_id=chunk.id,
            namespace=chunk.namespace,
            source_type=chunk.source_type,
            canonical_content_hash=canonical_content_hash(chunk.content),
            retrieval_metadata_fingerprint=_sha256(
                b"aemeath-rag-retrieval-metadata/v1\0",
                metadata_bytes,
            ),
        )
        prepared.append((entry, chunk))
    prepared.sort(key=lambda item: item[0].chunk_id.encode("utf-8"))
    entries = tuple(item[0] for item in prepared)
    corpus_fingerprint = corpus_fingerprint_for_entries(entries)
    return CorpusInventory(
        entries=entries,
        corpus_fingerprint=corpus_fingerprint,
        managed_chunks=tuple(item[1] for item in prepared),
    )


def corpus_fingerprint_for_entries(entries: Sequence[CanonicalInventoryEntry]) -> str:
    inventory_bytes = b"\n".join(
        _canonical_json_bytes(entry.to_dict()) for entry in entries
    )
    return _sha256(b"aemeath-rag-corpus/v1\0", inventory_bytes)


@dataclass(frozen=True)
class GenerationManifest:
    schema_version: str
    index_format_version: str
    index_id: str
    generation_id: str
    generation_number: int
    status: GenerationStatus
    embedding_signature_json: str
    embedding_signature_fingerprint: str
    embedding_purpose: str
    corpus_fingerprint: str
    inventory_schema_version: str
    retrieval_metadata_schema_version: str
    parser_version: str
    parser_config_fingerprint: str
    chunker_version: str
    chunker_config_fingerprint: str
    chunk_id_scheme_version: str
    namespace_policy_version: str
    chunk_count: int
    document_count: int
    namespace_counts: dict[str, int]
    build_plan_fingerprint: str
    build_mode: str
    base_generation_id: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    failure_reason: str | None

    @property
    def signature(self) -> EmbeddingModelSignature:
        return deserialize_signature(
            self.embedding_signature_json,
            self.embedding_signature_fingerprint,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def with_status(
        self,
        status: GenerationStatus,
        *,
        now: str,
        failure_reason: str | None = None,
    ) -> GenerationManifest:
        allowed = {
            "building": {"building", "staging", "failed"},
            "staging": {"staging", "ready", "failed"},
            "ready": {"ready"},
            "failed": {"failed"},
        }
        if status not in allowed[self.status]:
            raise RagIndexManifestError("rag_generation_status_transition_invalid")
        return replace(
            self,
            status=status,
            updated_at=now,
            completed_at=now if status in {"ready", "failed"} else None,
            failure_reason=failure_reason,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> GenerationManifest:
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise RagIndexManifestError("rag_generation_manifest_fields_invalid")
        try:
            manifest = cls(**value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RagIndexManifestError("rag_generation_manifest_invalid") from exc
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.schema_version != INDEX_MANIFEST_SCHEMA_VERSION:
            raise RagIndexManifestError("rag_generation_manifest_schema_invalid")
        if self.index_format_version != INDEX_FORMAT_VERSION:
            raise RagIndexManifestError("rag_index_format_invalid")
        validate_index_id(self.index_id)
        validate_generation_id(self.generation_id)
        if self.status not in _MANIFEST_STATUSES:
            raise RagIndexManifestError("rag_generation_status_invalid")
        if (
            isinstance(self.generation_number, bool)
            or not isinstance(self.generation_number, int)
            or self.generation_number <= 0
        ):
            raise RagIndexManifestError("rag_generation_number_invalid")
        if self.embedding_purpose != "document":
            raise RagIndexManifestError("rag_generation_purpose_invalid")
        try:
            _ = self.signature
        except EmbeddingArtifactValidationError as exc:
            raise RagIndexManifestError("rag_generation_signature_invalid") from exc
        for value in (
            self.corpus_fingerprint,
            self.parser_config_fingerprint,
            self.chunker_config_fingerprint,
            self.build_plan_fingerprint,
        ):
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise RagIndexManifestError("rag_generation_fingerprint_invalid")
        if self.inventory_schema_version != INVENTORY_SCHEMA_VERSION:
            raise RagIndexManifestError("rag_inventory_schema_invalid")
        if self.retrieval_metadata_schema_version != RETRIEVAL_METADATA_SCHEMA_VERSION:
            raise RagIndexManifestError("rag_retrieval_metadata_schema_invalid")
        counts = (self.chunk_count, self.document_count)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise RagIndexManifestError("rag_generation_count_invalid")
        if self.document_count > self.chunk_count:
            raise RagIndexManifestError("rag_generation_count_invalid")
        if not isinstance(self.namespace_counts, dict) or any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.namespace_counts.items()
        ):
            raise RagIndexManifestError("rag_generation_namespace_counts_invalid")
        if sum(self.namespace_counts.values()) != self.chunk_count:
            raise RagIndexManifestError("rag_generation_namespace_counts_invalid")
        version_fields = (
            self.parser_version,
            self.chunker_version,
            self.chunk_id_scheme_version,
            self.namespace_policy_version,
        )
        if any(not isinstance(value, str) or not value.strip() for value in version_fields):
            raise RagIndexManifestError("rag_generation_version_invalid")
        if self.build_mode not in {"full", "incremental"}:
            raise RagIndexManifestError("rag_generation_build_mode_invalid")
        if self.build_mode == "full" and self.base_generation_id is not None:
            raise RagIndexManifestError("rag_generation_base_invalid")
        if self.build_mode == "incremental":
            if self.base_generation_id is None:
                raise RagIndexManifestError("rag_generation_base_invalid")
            validate_generation_id(self.base_generation_id)
        timestamps = (self.created_at, self.updated_at)
        if any(not isinstance(value, str) or not value or len(value) > 64 for value in timestamps):
            raise RagIndexManifestError("rag_generation_timestamp_invalid")
        terminal = self.status in {"ready", "failed"}
        if terminal != isinstance(self.completed_at, str):
            raise RagIndexManifestError("rag_generation_completion_invalid")
        if isinstance(self.completed_at, str) and (
            not self.completed_at or len(self.completed_at) > 64
        ):
            raise RagIndexManifestError("rag_generation_completion_invalid")
        if self.status == "failed":
            if (
                not isinstance(self.failure_reason, str)
                or not re.fullmatch(r"[a-z0-9_]{1,128}", self.failure_reason)
            ):
                raise RagIndexManifestError("rag_generation_failure_reason_invalid")
        elif self.failure_reason is not None:
            raise RagIndexManifestError("rag_generation_failure_reason_invalid")


@dataclass(frozen=True)
class ActiveGenerationReference:
    generation_id: str
    manifest_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ActiveGenerationReference:
        if set(value) != {"generation_id", "manifest_fingerprint"}:
            raise RagIndexManifestError("rag_active_reference_fields_invalid")
        reference = cls(
            generation_id=str(value["generation_id"]),
            manifest_fingerprint=str(value["manifest_fingerprint"]),
        )
        validate_generation_id(reference.generation_id)
        if not _SHA256_RE.fullmatch(reference.manifest_fingerprint):
            raise RagIndexManifestError("rag_active_reference_fingerprint_invalid")
        return reference


@dataclass(frozen=True)
class ActiveIndexManifest:
    schema_version: str
    index_id: str
    active_by_signature: dict[str, ActiveGenerationReference]
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "index_id": self.index_id,
            "active_by_signature": {
                fingerprint: reference.to_dict()
                for fingerprint, reference in sorted(self.active_by_signature.items())
            },
            "updated_at": self.updated_at,
        }

    @classmethod
    def empty(cls, *, index_id: str, now: str) -> ActiveIndexManifest:
        return cls(
            schema_version=ACTIVE_MANIFEST_SCHEMA_VERSION,
            index_id=validate_index_id(index_id),
            active_by_signature={},
            updated_at=now,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ActiveIndexManifest:
        if set(value) != {"schema_version", "index_id", "active_by_signature", "updated_at"}:
            raise RagIndexManifestError("rag_active_manifest_fields_invalid")
        mapping = value["active_by_signature"]
        if not isinstance(mapping, dict):
            raise RagIndexManifestError("rag_active_mapping_invalid")
        active_by_signature: dict[str, ActiveGenerationReference] = {}
        for fingerprint, raw_reference in mapping.items():
            if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
                raise RagIndexManifestError("rag_active_signature_invalid")
            if not isinstance(raw_reference, dict):
                raise RagIndexManifestError("rag_active_reference_invalid")
            active_by_signature[fingerprint] = ActiveGenerationReference.from_dict(raw_reference)
        manifest = cls(
            schema_version=str(value["schema_version"]),
            index_id=str(value["index_id"]),
            active_by_signature=active_by_signature,
            updated_at=str(value["updated_at"]),
        )
        if manifest.schema_version != ACTIVE_MANIFEST_SCHEMA_VERSION:
            raise RagIndexManifestError("rag_active_manifest_schema_invalid")
        validate_index_id(manifest.index_id)
        return manifest


@dataclass(frozen=True)
class BuildCheckpoint:
    schema_version: str
    build_plan_fingerprint: str
    phase: str
    next_batch_index: int
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BuildCheckpoint:
        if set(value) != set(cls.__dataclass_fields__):
            raise RagIndexManifestError("rag_checkpoint_fields_invalid")
        try:
            checkpoint = cls(**value)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RagIndexManifestError("rag_checkpoint_invalid") from exc
        if checkpoint.schema_version != "rag-index-checkpoint/v1":
            raise RagIndexManifestError("rag_checkpoint_schema_invalid")
        if not _SHA256_RE.fullmatch(checkpoint.build_plan_fingerprint):
            raise RagIndexManifestError("rag_checkpoint_fingerprint_invalid")
        if checkpoint.phase not in {"embedding", "stale_cleanup", "validation"}:
            raise RagIndexManifestError("rag_checkpoint_phase_invalid")
        if (
            isinstance(checkpoint.next_batch_index, bool)
            or not isinstance(checkpoint.next_batch_index, int)
            or checkpoint.next_batch_index < 0
        ):
            raise RagIndexManifestError("rag_checkpoint_index_invalid")
        return checkpoint


@dataclass(frozen=True)
class GenerationScanDiagnostic:
    generation_id: str
    reason: str


@dataclass(frozen=True)
class GenerationManifestScan:
    manifests: tuple[GenerationManifest, ...]
    diagnostics: tuple[GenerationScanDiagnostic, ...]


def manifest_fingerprint(manifest: GenerationManifest) -> str:
    return _sha256(
        b"aemeath-rag-generation-manifest/v1\0",
        _canonical_json_bytes(manifest.to_dict()),
    )


def config_fingerprint(value: Mapping[str, object]) -> str:
    try:
        payload = _canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise RagIndexConfigError("rag_build_config_not_json") from exc
    return _sha256(b"aemeath-rag-build-config/v1\0", payload)


def build_plan_fingerprint(
    *,
    signature_fingerprint: str,
    corpus_fingerprint: str,
    parser_version: str,
    parser_config_fingerprint: str,
    chunker_version: str,
    chunker_config_fingerprint: str,
    chunk_id_scheme_version: str,
    namespace_policy_version: str,
    write_batch_size: int,
) -> str:
    return _sha256(
        b"aemeath-rag-build-plan/v2\0",
        _canonical_json_bytes(
            {
                "signature_fingerprint": signature_fingerprint,
                "corpus_fingerprint": corpus_fingerprint,
                "parser_version": parser_version,
                "parser_config_fingerprint": parser_config_fingerprint,
                "chunker_version": chunker_version,
                "chunker_config_fingerprint": chunker_config_fingerprint,
                "chunk_id_scheme_version": chunk_id_scheme_version,
                "namespace_policy_version": namespace_policy_version,
                "write_batch_size": write_batch_size,
            }
        ),
    )


class RagIndexFileRepository:
    def __init__(self, *, root: Path, index_id: str) -> None:
        if not isinstance(root, Path):
            raise RagIndexConfigError("rag_index_root_invalid")
        self.root = root.resolve(strict=False)
        self.index_id = validate_index_id(index_id)
        self.index_root = self.root / self.index_id
        self.generations_root = self.index_root / "generations"
        self.active_path = self.index_root / "active.json"
        self.lock_path = self.index_root / "build.lock"

    def ensure_layout(self) -> None:
        self.generations_root.mkdir(parents=True, exist_ok=True)

    def generation_dir(self, generation_id: str) -> Path:
        validate_generation_id(generation_id)
        path = self.generations_root / generation_id
        self._assert_within_root(path)
        return path

    def manifest_path(self, generation_id: str) -> Path:
        return self.generation_dir(generation_id) / "manifest.json"

    def inventory_path(self, generation_id: str) -> Path:
        return self.generation_dir(generation_id) / "corpus_inventory.jsonl"

    def checkpoint_path(self, generation_id: str) -> Path:
        return self.generation_dir(generation_id) / "checkpoint.json"

    def database_path(self, generation_id: str) -> Path:
        return self.generation_dir(generation_id) / "index.sqlite"

    def read_active(self, *, now: str) -> ActiveIndexManifest:
        if not self.active_path.is_file():
            return ActiveIndexManifest.empty(index_id=self.index_id, now=now)
        raw = self._read_json(self.active_path)
        active = ActiveIndexManifest.from_dict(raw)
        if active.index_id != self.index_id:
            raise RagIndexManifestError("rag_active_index_id_mismatch")
        return active

    def write_active(self, active: ActiveIndexManifest) -> None:
        if active.index_id != self.index_id:
            raise RagIndexManifestError("rag_active_index_id_mismatch")
        previous_exists = self.active_path.is_file()
        try:
            previous_bytes = self.active_path.read_bytes() if previous_exists else None
        except OSError as exc:
            raise RagIndexManifestError("rag_active_snapshot_failed") from exc
        try:
            self._atomic_write_json(self.active_path, active.to_dict())
            try:
                verified = ActiveIndexManifest.from_dict(self._read_json(self.active_path))
            except RagIndexError as exc:
                raise RagIndexManifestError("rag_active_reopen_verify_failed") from exc
            if verified != active:
                raise RagIndexManifestError("rag_active_reopen_verify_failed")
        except Exception:
            try:
                self._restore_active(previous_bytes=previous_bytes)
            except Exception as rollback_exc:
                raise RagIndexManifestError("rag_active_rollback_failed") from rollback_exc
            raise

    def read_generation_manifest(self, generation_id: str) -> GenerationManifest:
        manifest = GenerationManifest.from_dict(self._read_json(self.manifest_path(generation_id)))
        if manifest.index_id != self.index_id or manifest.generation_id != generation_id:
            raise RagIndexManifestError("rag_generation_manifest_identity_mismatch")
        return manifest

    def write_generation_manifest(self, manifest: GenerationManifest) -> None:
        manifest.validate()
        if manifest.index_id != self.index_id:
            raise RagIndexManifestError("rag_generation_manifest_identity_mismatch")
        self.generation_dir(manifest.generation_id).mkdir(parents=False, exist_ok=True)
        self._atomic_write_json(self.manifest_path(manifest.generation_id), manifest.to_dict())

    def write_inventory(
        self,
        generation_id: str,
        entries: Sequence[CanonicalInventoryEntry],
    ) -> None:
        payload = b"\n".join(_canonical_json_bytes(entry.to_dict()) for entry in entries)
        if payload:
            payload += b"\n"
        self._atomic_write_bytes(self.inventory_path(generation_id), payload)

    def read_inventory(self, generation_id: str) -> tuple[CanonicalInventoryEntry, ...]:
        path = self.inventory_path(generation_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RagIndexManifestError("rag_inventory_read_failed") from exc
        entries: list[CanonicalInventoryEntry] = []
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RagIndexManifestError("rag_inventory_json_invalid") from exc
            if not isinstance(raw, dict):
                raise RagIndexManifestError("rag_inventory_entry_invalid")
            entries.append(CanonicalInventoryEntry.from_dict(raw))
        if entries != sorted(entries, key=lambda item: item.chunk_id.encode("utf-8")):
            raise RagIndexManifestError("rag_inventory_order_invalid")
        if len({entry.chunk_id for entry in entries}) != len(entries):
            raise RagIndexManifestError("rag_inventory_duplicate")
        return tuple(entries)

    def write_checkpoint(self, generation_id: str, checkpoint: BuildCheckpoint) -> None:
        self._atomic_write_json(self.checkpoint_path(generation_id), checkpoint.to_dict())

    def read_checkpoint(self, generation_id: str) -> BuildCheckpoint:
        return BuildCheckpoint.from_dict(self._read_json(self.checkpoint_path(generation_id)))

    def scan_generation_manifests(self) -> GenerationManifestScan:
        if not self.generations_root.is_dir():
            return GenerationManifestScan(manifests=(), diagnostics=())
        manifests: list[GenerationManifest] = []
        diagnostics: list[GenerationScanDiagnostic] = []
        for path in sorted(self.generations_root.iterdir()):
            if not path.is_dir() or not _GENERATION_ID_RE.fullmatch(path.name):
                continue
            try:
                manifest = self.read_generation_manifest(path.name)
            except RagIndexError as exc:
                diagnostics.append(
                    GenerationScanDiagnostic(
                        generation_id=path.name,
                        reason=exc.reason,
                    )
                )
                continue
            manifests.append(manifest)
            try:
                inventory = self.read_inventory(path.name)
                if (
                    corpus_fingerprint_for_entries(inventory)
                    != manifest.corpus_fingerprint
                    or len(inventory) != manifest.chunk_count
                ):
                    raise RagIndexManifestError("rag_generation_inventory_mismatch")
            except RagIndexError as exc:
                diagnostics.append(
                    GenerationScanDiagnostic(
                        generation_id=path.name,
                        reason=exc.reason,
                    )
                )
        return GenerationManifestScan(
            manifests=tuple(sorted(manifests, key=lambda item: item.generation_number)),
            diagnostics=tuple(diagnostics),
        )

    def list_generation_manifests(self) -> list[GenerationManifest]:
        return list(self.scan_generation_manifests().manifests)

    def next_generation_number(self) -> int:
        if not self.generations_root.is_dir():
            return 1
        numbers = [
            int(path.name[1:7])
            for path in self.generations_root.iterdir()
            if path.is_dir() and _GENERATION_ID_RE.fullmatch(path.name)
        ]
        return max(numbers, default=0) + 1

    def _restore_active(self, *, previous_bytes: bytes | None) -> None:
        if previous_bytes is not None:
            self._atomic_write_bytes(self.active_path, previous_bytes)
            try:
                restored = self.active_path.read_bytes()
            except OSError as exc:
                raise RagIndexManifestError("rag_active_rollback_failed") from exc
            if restored != previous_bytes:
                raise RagIndexManifestError("rag_active_rollback_failed")
            return
        try:
            self.active_path.unlink(missing_ok=True)
            self._fsync_directory(self.active_path.parent)
        except OSError as exc:
            raise RagIndexManifestError("rag_active_rollback_failed") from exc

    def _read_json(self, path: Path) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RagIndexManifestError("rag_index_json_read_failed") from exc
        if not isinstance(raw, dict):
            raise RagIndexManifestError("rag_index_json_object_required")
        return raw

    def _atomic_write_json(self, path: Path, value: object) -> None:
        self._atomic_write_bytes(path, _canonical_json_bytes(value) + b"\n")

    def _atomic_write_bytes(self, path: Path, payload: bytes) -> None:
        self._assert_within_root(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            delay = 0.05
            last_error: OSError | None = None
            for _attempt in range(5):
                try:
                    os.replace(temporary_path, path)
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    if os.name != "nt":
                        break
                    time.sleep(delay)
                    delay *= 2
            if last_error is not None:
                raise last_error
            self._fsync_directory(path.parent)
        except OSError as exc:
            raise RagIndexManifestError("rag_index_atomic_write_failed") from exc
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _assert_within_root(self, path: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(self.index_root.resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise RagIndexManifestError("rag_index_path_escape") from exc

    def _fsync_directory(self, path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def namespace_counts(chunks: Iterable[ChunkRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.namespace] = counts.get(chunk.namespace, 0) + 1
    return dict(sorted(counts.items()))


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
