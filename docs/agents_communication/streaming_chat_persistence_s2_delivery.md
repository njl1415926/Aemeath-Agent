# T-0143 Streaming Chat Persistence S2 Delivery

Date: 2026-08-05

Status: **S2 Accepted by Manager / T-0143 In Progress**

## Scope

This delivery implements only the Manager-authorized S2 persistence slice for T-0143.
It adds durable stream-turn idempotency, its strict SQLite migration, and raw SQLite plus
SQLAlchemy-on-SQLite store parity. It does not add an `AgentStreamService`, SSE route, API endpoint,
runtime registry, admission-handle wiring, controller changes, frontend changes, or production
configuration. T-0143 remains `in_progress` and is not marked done here.

## Persistence Contract

`app/db/stream_turns.py` defines `StreamTurnPersistencePort` and these DTOs:

- `StreamTurnResolveOrReserve`
- `StreamTurnComplete`
- `StreamTurnFail`
- `StreamRequestRecord`
- `StreamTurnResolveOutcome`

The persistence request receives an already-computed fingerprint version and opaque digest. It stores
and compares those fields but does not calculate, normalize, log, or reinterpret the fingerprint
algorithm. `active_character_id` is persisted as the fallback-resolved character and is revalidated
against the durable Session; it never substitutes for the original requested character in a
fingerprint.

`resolve_or_reserve` starts a short writer transaction, revalidates Session owner and active
character, then atomically inserts the user message and `in_progress` idempotency row. A matching key
returns `in_progress`, completed replay, failed replay, or an orphan-recovered failed record; a
version/digest mismatch returns conflict without inserting another user message.

Replay rechecks owner and lifecycle before any assistant content is loaded. Ended Sessions permit only
same-fingerprint terminal replay. Archived Sessions always raise `SESSION_ARCHIVED`; cross-user access
does not inspect the idempotency row.

`complete` uses a short SQLite writer transaction and commits exactly one assistant message, the
latest-row relationship update, and `completed` state together. The shared pure helper applies and
clamps numeric deltas while preserving `current_scene`; mood, user emotion, and topic use the current
completion's policy output. Completion after an ended or archived Session records terminal failure and
does not write an assistant.

`fail` only performs `in_progress -> failed` terminalization and never writes partial assistant text.
The terminal row stores only code, retryability, and an allowlisted internal reason. The explicit,
indexed `fail_orphan_if_owned_by_other_runtime(..., limit=...)` operation marks a bounded batch of
foreign-runtime in-progress rows as `CHAT_STREAM_INTERRUPTED`; no runtime startup hook was added in
S2.

## Schema And Migration

`chat_request_idempotency` adds the accepted fields, lifecycle coherence checks, three foreign keys
(Session, user message, assistant message), unique key/turn indexes, and a bounded orphan lookup index.
No account foreign key was added, preserving existing Session ownership/backfill compatibility across
both stores.

The current migration id is `streaming_chat_idempotency_v0_2`. It freezes the persistence
reason vocabulary to provider-neutral categories: user cancellation/disconnection, runtime closure,
model failure or invalid response, preparation/runtime interruption, and Session lifecycle reasons.
Provider-specific parser reasons remain outside this persistence enum. `completed` hydration requires
exactly the integer `trust`, `affection`, and `familiarity` keys; `in_progress` and `failed` records
require the canonical empty delta object.

`ensure_streaming_chat_schema(...)` is incremental and idempotent. A strict v0.1 table is promoted
inside one SQLite writer transaction: its full v0.1 contract is verified, the replacement table is
created, every row is copied, `cancelled` and `runtime_shutdown` are canonically mapped to
`user_cancelled` and `runtime_closing`, then indexes and the v0.2 marker are committed together.
Invalid legacy data rolls the transaction back and raises `STORAGE_MIGRATION_REQUIRED`. After the v0.2
marker exists, every startup validates exact columns, defaults, check expressions, foreign keys, and
named index order/DDL. Both backends preflight an existing table before any schema initializer can
recreate a missing index or table, and fail closed on drift.

## Files

- `app/db/stream_turns.py`
- `app/db/streaming_chat_migrations.py`
- `app/db/repositories/stream_turns.py`
- `app/db/repositories/__init__.py`
- `app/db/schema.sql`
- `app/db/models.py`
- `app/db/session.py`
- `app/db/sqlite_store.py`
- `app/db/sqlalchemy_store.py`
- `tests/store_contract/test_streaming_chat_contract.py`
- `tests/test_streaming_chat_migrations.py`

## Validation

- `pytest -q tests/store_contract/test_streaming_chat_contract.py tests/test_streaming_chat_migrations.py`: 58 passed after the review repair.
- `pytest -q tests/store_contract`: passed.
- Focused Session, Browser Auth migration, S1, and S3 regression suite: passed.
- Full `pytest -q`: passed, with one existing skip and existing dependency/deprecation warnings.
- `ruff check app tests`: passed.
- `python -m compileall -q app tests`: passed.
- `ruff check .`: blocked only by the existing, out-of-scope
  `frontend/capture_frontend.py:17:1 I001`; frontend changes are prohibited for S2.
- `git diff --check`: passed with no whitespace errors; Git emitted only existing CRLF notices.

## Deferred To Later Slices

- Calling orphan recovery during runtime startup.
- `AgentStreamService` orchestration, admission lifecycle, provider streaming, SSE, API, and frontend.
- Mapping stream terminal codes to public HTTP/SSE events and ErrorCode additions.
- Any changes to `DialogueController`, synchronous completion semantics, Memory scheduling, or production profile.
