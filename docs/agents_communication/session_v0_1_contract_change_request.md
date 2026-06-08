# Worker Session 4 Contract Change Request: Session Lifecycle Fields

Date: 2026-06-08

Task: T-0112 Session Contract v0.1

Status: awaiting Manager Session decision

Related design:

- `docs/session_contract.md`
- `docs/agents_communication/session_v0_1_design_review.md`

This request does not modify the submitted design review or implementation code. It records a
proposed extension discussed after the initial design was submitted.

## 1. Motivation

The locked contract currently models:

```text
status = active | archived
created_at
updated_at
last_message_at
```

This is sufficient for basic create/resume/archive behavior, but it does not distinguish:

- a conversation that has naturally ended and may later be resumed;
- a conversation explicitly archived by the user;
- the time at which either lifecycle event occurred;
- an efficiently queryable message count for CLI/API session lists.

The proposal is to add objective lifecycle and aggregate fields while keeping Session separate
from long-term Memory.

## 2. Proposed SessionRecord

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

The proposal retains `metadata`; non-core attributes such as `client`, `channel`, or
`created_by=migration` should remain metadata rather than new top-level columns.

## 3. Field Semantics

Identity:

- `id`: globally unique, stable, opaque primary key.
- `user_id`: immutable owner.
- `character_id`: immutable v0.1 character owner.

Lifecycle:

- `status=active`: accepts new turns.
- `status=ended`: continuous conversation has stopped, but explicit resume is allowed.
- `status=archived`: intentionally archived and unavailable for normal turn handling.
- `ended_at`: set when entering `ended`; null while active.
- `archived_at`: set when entering `archived`; null until archived.

Content and aggregates:

- `title`: optional user/client supplied title; no LLM title generation in v0.1.
- `metadata`: JSON-compatible non-secret extension data.
- `last_message_at`: timestamp of the most recently persisted message.
- `message_count`: count of successfully persisted user and assistant messages in the session.

General timestamps:

- `created_at`: immutable creation timestamp.
- `updated_at`: changes when lifecycle, title, metadata, or message aggregate fields change.
- all timestamps use UTC ISO 8601 strings, matching the current project convention.

Consistency rules:

```text
active   -> ended_at is null
ended    -> ended_at is not null
archived -> archived_at is not null
message_count >= 0
```

When an ended session is resumed, the recommendation is to clear `ended_at`. It describes the
current lifecycle state, not a complete event history. Multiple end/resume cycles would require a
future session event log if audit history becomes necessary.

If an ended session is archived, `ended_at` may be retained as historical information while
`archived_at` records the later archive action.

## 4. Proposed State Transitions

```text
active -> ended
ended -> active
active -> archived
ended -> archived
archived -> no v0.1 transition
```

Recommended CLI meaning:

- startup creates an `active` session;
- `/session new` marks the current active session `ended`, then creates a new active session;
- `/session resume <session_id>` explicitly reactivates an `ended` session;
- `/session archive [session_id]` archives an active or ended session;
- archived sessions cannot be resumed in v0.1.

Direct `AgentTurnInput` compatibility should not silently reactivate an ended session.
`ensure_session` should:

- create a missing session as active;
- accept an existing active session with matching ownership;
- reject an ended session with a typed lifecycle error;
- reject an archived session with `SessionArchivedError`;
- reject ownership mismatch with `SessionOwnershipError`.

An explicit CLI/API resume operation should use a dedicated store method rather than overloading
`ensure_session`.

## 5. Proposed Port Changes

The existing locked port does not expose an `ended` transition or explicit resume operation.
If Manager accepts `ended`, the recommended additions are:

```python
def end_session(
    self,
    *,
    session_id: str,
    user_id: str,
) -> SessionRecord:
    ...

def resume_session(
    self,
    *,
    session_id: str,
    user_id: str,
    character_id: str,
) -> SessionRecord:
    ...
```

Recommended behavior:

- `end_session` is idempotent for an already ended session owned by the user.
- `resume_session` only accepts `ended`, matching user, and matching character.
- neither method changes `user_id` or `character_id`.
- neither method deletes messages, prompt logs, or Memory extraction jobs.
- `archive_session` accepts active or ended sessions and remains idempotent for the owner.

A new typed `SessionEndedError` is recommended for accidental turn handling against an ended
session.

## 6. Proposed SQLite Columns

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('active', 'ended', 'archived')),
    title TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    archived_at TEXT,
    last_message_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0
        CHECK (message_count >= 0)
);
```

The two previously planned indexes remain sufficient:

```text
(user_id, status, updated_at DESC)
(user_id, character_id, status, updated_at DESC)
```

No index on `message_count`, `ended_at`, or `archived_at` is proposed until an actual query
requires one.

## 7. Message Count Consistency

`message_count` is a denormalized aggregate. It is useful for session list views, but a separate
Controller sequence such as:

```text
MessageStore.save_message
SessionStore.touch_session
```

cannot guarantee exact count consistency if the process fails between calls.

Recommended invariant:

- the persistence adapter that writes a message also updates `sessions.message_count`,
  `last_message_at`, and `updated_at` in the same database transaction;
- Controller still ensures session ownership before the first message write;
- Controller does not calculate or increment counts;
- another storage adapter must provide the same atomic persistence guarantee internally.

For `SQLiteStore`, this can be implemented inside `save_message` because the adapter owns both
tables. This is an adapter implementation detail and does not make Controller depend on SQLite.
The existing `touch_session` port can remain for explicit timestamp repair or adapters that need
it, but it must not increment `message_count`.

Alternative if Manager does not want this implicit cross-table responsibility:

- omit persisted `message_count` in v0.1;
- calculate counts with an aggregate query when listing sessions;
- add a transactional conversation persistence port in a later reviewed contract.

The worker recommends the atomic adapter-maintained field because the current SQLite adapter
already owns both persistence concerns and the field is useful to future API/UI consumers.

## 8. Migration Changes

Legacy backfill would additionally derive:

```text
message_count   = COUNT(messages.id)
last_message_at = MAX(messages.created_at)
created_at      = MIN(messages.created_at)
updated_at      = MAX(messages.created_at)
ended_at        = null
archived_at     = null
status          = active
```

Backfill remains idempotent and must not modify or delete existing messages.

For sessions created only from existing Memory v0.3 jobs:

```text
message_count   = 0
last_message_at = null
status          = active
```

The provenance conflict rules in the submitted design review remain unchanged.

An optional reconciliation query/test should compare persisted `message_count` with
`COUNT(messages.id)`. No general repair command is required in v0.1.

## 9. Memory v0.3 Impact

The proposal does not change Memory v0.3 scope or job payloads:

- jobs retain their current `user_id`, `session_id`, and `character_id`;
- ending or archiving a session does not cancel existing extraction jobs;
- long-term memory remains user-scoped across sessions;
- `message_count` counts dialogue messages only, not memories or extraction jobs;
- resume continues the same session provenance and message window.

Window extraction behavior after resume remains unchanged: unprocessed messages in the same
session continue contributing to that session's extraction window.

## 10. Additional Test Requirements

If approved, implementation tests should also cover:

- active/ended/archived state transitions;
- `ended_at` and `archived_at` timestamp invariants;
- explicit resume of ended sessions;
- rejection of direct turns against ended sessions;
- archived sessions cannot be resumed;
- `/session new` ends the previous current session;
- message insert and count/timestamp update commit together;
- failed message insert does not increment the count;
- migration derives the exact legacy count;
- count remains correct across resume;
- archiving or ending does not change message count or Memory jobs.

## 11. Manager Decisions Requested

1. Approve or reject expanding `SessionStatus` from `active | archived` to
   `active | ended | archived`.
2. Approve or reject `ended_at`, `archived_at`, and `message_count` as public
   `SessionRecord` fields.
3. Confirm whether `/session new` should end the previous current session.
4. Approve or reject `end_session(...)` and `resume_session(...)` additions to
   `SessionStorePort`.
5. Confirm that archived sessions are not resumable in v0.1.
6. Approve adapter-atomic maintenance of `message_count`, `last_message_at`, and `updated_at`
   during message insertion.

Worker Session 4 will wait for Manager approval before updating the design review or beginning
implementation.
