# RAG Generation Store Persistence Delivery

## Scope

Implemented Persistence-only preconditions on `SqliteVectorStore` for a future managed RAG generation.
No generation manifest, registry, builder, lock, canonical retriever, EvidenceProvider wiring, runtime
configuration, or active `WorldInfoEvidenceProvider` behavior was added or changed. Existing tables and
their schema remain unchanged.

## New Interfaces

- `SqliteVectorStore.open_read_only(db_path: Path) -> SqliteVectorStore`
- `SqliteVectorStore.backup_to(target_path: Path) -> None`
- `SqliteVectorStore.delete_chunks(*, chunk_ids: list[str]) -> ChunkDeletionResult`
- `SqliteVectorStore.inspect_exact_signature_coverage(*, signature, expected_chunks) -> ExactSignatureCoverage`
- `RagChunkExpectation.from_chunk(chunk)` provides a manifest-independent expected inventory fact:
  chunk id, namespace, source type, and canonical content hash.

## Read-only And Clone Semantics

`open_read_only` bypasses `__init__`, uses SQLite URI `mode=ro`, creates no parent or database file, and
does not run schema initialization, migration, or cleanup. It validates required tables, columns, and the
canonical artifact primary key before serving reads. Existing writers (`reset`, legacy upsert, canonical
upsert, and `delete_chunks`) fail deterministically with `rag_generation_store_read_only`.

`backup_to` uses SQLite's backup API for a committed snapshot. It writes to a temporary database in the
target directory, closes both SQLite connections explicitly before publishing, then uses a no-overwrite
hard-link publish. Existing targets are rejected, source data is never migrated or vacuumed, and a failed
backup only cleans up its own temporary file. The clone opens through the normal writable store.

## Delete And Coverage Semantics

`delete_chunks` deduplicates input, accepts empty and missing IDs, batches at 900 IDs, and deletes canonical
artifacts, legacy artifacts, and chunks inside one `BEGIN IMMEDIATE` transaction. Any SQLite failure rolls
back all three tables. It does not touch migration markers or run `VACUUM`.

Coverage is a read-only snapshot and returns only aggregate counts plus safe reason/count diagnostics. It
checks full canonical signature JSON, `document` purpose, artifact validation, current canonical content
hash, expected chunk namespace/source type/content hash, incompatible signature artifacts, and all legacy
artifact rows. `ready_eligible` is true only when every required inventory and integrity count is clean.

## Validation

- `pytest -q tests/test_vector_artifact_store.py tests/test_rag_generation_store_contract.py`: passed.
- Full `pytest -q`, `ruff check .`, and `python -m compileall -q app tests`: passed.
- `git diff --check` found no whitespace errors; Git reported only existing CRLF conversion notices.

## Residual Risks

- `backup_to` uses same-directory hard links to preserve no-overwrite publication semantics. This is valid
  on the supported Windows NTFS workspace; a filesystem that disallows hard links fails safely with
  `rag_generation_store_backup_failed` and leaves no target clone.
- Backup is snapshot-consistent but does not coordinate a future generation build lock. Locking, manifest
  ownership, and concurrent generation replacement remain RAG Worker responsibilities.
