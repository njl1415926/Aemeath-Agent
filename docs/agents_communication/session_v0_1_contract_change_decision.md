# Manager Decision: Session Lifecycle Contract Change

Date: 2026-06-08

Task: T-0112 Session Contract v0.1

Decision: conditionally approved with the following binding adjustments.

## Decisions

1. Approve `SessionStatus = active | ended | archived`.
2. Approve `ended_at`, `archived_at`, `last_message_at`, and `message_count` as public
   `SessionRecord` fields.
3. Do not make `/session new` end the previous session. CLI selection is client state, while
   `ended` is durable domain state. Add `/session end [session_id]` for the explicit transition.
4. Approve `end_session(...)` and `resume_session(...)`. Lifecycle transition methods return the
   updated `SessionRecord`; `archive_session(...)` should do the same.
5. Confirm that archived sessions cannot be resumed in v0.1.
6. Do not make adapter-atomic counter maintenance part of the v0.1 cross-port contract.
   `message_count` and `last_message_at` are read-model fields. The SQLite adapter must derive
   them exactly from `messages`; it may use aggregate SQL backed by the existing session index.

## Required Semantics

- `ensure_session` accepts only matching active sessions or creates a missing active session.
- An ended session raises `SessionEndedError` during implicit turn handling.
- An archived session raises `SessionArchivedError`.
- `resume_session` accepts only an ended session with matching user and character and clears
  `ended_at`.
- `end_session` is idempotent for an already ended session owned by the caller.
- `archive_session` accepts active or ended sessions and is idempotent for the owner.
- An ended session archived later may retain `ended_at`; `archived_at` records the archive time.
- Session reads expose an effective `updated_at` reflecting the newest lifecycle update or
  persisted message.
- Session validation failure must occur before messages, prompt logs, relationship updates, or
  memory jobs. A redacted `session_validation_failed` trace may be recorded.
- The resolved active character ID remains the session ownership character.

## Persistence Guidance

The public shape of `SessionRecord` does not require every field to be a physical session-table
column. For SQLite v0.1:

- persist lifecycle and identity fields in `sessions`;
- derive `message_count` with `COUNT(messages.id)`;
- derive `last_message_at` with `MAX(messages.created_at)`;
- expose `updated_at` as the newer of the session lifecycle timestamp and last message timestamp.

This avoids making `MessageStore.save_message` secretly responsible for a second module's state.
A future high-volume adapter may materialize the projection internally without changing the port.

## Worker Action

Update the design review to reflect this decision, then implementation may begin. Do not modify
manager-owned architecture, interface, or task documents; they have already been synchronized.
