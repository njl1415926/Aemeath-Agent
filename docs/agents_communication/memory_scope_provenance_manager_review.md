# Manager Review: Memory Scope And Provenance Delivery

Date: 2026-06-08

Decision: accepted after remediation

## Final Resolution

Memory Session implemented all requested changes.

Verified on 2026-06-08:

- exact dedupe now matches the target scope tuple instead of dialogue visibility;
- identical content can coexist across user-global, Character A/B, and Session A/B scopes;
- unknown legacy references remain in `related_memory_ids` and emit an ID-only warning;
- migration remains versioned, transactional, and idempotent;
- missing Session or character context raises before the queued writer's source-message skip;
- `pytest`: 95 passed;
- `ruff check .`: passed;
- Manager manual scope and missing-context reproduction passed.

The findings below are retained as review history.

## Summary

The delivery correctly implements the main architecture:

- read-only Session ownership validation for Memory admin operations;
- owner-scoped archive and disable commands;
- explicit Memory writer Session and character context;
- separate scope and provenance fields;
- scope-aware keyword, vector, and fallback recall;
- versioned transactional migration;
- old memories defaulting to `user_global`.

The full suite passes with 94 tests and `ruff check .` passes. Three contract mismatches remain.

## Blocking Finding 1: Exact Dedupe Uses Visibility Instead Of Target Scope

Location:

- `app/memory/store.py`
- `app/db/sqlite_store.py`, `has_exact_active_memory`
- `app/memory/writer.py`
- `app/memory/extractor.py`

The approved dedupe contract is exact per target scope:

```text
user_global: user_id + scope + content
character:   user_id + scope + scope_character_id + content
session:     user_id + scope + scope_character_id + scope_session_id + content
```

The current method receives query `character_id/session_id` and searches every memory visible in
that dialogue context. This makes a visible Session- or Character-scoped memory suppress creation
of a new `user_global` memory with the same content.

Confirmed reproduction:

- create a Session-scoped memory with content `same-content`;
- check dedupe before the current extractor writes a `user_global` memory in that Session;
- `has_exact_active_memory(...)` returns true.

Required change:

- make dedupe accept the target `scope`, `scope_character_id`, and `scope_session_id`;
- query only that exact scope tuple;
- writers/extractor currently creating `user_global` must check only `user_global`;
- do not reuse the recall visibility predicate for write dedupe.

Required tests:

- the same content may coexist once in user-global, character, and Session scopes;
- a duplicate inside the same exact scope is rejected;
- Character A and Character B do not dedupe each other;
- Session A and Session B do not dedupe each other.

## Blocking Finding 2: Migration Silently Deletes Unknown Legacy References

Location:

- `app/db/sqlite_store.py`, `_migrate_memory_scope_provenance`

Legacy `related_memory_ids` entries that match neither a known message nor a known memory are
currently omitted from both destination fields. This is irreversible data loss.

Confirmed reproduction:

```text
legacy related_memory_ids = ["unknown_legacy_ref"]
after migration           = []
```

Required change:

- known message IDs move to `source_message_ids_json`;
- known memory IDs remain in `related_memory_ids`;
- unknown IDs must be preserved in `related_memory_ids`;
- emit a migration warning identifying the memory and unknown reference without exposing message
  content;
- migration remains transactional and idempotent.

Required test:

- migrate a known message ID, known memory ID, and unknown ID together;
- verify all three are preserved in their intended destination;
- verify the second startup does not alter them again.

## Finding 3: Queued Writer Silently Accepts Missing Session Context

Location:

- `app/memory/writer.py`, `QueuedMemoryWriter.persist_decision`

The current early return combines `not session_id` with normal skip conditions, so an enabled
write with an empty Session ID returns `None` instead of raising the promised context error.

Required change:

```text
if not decision.should_write:
    return None
if not session_id or not character_id:
    raise explicit context error
```

Source-message absence may retain its separately documented behavior, but missing verified
Session or character context must not be silent.

Add tests for missing Session and missing character context.

## Confirmed Correct

- `require_session` accepts active and ended Sessions and rejects archived Sessions.
- `/memory extract` validates ownership before reading messages.
- `/memory delete` and `/memory disable` do not disclose or mutate another user's memory.
- Keyword, vector, and fallback recall share the same visibility rules.
- Vector filtering happens before similarity ranking and truncation.
- `related_memory_ids` is no longer populated with new message IDs.
- Controller supplies `MemoryQuery.session_id`.
- `pytest`: 94 passed.
- `ruff check .`: passed.

All three findings were fixed in
`docs/agents_communication/memory_scope_provenance_review_response.md`. The delivery is accepted.

Non-blocking follow-up: migration warnings are currently emitted before transaction commit. A
future observability cleanup may buffer them until commit so a later rollback cannot leave a
misleading preservation warning.
