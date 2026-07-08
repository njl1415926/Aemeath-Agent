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

Legacy raw SQLite migration/backfill tests remain specific to `SQLiteStore`.

## Rollback

If the experimental backend regresses, set:

```env
AEMEATH_DB_BACKEND=sqlite_raw
```

`SQLiteStore` remains in place and continues to be the default until manager approval explicitly
changes it.
