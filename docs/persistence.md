# Persistence Backend Plan

## Current Decision

The default persistence backend remains the existing raw SQLite adapter:

```env
AEMEATH_DB_BACKEND=sqlite_raw
```

Supported values:

- `sqlite_raw`: current `SQLiteStore`, backed by `app/db/schema.sql` and the existing
  hand-written `schema_migrations` / backfill logic.
- `sqlalchemy`: experimental sync SQLAlchemy backend, backed by SQLAlchemy models and
  `Base.metadata.create_all(...)` for fresh SQLite databases.

The SQLAlchemy backend is opt-in for v0.1. It is not the default, does not support PostgreSQL, and
does not migrate old `data/aemeath.db` files.

## Architecture

The SQLAlchemy implementation uses repositories internally and a thin facade externally:

```text
SqlAlchemyStore facade
  -> MessageRepository
  -> SessionRepository
  -> AccountRepository
  -> RelationshipRepository
  -> MemoryRepository
  -> MemoryJobRepository
  -> MemoryEmbeddingRepository
```

`SqlAlchemyStore` exists to keep current port injection stable while the runtime still expects one
object that implements:

- `MessageStore`
- `SessionStorePort`
- `RelationshipStore`
- `MemoryStore`
- `AccountStorePort`
- memory extraction job operations
- memory embedding operations

The facade should only compose and forward calls. SQLAlchemy query logic belongs in the
repositories.

## Schema Ownership

Current ownership:

```text
sqlite_raw backend:
  app/db/schema.sql
  app/db/sqlite_store.py hand-written bootstrap and schema_migrations

sqlalchemy backend v0.1:
  app/db/models.py
  create_all on fresh SQLite databases

future:
  Alembic manages SQLAlchemy backend migrations
```

Do not let the same database be managed by both `schema.sql/schema_migrations` and Alembic.

## Alembic Baseline Strategy

Alembic is intentionally deferred for this implementation round.

When Alembic is introduced later:

1. Generate a baseline from `app/db/models.py`.
2. Use Alembic's own `alembic_version` table.
3. Keep raw SQLite's `schema_migrations` table scoped to `SQLiteStore`.
4. For development SQLAlchemy databases, prefer delete-and-recreate until the baseline is
   accepted.
5. For any existing SQLAlchemy database that must be preserved, compare schema first, then stamp
   the baseline only after manager review.

## Compatibility Notes

The SQLAlchemy v0.1 backend preserves current public DTOs. Datetime fields remain ISO strings, JSON
fields remain text columns with explicit serialization, and embeddings remain `float32` BLOBs.

## Embedding Artifact Persistence

Embedding spaces are persisted as independent artifacts rather than as a single vector attached to
one Memory or RAG chunk. A target may therefore retain Hash, local BGE, and SiliconFlow artifacts
side by side while a later worker rebuilds or rolls back one space.

Every canonical artifact records:

- `embedding_signature_json`: exact `EmbeddingModelSignature.canonical()` value;
- `embedding_signature_fingerprint`: SHA-256 of that canonical value;
- `embedding_purpose`: currently `document` for durable material;
- `embedding_content_hash`: SHA-256 of the canonical text that was actually embedded;
- `embedding_vector_format`: `f32le-v1`;
- `embedding_vector_sha256`: SHA-256 of the persisted canonical vector bytes.

The new artifact APIs require the caller to create canonical text, content hash, and embedding first.
Within the same write transaction, the store recomputes the current target content hash and rejects a
write if it differs. This prevents an embedding created for older text from being attached after the
Memory or chunk changes.

For an existing `(target_id, signature fingerprint, purpose)` artifact:

- same content hash: preserve the stored vector and return `reused`; a different incoming vector
  returns the `incoming_vector_differs` diagnostic without overwriting it;
- different content hash: replace that artifact only;
- different signature: add a separate artifact.

Canonical searches prefilter by query signature fingerprint and purpose in SQL, then verify the complete
canonical JSON before scoring. They only score exact-space, valid, non-stale artifacts. The new parallel
search APIs return aggregate compatibility diagnostics after the normal user/scope or namespace filter,
and never expose target IDs. The older list-returning Memory and RAG methods remain legacy compatibility
APIs for one release.

### Memory Canonical Contracts

Active Memory identity is `user_id + scope + scope_character_id + scope_session_id + content`. Both
backends enforce it with the same partial unique expression index, restricted to
`status = 'active' AND is_active = 1`. `create_or_get_exact_active_memory(...)` first enters a SQLite
writer transaction, selects that exact active identity, and either reuses the original row unchanged or
performs an ordinary insert. It never uses `INSERT OR IGNORE`. Archived or disabled rows are deliberately
outside the invariant and therefore allow a replacement active Memory.

Raw SQLite checks legacy data for duplicate active identities before creating the index. A duplicate
raises content-safe `STORAGE_MIGRATION_REQUIRED` without writing the migration marker or altering Memory
rows. SQLAlchemy verifies the index on every existing experimental database using `PRAGMA index_list` and
`PRAGMA index_xinfo` plus the `sqlite_master` DDL, including its uniqueness, exact five-key expression
layout, and exact partial predicate. An expanded or narrowed `WHERE` expression is invalid even when it
contains the expected terms. The DDL normalizer is quote-aware: it folds only structural SQL outside
single-quoted literals, preserving literal whitespace, case, and escaped quotes. It fails fast rather
than relying on incomplete reflection or attempting repair.

Canonical Memory search accepts a `MemoryArtifactEligibility` value. `eligible_memory_count`, artifact
candidates, and aggregate diagnostics share the identical owner, scope, active/status, namespace,
memory-type, and minimum-confidence predicates in one transaction snapshot. `eligible_memory_count` is
coverage, not a BLOB scan count.

`list_memory_embedding_artifact_gaps(...)` is a persistence-only, internal Worker API. It scans at most
`min(limit * 4, 400)` active Memory rows in stable `(created_at, id)` order and uses one bounded query per
window to fetch the exact artifact, the presence of another canonical space, and legacy evidence. Reasons
are `missing`, `content_stale`, `vector_corrupt`, `signature_corrupt`, and `legacy_only`. A valid artifact
in another signature space only makes the requested exact space `missing`; it is never damaged or called
corrupt. When a window contains more gaps than the requested page size, the cursor is the last Memory
actually checked, so a subsequent page cannot skip unreturned gaps. An empty gap page may still return a
cursor after scanning healthy rows. The API has no repair, retry, fallback, or rebuild policy and must not
be exposed through Service, FastAPI, or MCP.

`long_term_memory_embeddings` and RAG's `legacy_chunk_embedding_artifacts` retain legacy evidence. Raw
SQLite can promote only a 384-dimensional `deterministic_hash_v1` row whose canonical document text is
re-embedded and byte-for-byte verified. A bad legacy row is left unverifiable without blocking startup;
only database or program-level migration failures roll back. Local BGE, SiliconFlow, unknown models,
malformed blobs, and non-matching Hash rows remain legacy and cannot enter canonical search. Once the
RAG copy and promotion marker are committed, the temporary `chunks_legacy_embedding_v0` table is dropped
without an automatic `VACUUM`.

Legacy diagnostics suppress only the individual verified `deterministic_hash_v1` legacy row that exactly
matches its promoted canonical artifact. A BGE, unknown, stale, or corrupt legacy row remains
`legacy_unverified` even when the same Memory or RAG chunk also has a usable canonical artifact in another
space. This compatibility classification is independent of the active query profile.

The SQLAlchemy backend deliberately does not migrate pre-artifact experimental databases. It raises
`STORAGE_MIGRATION_REQUIRED`; a developer must explicitly back up, delete, and recreate the database.
Runtime code never deletes a database automatically. RAG index manifest state, repair scheduling,
fallback, and rebuild policy remain owned by later Memory and RAG work.

Known SQLite/PostgreSQL compatibility areas for future work:

- JSON text columns should become JSON/JSONB only after a reviewed migration.
- ISO datetime strings avoid timezone conversion now, but typed datetime columns may be preferable
  for PostgreSQL later.
- Embedding BLOB storage can map to PostgreSQL `BYTEA`; pgvector should be a separate design.
- SQLite job claim uses database locking semantics; PostgreSQL should use row-level locking such
  as `FOR UPDATE SKIP LOCKED`.
- Dialect-specific upsert helpers should be isolated inside repositories.

## Testing

Store contract tests live under `tests/store_contract/` and are parameterized to run against both:

- `SQLiteStore`
- `SqlAlchemyStore`

Current contract coverage includes:

- session create/get/list/end/resume/archive/require;
- message save/recent/list;
- session read model `message_count` and `last_message_at`;
- relationship state create/update/clamp;
- account bootstrap, dev token hash lookup, login touch, and status update;
- memory scope visibility;
- memory exact dedupe;
- memory owner-only archive/disable;
- memory extraction job enqueue idempotency, claim, complete, and fail;
- embedding upsert/search/retrieval.
- exact-active Memory create-or-get concurrency, scope identity isolation, and first-write preservation;
- eligible-Memory coverage counts under combined owner, scope, namespace, type, and confidence filters;
- canonical artifact-gap classification and bounded keyset pagination;
- raw duplicate-identity migration fail-fast with no row or marker mutation;
- SQLAlchemy missing/wrong expression-index fail-fast.
- expanded or narrowed active-identity predicates rejected by both backends;
- altered string literals in the predicate or `COALESCE` expression rejected by both backends;
- archive followed by restart and same-identity recreation.

Legacy raw SQLite migration/backfill tests remain specific to `SQLiteStore`.

Artifact contract coverage additionally includes:

- multiple exact signatures for one Memory or RAG chunk;
- canonical content hash binding and replacement only after target content changes;
- non-destructive handling of different valid incoming vectors;
- vector SHA-256 corruption detection;
- diagnostics filtered by Memory ownership/scope or RAG namespace;
- verified Hash promotion and BGE/unknown legacy rejection;
- SQLAlchemy experimental-schema fail-fast behavior.

## Rollback

If the experimental backend regresses, set:

```env
AEMEATH_DB_BACKEND=sqlite_raw
```

`SQLiteStore` remains in place and continues to be the default until manager approval explicitly
changes it.

## SQLite Backup And Restore

D6 production backup targets `sqlite_raw` only. Online backup uses Python's SQLite backup API against
a read-only source connection and publishes each backup as one generation directory containing
`aemeath.db` plus immutable `manifest.json`. Direct copying of the live database or live WAL/SHM
sidecars is forbidden.
Production backup manifests must include exact app and ops image digests, deploy-lock hash, rendered
Compose config hash, and Git commit; placeholder identity is rejected before publication.

Verification is independent and read-only: it checks manifest hash and size, SQLite integrity,
`foreign_key_check`, exact schema/migration contracts, active Memory identity index structure,
deployment identity fields, and safe count buckets. It must not instantiate `SQLiteStore` or
`SqlAlchemyStore`, because those paths may create or migrate schema.

Restore is a writer-stopped operation. It quarantines the old `aemeath.db`, `aemeath.db-wal`, and
`aemeath.db-shm` as one set with per-file journal intents and digest/location reconciliation, then
installs the verified candidate main DB without old sidecars. `active.pointer.json` is only a
locator/progress hint; after a crash, the verified per-restore journal and filesystem/digest
reconciliation are authoritative.
An OS-owned restore lock serializes the whole mutation path, and candidate installation uses a
journaled temp-file publication protocol so crashes after live DB publication can resume or roll back.

Production restore hard-rejects SQLAlchemy manifests in D6 v0.1. Local/dev SQLAlchemy-on-SQLite
verification remains only a compatibility check for the file mechanics, not a production recovery
path.
