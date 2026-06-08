# Manager Review: Session Contract v0.1 Delivery

Date: 2026-06-08

Task: T-0112 Session Contract v0.1

Decision: accepted after remediation

## Final Resolution

Worker Session 4 addressed the blocking migration defect and added the requested regression
coverage.

Verified on 2026-06-08:

- an existing Session row is authoritative for character ownership;
- a non-default-character Session survives SQLiteStore restart after messages are written;
- missing Sessions use Memory-job character provenance before the message-only legacy default;
- conflicting job owners, job/existing-Session ownership, and message/existing-Session ownership
  are rejected;
- `pytest`: 86 passed;
- `ruff check .`: passed.

T-0112 is accepted. The findings below are retained as the review history that led to the
remediation.

## Summary

The public Session contract, lifecycle transitions, Controller validation order, CLI workflow,
and read-model aggregation generally match the approved Manager decision. The full suite currently
passes with 82 tests and `ruff check .` passes.

One restart-blocking migration defect must be fixed before T-0112 can be accepted.

## Blocking Finding

### Existing non-default-character sessions are misclassified as legacy on restart

Location:

- `app/db/sqlite_store.py`, `_backfill_sessions`

Current backfill assigns every session found in `messages` the legacy character ID `aemeath`,
including sessions that already have an authoritative row in `sessions`.

Reproduction:

1. Create a session with `character_id="other_character"`.
2. Save one message in that session.
3. Construct `SQLiteStore` again for the same database.
4. Startup raises `SessionMigrationError` because message provenance is interpreted as
   `aemeath` and conflicts with the existing Session row.

This breaks the promised multi-character extension boundary and makes a valid database unable to
restart.

Required behavior:

- If a `sessions` row already exists, it is authoritative for `character_id`.
- Message rows may validate the existing Session's `user_id`, but must not overwrite or infer its
  character from the legacy default.
- If Memory jobs exist for an existing Session, validate both their user and character against
  the authoritative Session row.
- Use the legacy default character only when creating a missing Session from messages that have
  no stronger character provenance.
- Backfill must remain idempotent and continue rejecting genuine cross-user or provenance
  conflicts.

Required regression test:

- create a non-default-character Session;
- save messages;
- reopen the same SQLite database;
- assert startup succeeds and ownership, messages, count, and timestamps remain unchanged.

## Additional Required Tests

The delivery report states that conflicting Memory-job ownership and existing Session provenance
conflicts are covered, but the submitted Session tests currently cover only mixed message owners
and a valid jobs-only backfill.

Add focused migration tests for:

- multiple `user_id + character_id` owners in jobs for one missing Session;
- a Memory job whose ownership conflicts with an existing Session;
- a message owner conflicting with an existing Session user.

## Compatibility Follow-up

`_migrate_session_columns` adds `ended_at` and `archived_at`, but it does not migrate an existing
two-state table whose CHECK constraint allows only `active | archived`. Such a table fails on
`end_session` with a SQLite integrity error.

No released project version currently contains that table, so this is not a T-0112 blocker.
Either:

- explicitly document that pre-release experimental Session tables are unsupported; or
- rebuild the table in a proper schema migration before Session is released.

Do not add an ad hoc SQL-string replacement.

## Acceptance Status

Confirmed:

- `SessionRecord` and `SessionStorePort` match the approved contract.
- `active | ended | archived` transitions are implemented.
- archived sessions are not resumable.
- `/session new` does not end the previous Session.
- Controller validates Session before business persistence.
- `message_count` and `last_message_at` are derived from messages.
- Memory v0.3 provenance is preserved in the normal runtime path.
- `pytest`: 82 passed.
- `ruff check .`: passed.

This condition was satisfied by the revised delivery. The full suite and manual
non-default-character restart scenario now pass.
