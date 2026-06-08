# Memory Worker Response: Scope And Provenance Manager Review

Date: 2026-06-08

Status: all requested changes implemented

## Fixes

1. Exact dedupe now matches the target scope tuple:

```text
user_id
scope
scope_character_id
scope_session_id
content
```

Recall visibility is no longer reused for write dedupe. Tests cover coexistence and duplicate
detection across user-global, Character A/B, and Session A/B scopes, including the reported
Session-scoped-memory-before-user-global reproduction.

2. Migration preserves unknown legacy references.

- Known message IDs move to `source_message_ids_json`.
- Known memory IDs remain in `related_memory_ids`.
- Unknown IDs also remain in `related_memory_ids`.
- Unknown IDs emit a `RuntimeWarning` containing only memory/reference IDs.
- The migration remains transactional, versioned, and idempotent.

3. `QueuedMemoryWriter.persist_decision(...)` validates context before source-message skip.

- `should_write=False` still returns `None`.
- Missing `session_id` or `character_id` raises the explicit context error.
- Missing source message ID may still return `None`.

## Validation

```text
pytest
95 passed

ruff check .
All checks passed!

git diff --check
No whitespace errors; only existing Windows line-ending warnings.
```
