# Worker Session 4 Design Review: Session Contract v0.1

Date: 2026-06-08

Task: T-0112 Session Contract v0.1

Status: approved with Manager adjustments, implementation in progress

Manager decision:

- `docs/agents_communication/session_v0_1_contract_change_decision.md`

## 1. Scope And Baseline

This review follows the locked contract in `docs/session_contract.md`. No implementation code has
been changed in this phase.

Current runtime observations:

- `AgentTurnInput` already carries flat `user_id`, `session_id`, and `character_id` fields.
- `DialogueController` currently writes messages without first validating session ownership.
- `SQLiteStore` implements message, memory, memory-job, and relationship persistence in one
  adapter.
- Schema initialization uses `CREATE TABLE IF NOT EXISTS` plus explicit Python migration helpers.
- The CLI generates one transient session ID at startup and has no session commands.
- Memory v0.3 creates hard/window extraction jobs after both user and assistant messages have been
  persisted. Job identity includes `user_id`, `session_id`, and `character_id`.
- The checked `data/aemeath.db` currently has 54 messages in 10 sessions, no extraction jobs, and
  no session IDs containing messages from multiple users.

## 2. Session Module

Planned package:

```text
app/session/
  __init__.py
  schemas.py
  ports.py
  errors.py
```

`app/session/schemas.py` will define the locked public schema:

```python
SessionStatus = Literal["active", "ended", "archived"]

@dataclass(frozen=True)
class SessionRecord:
    id: str
    user_id: str
    character_id: str
    status: SessionStatus
    title: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    ended_at: str | None
    archived_at: str | None
    last_message_at: str | None
    message_count: int
```

`app/session/ports.py` will contain `SessionStorePort` with the exact methods and defaults from
`docs/session_contract.md`. `SQLiteStore` will implement this port alongside its existing store
ports.

Planned typed errors:

- `SessionError`: base error.
- `SessionNotFoundError`: requested session does not exist.
- `SessionOwnershipError`: an existing ID belongs to another user or character.
- `SessionEndedError`: an ended session is used without explicit resume.
- `SessionArchivedError`: an archived session is used as an active session.
- `SessionMigrationError`: legacy rows cannot be assigned one unambiguous owner.

`ensure_session` behavior:

1. Atomically insert the supplied ID if it does not exist.
2. Read the resulting row.
3. Require exact `user_id` and `character_id` equality.
4. Require `status == "active"`.
5. Return the `SessionRecord`.

The insert/read sequence will be transaction-safe so two compatible callers racing on the same ID
converge on one row. Metadata is used only when creating a missing session; an idempotent
`ensure_session` call will not silently merge or overwrite existing metadata.

`create_session` will generate an opaque globally unique ID with the existing ID helper, insert an
active record, and return it. Callers must treat the generated value as opaque even if the current
implementation includes a diagnostic prefix.

`archive_session` will:

- raise `SessionNotFoundError` for an unknown ID;
- raise `SessionOwnershipError` for a different user;
- be idempotent for the owning user if the session is already archived;
- accept active or ended sessions;
- update `status`, `archived_at`, and `updated_at` without deleting messages or jobs.

`end_session` will explicitly move an active session to ended, set `ended_at`, and be idempotent
for an already ended session owned by the caller. `resume_session` will only accept an ended
session with matching user and character, move it to active, and clear `ended_at`. Archived
sessions have no outgoing transition in v0.1.

## 3. Direct AgentTurnInput Compatibility

`AgentTurnInput` will remain unchanged. Existing tests and API-style callers may continue to
construct it directly with an arbitrary new `session_id`.

`DialogueController` will receive an injected `SessionStorePort` and call:

```python
session_store.ensure_session(
    session_id=turn_input.session_id,
    user_id=turn_input.user_id,
    character_id=active_character_id,
)
```

The character provider is currently allowed to fall back from an unknown requested character to
the configured default. Session ownership should therefore use the resolved
`active_character_id`, not an unavailable requested ID. This keeps the session row, prompt log,
relationship state, and Memory v0.3 jobs on the same actual character identity.

The validation occurs before `turn_started`, message writes, prompt logs, state updates, or memory
job enqueue. A rejected reuse therefore has no dialogue or memory persistence side effects.

The controller will pass no arbitrary turn metadata to compatibility-created sessions. This avoids
persisting API tokens or other secrets from `AgentTurnInput.metadata`. The CLI may explicitly use
the safe metadata `{"client": "cli"}` when it creates a session.

## 4. SQLite Schema And Migration

Planned table:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    archived_at TEXT
);
```

Planned indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_sessions_user_status_updated
    ON sessions (user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_user_character_status_updated
    ON sessions (user_id, character_id, status, updated_at DESC);
```

Migration will run during `SQLiteStore._init_db()` after schema creation and in the same
transactional initialization path as existing migrations.

For each distinct legacy message `session_id`:

- owner is the single distinct `messages.user_id`;
- character is the configured legacy default, currently `aemeath`;
- `created_at` is `MIN(messages.created_at)`;
- persisted `updated_at` is `MAX(messages.created_at)`;
- status is `active`, title is null, and metadata is `{}`.

`message_count` and `last_message_at` are not physical v0.1 session columns. Session reads use
aggregate SQL over `messages` to derive exact values, and expose effective `updated_at` as the
newer of the persisted lifecycle timestamp and `last_message_at`.

For a memory-job session with no message row, migration may create a session from the job's
existing `user_id`, `character_id`, and timestamps. This preserves already queued provenance
without rewriting the job.

Migration provenance priority is:

```text
existing sessions row > memory job user/character > message user + legacy default character
```

An existing Session row is authoritative for character ownership. Message rows validate its user
only because legacy messages have no character field. For a missing Session, Memory jobs provide
stronger character provenance than the message-only `aemeath` default.

Migration safety rules:

- Use insert-if-missing semantics and verify existing session ownership, making repeated startup
  idempotent.
- Never update, delete, or rebuild `messages` or `memory_extraction_jobs`.
- Preserve an existing session's status, title, metadata, and timestamps on later startups.
- Reject ambiguous legacy data rather than guessing an owner.

Ambiguous data includes:

- one message session containing more than one distinct `user_id`;
- one job session containing more than one distinct `user_id + character_id`;
- message user and job user disagreeing for the same missing session;
- an existing `sessions` row disagreeing with the legacy provenance being backfilled.

Such a conflict raises `SessionMigrationError` and rolls back the session backfill transaction.
All legacy messages and jobs remain untouched for diagnosis. This is preferable to silently
assigning cross-user messages to one owner.

No foreign key will be added to `messages` or `memory_extraction_jobs` in v0.1.

## 5. Ownership Rejection

For an existing session, `ensure_session` compares both immutable ownership fields:

```text
stored user != supplied user          -> SessionOwnershipError
stored character != supplied character -> SessionOwnershipError
stored status == ended                -> SessionEndedError
stored status == archived             -> SessionArchivedError
```

The check is performed inside the session adapter, not in `DialogueController`.

`MessageStore` read methods remain keyed only by `session_id` for compatibility. They may only be
called after session validation in Controller and CLI flows. This preserves the current interface
while preventing cross-user history reads in the supported runtime paths.

## 6. CLI Workflow

Startup:

- create a new active session for `Settings.default_user_id` and
  `Settings.default_character_id`;
- set it as current;
- print its ID.

Commands:

- `/session current`: show the current session ID, character, status, and last-message time.
- `/session new`: create and switch to a new active session; do not archive the previous session.
- `/session list`: list active sessions owned by the current user, newest updated first.
- `/session resume <session_id>`: explicitly reactivate an ended owned session, switch to it, then
  read and display recent history through `MessageStore`.
- `/session end [session_id]`: explicitly end the supplied or current active session.
- `/session archive [session_id]`: archive the supplied session or the current session.

The v0.1 CLI remains single-character and resumes only sessions owned by the current user and
configured default character. It will not display another user's session metadata or messages.

If the current session is ended or archived, the CLI will immediately create and switch to a fresh
session so normal input and `/memory` commands always have a valid active session. Ending or
archiving a non-current session does not change the current selection.

`/session list` uses the port's default active filter. Archived sessions remain persisted but are
not shown by this v0.1 command.

## 7. Controller And Store Call Order

Planned turn order:

```text
resolve active character
SessionStore.ensure_session
record turn_started
build character runtime context
MessageStore.save_message(user)
MessageStore.get_recent_messages
read relationship / memory / evidence
build prompt and call model
MessageStore.save_message(assistant)
update relationship state
classify memory write
enqueue hard memory job
enqueue window memory job
record trace and prompt log
return AgentTurnOutput
```

No session mutation follows message writes. `message_count`, `last_message_at`, and effective
`updated_at` are derived from `messages` when the session read model is queried. `MessageStore`
remains a message persistence port and does not maintain Session counters.

## 8. Memory v0.3 Compatibility

The Session implementation will not modify Memory extraction, embedding, recall, or long-term
scope policy.

Provenance is preserved by these invariants:

- Controller validates/creates the session before any message or job exists.
- Session ownership uses the same resolved `active_character_id` passed to
  `persist_decision(...)` and `enqueue_window_if_due(...)`.
- The same `turn_input.user_id` and `turn_input.session_id` are passed to the session store,
  messages, and both memory job paths.
- Long-term memory retrieval remains user-scoped and can cross sessions.
- `session_id` remains a source/window/audit field and is not introduced as a mandatory
  long-term-memory isolation key.
- Session migration does not rewrite `memory_extraction_jobs`, their idempotency keys, source
  message IDs, or payloads.

Direct low-level Memory v0.3 tests may still enqueue jobs without first creating a session because
v0.1 intentionally adds no physical foreign key. The supported dialogue runtime, however, always
ensures a session first.

## 9. Planned Files

Implementation files:

- add `app/session/__init__.py`
- add `app/session/schemas.py`
- add `app/session/ports.py`
- add `app/session/errors.py`
- modify `app/agent/dialogue_controller.py`
- modify `app/db/schema.sql`
- modify `app/db/sqlite_store.py`
- modify `app/main.py`
- modify `app/cli.py`

`app/agent/message_store.py` and `app/schemas.py` are not expected to require interface changes.
They will only be changed if implementation reveals a compatibility need.

Manager-owned `docs/architecture.md`, `docs/interfaces.md`, and `docs/task_log.md` will not be
edited by this worker. Any final documentation requests will be recorded in the delivery report.

## 10. Planned Tests

Store tests:

- create/get round trip, including metadata JSON;
- list by user, optional character, status, limit, and updated ordering;
- archive behavior and archived `ensure_session` rejection;
- matching `ensure_session` is idempotent;
- cross-user and cross-character reuse raise `SessionOwnershipError`;
- generated IDs are unique and treated as opaque.

Migration tests:

- backfill an old messages-only database;
- derive first/last timestamps correctly;
- run initialization twice without duplicate or changed sessions;
- prove message count, IDs, content, and timestamps are unchanged;
- preserve jobs-only provenance;
- reject mixed-user or conflicting job provenance without deleting legacy rows.

Controller/runtime tests:

- a direct `AgentTurnInput` creates its missing session;
- an existing compatible session remains usable;
- rejected ownership creates no message, prompt log, or memory job;
- session record, messages, prompt log, and Memory v0.3 job share the same
  `user_id/session_id/character_id`;
- `last_message_at` advances after persisted messages;
- resumed history is included in the next turn's recent conversation.

CLI tests:

- startup and `/session current`;
- `/session new` switches without archiving the old session;
- `/session list` only exposes the current user's active sessions;
- `/session end [id]` performs the explicit ended transition;
- `/session resume <id>` reactivates an ended session and reads history;
- cross-user, cross-character, missing, and archived resume are rejected;
- `/session archive` supports explicit and current IDs and leaves a valid current session.

Final validation after approved implementation:

```bash
pytest
ruff check .
```

## 11. Manager Decision Applied

The approved contract uses `active | ended | archived`, adds explicit `end_session` and
`resume_session`, and treats archived sessions as non-resumable in v0.1. `/session new` only
changes CLI selection and does not mutate the previous session's lifecycle.

Session ownership follows the resolved active character after the existing character fallback.
The SQLite adapter derives `message_count`, `last_message_at`, and effective `updated_at` from
messages without Controller-managed counters or cross-port write coupling.
