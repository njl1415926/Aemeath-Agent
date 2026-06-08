# Worker Session 4 Delivery: Session Contract v0.1

Date: 2026-06-08

Task: T-0112 Session Contract v0.1

Status: implementation complete, ready for Manager review

## Summary

Implemented Session as a first-class runtime object with:

- globally unique opaque session IDs;
- immutable user and character ownership;
- `active | ended | archived` lifecycle;
- create/get/list/end/resume/archive store operations;
- Controller ownership and lifecycle validation before business persistence;
- CLI current/new/list/end/resume/archive workflows;
- idempotent legacy SQLite backfill;
- exact message count and last-message read projections;
- unchanged Memory v0.3 extraction and recall policy.

## Modified Files

Added:

- `app/session/__init__.py`
- `app/session/schemas.py`
- `app/session/ports.py`
- `app/session/errors.py`
- `tests/test_session_store.py`
- `tests/test_session_runtime.py`
- `docs/agents_communication/session_v0_1_delivery.md`

Modified:

- `app/agent/dialogue_controller.py`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/main.py`
- `app/cli.py`
- `docs/agents_communication/session_v0_1_design_review.md`

No changes were required in `app/schemas.py` or `app/agent/message_store.py`.
Manager-owned architecture, interface, and task documents were not modified by this worker after
the Manager synchronized them.

## Interfaces

Added public types:

- `SessionStatus = Literal["active", "ended", "archived"]`
- `SessionRecord`
- `SessionStorePort`

Added store operations:

- `ensure_session(...)`
- `create_session(...)`
- `get_session(...)`
- `list_sessions(...)`
- `end_session(...)`
- `resume_session(...)`
- `archive_session(...)`

Added typed errors:

- `SessionError`
- `SessionNotFoundError`
- `SessionOwnershipError`
- `SessionEndedError`
- `SessionArchivedError`
- `SessionTransitionError`
- `SessionMigrationError`

`AgentTurnInput` remains unchanged.

## Runtime Behavior

`DialogueController` resolves the active character and then calls `ensure_session(...)` before:

- `turn_started`;
- message writes;
- relationship reads or updates;
- prompt-run logs;
- memory extraction jobs.

Validation uses the resolved active character ID, so Session ownership matches relationship state,
prompt logs, and Memory v0.3 job provenance.

Validation failures may emit the redacted `session_validation_failed` trace event, then re-raise
the typed Session error. They do not persist messages, prompt logs, relationship changes, or
memory jobs.

## CLI

Added:

```text
/session current
/session new
/session list
/session resume <session_id>
/session end [session_id]
/session archive [session_id]
```

Startup creates and prints a new current session.

`/session new` creates and selects a new active session without mutating the previous session.
`/session end` is the explicit ended transition. Resuming an ended session reactivates it and
prints recent message history. Archived sessions are not resumable in v0.1.

Ending or archiving the currently selected session creates a fresh current session so subsequent
dialogue and memory commands always use an active session.

## Persistence And Migration

Physical `sessions` columns:

```text
id
user_id
character_id
status
title
metadata_json
created_at
updated_at
ended_at
archived_at
```

SQLite derives these `SessionRecord` read-model fields from `messages`:

- `message_count = COUNT(messages.id)`
- `last_message_at = MAX(messages.created_at)`
- effective `updated_at = max(lifecycle updated_at, last_message_at)`

`MessageStore.save_message` does not maintain Session counters, and Controller does not coordinate
cross-port writes.

Startup migration:

- backfills distinct message sessions using the legacy default character `aemeath`;
- derives creation/update timestamps from message history;
- preserves jobs-only Memory v0.3 session provenance;
- treats an existing Session row as authoritative for character ownership on restart;
- uses Memory job character provenance before the legacy message-only default when creating a
  missing Session;
- uses insert-if-missing behavior and is idempotent;
- never rewrites or deletes messages or extraction jobs;
- rejects mixed users, conflicting job ownership, or existing Session provenance conflicts with
  `SessionMigrationError`;
- rolls back Session backfill on ambiguity while preserving legacy rows.

No SQLite foreign key was added to messages or memory jobs.

## Memory v0.3 Compatibility

Memory extraction, embedding, and recall code was not changed.

The runtime passes the same:

```text
user_id
session_id
resolved character_id
```

to Session validation, messages, and hard/window Memory extraction jobs. Existing job
idempotency keys and payloads remain unchanged. Long-term memory remains user-scoped across
sessions; Session is provenance and dialogue continuity, not a mandatory memory isolation key.

## Tests

Coverage includes:

- create/get/list/read projection;
- idempotent ensure;
- cross-user and cross-character rejection;
- active/ended/resumed/archived transitions;
- archived non-resumability;
- direct `AgentTurnInput` compatibility;
- rejection without message, prompt-log, or memory-job side effects;
- exact message count and last-message time;
- resumed history in prompt context and CLI output;
- `/session new` leaving the old session active;
- legacy messages-only migration and idempotence;
- mixed-owner rollback without message loss;
- jobs-only Memory provenance migration;
- non-default-character Session restart preservation;
- conflicting job owners for a missing Session;
- job ownership conflicting with an existing Session;
- message user conflicting with an existing Session;
- Memory window-job provenance matching Session ownership.

Validation:

```text
pytest: 86 passed
ruff check .: All checks passed
```

## Known Risks And Follow-ups

- Session list projection aggregates messages at read time. This is exact and suitable for v0.1,
  but a high-volume adapter may later materialize the projection behind the same port.
- Message-only legacy migration must use the contract default character `aemeath` because old
  messages do not contain character identity.
- Pre-release experimental `sessions` tables whose SQLite CHECK constraint only permits
  `active | archived` are unsupported. No released project version contains that schema; a
  proper table-rebuild migration is required if such compatibility becomes necessary.
- Lifecycle audit history is intentionally not retained across repeated end/resume cycles. A
  future requirement should add a `session_events` stream instead of more timestamp fields.
- CLI currently lists active sessions only. Archived/ended browsing and API pagination remain
  deferred.
