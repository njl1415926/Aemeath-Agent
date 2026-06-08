# Memory Worker Session 3 Delivery: Scope, Provenance, And Session Permissions

Date: 2026-06-08

Status: implementation complete, ready for Manager review

## Summary

- Added read-only `SessionStorePort.require_session(...)`.
- `/memory extract` validates `session_id + user_id + character_id` before reading messages.
- Active and ended Sessions are accepted; archived Sessions are rejected.
- Removed Memory-side default character guessing.
- Memory writer inputs now require explicit `session_id` and `character_id`.
- Source messages are checked against the current user and Session.
- `/memory delete` and `/memory disable` use owner-scoped atomic updates.
- Split memory visibility fields from provenance fields.
- Unified keyword, vector, and fallback scope visibility.
- Exact dedupe matches the target scope tuple only.
- Added a transactional, versioned legacy memory migration.

## Schema

Added to `long_term_memories`:

```text
scope
scope_character_id
scope_session_id
source_session_id
source_job_id
source_message_ids_json
```

Added migration registry:

```text
schema_migrations
```

Migration version:

```text
memory_scope_provenance_v1
```

Existing memories migrate to `user_global`. Known message IDs move to
`source_message_ids_json`; known memory IDs remain in `related_memory_ids`. Source Session and
completed extraction job provenance are inferred where available. Migration changes and the
version marker commit in one transaction.

Unknown legacy references remain in `related_memory_ids` and emit an ID-only migration warning.

## Scope Contract

```text
user_global: same user
character:   same user and character
session:     same user, character, and Session
```

`/memory list` remains an owner management view over all active memories. Dialogue recall uses
the current `MemoryQuery` visibility and does not reuse the management-list semantics.

Automatic extraction continues to create `user_global` memories. No scope classifier was added.

## Interface Changes

- `SessionStorePort.require_session(...) -> SessionRecord`
- `MemoryQuery.session_id: str | None`
- `MemoryWriterPort.persist_decision(...)` requires `session_id` and `character_id`
- `MemoryStore.archive_memory(memory_id, user_id) -> bool`
- `MemoryStore.disable_memory(memory_id, user_id) -> bool`
- Recall store methods receive query `character_id` and `session_id`
- Exact-dedupe receives target `scope`, `scope_character_id`, and `scope_session_id`
- `MemoryRecord` exposes scope and provenance fields

## Main Files

- `app/session/ports.py`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/memory/store.py`
- `app/memory/schemas.py`
- `app/memory/admin.py`
- `app/memory/writer.py`
- `app/memory/extractor.py`
- `app/memory/reader.py`
- `app/agent/ports.py`
- `app/agent/dialogue_controller.py`
- `app/schemas.py`
- `tests/test_memory_scope_and_permissions.py`

## Validation

```text
pytest
95 passed

ruff check .
All checks passed!

pytest -q tests/test_chinese_encoding_and_behavior.py
4 passed
```

## Deferred

- Session-ended tail extraction and lifecycle events.
- Automatic scope classification.
- SessionStore-to-Memory dependencies.
- Physical foreign keys for memory provenance.
