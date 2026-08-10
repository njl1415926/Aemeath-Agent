# T-0144 Web Session API Completion v0.1 Design

Date: 2026-08-07

Worker Session: 20

Baseline: `9af2eb7`

Status: Manager Accepted. Implementation and final cross-workflow admission repairs were accepted on
2026-08-07; T-0144 is complete.

## Scope

T-0144 completes the browser-facing dialogue Session API:

- update Session title;
- end, resume, and archive Session lifecycle commands;
- stable opaque cursor pagination for Session lists and message history;
- owner, character, and lifecycle validation through `SessionService`;
- raw SQLite and SQLAlchemy-on-SQLite contract parity.

Out of scope remains unchanged: no `DialogueController`, Streaming, Memory, Character, RAG, model,
frontend, D6 backup/restore, browser hard delete, or Manager-owned task-status change.

## Current Baseline Findings

Current API routes are thin and already call `SessionService` for create/list/message history.
Routes do not directly access SQL/store. This boundary should be preserved.

Current `SessionService` supports create/list/messages only. It verifies owner and character for
message reads, but message history pagination is implemented by loading all messages and slicing the
tail in service memory.

Current `SessionStorePort` has lifecycle methods but no title update and no cursor-aware list API.
Current `MessageStore` has no cursor-aware history query.

Raw SQLite and SQLAlchemy implementations are semantically parallel today:

- Session effective `updated_at` is derived as max(`sessions.updated_at`, newest message timestamp).
- Session list ordering is `updated_at DESC` without an explicit public tie-breaker.
- Message list ordering is `created_at ASC`; recent-message reads use `created_at DESC` then reverse.
- Existing tables already have `sessions.title`, lifecycle timestamp columns, and the core message
  timestamp/index needed for correctness.

Cookie Web Auth already enforces provenance and CSRF for non-safe Web Session requests in the shared
API dependency/middleware path. Dev Bearer auth remains a separate mode. T-0144 routes should reuse
that path and must not inspect cookies, CSRF tokens, or Authorization headers directly.

## Public HTTP Contract

Existing paths remain:

```text
GET  /v1/sessions
POST /v1/sessions
GET  /v1/sessions/{session_id}/messages
```

T-0144 adds:

```text
PATCH /v1/sessions/{session_id}
POST  /v1/sessions/{session_id}/end
POST  /v1/sessions/{session_id}/resume
POST  /v1/sessions/{session_id}/archive
```

All routes depend only on:

- `get_user_context`;
- `get_runtime`;
- `runtime.session_service`.

No route may call `runtime.store`, repository objects, SQLAlchemy sessions, raw SQL, or auth internals.

### Request DTOs

`PATCH /v1/sessions/{session_id}`

```json
{
  "title": "optional display title or null",
  "character_id": "aemeath"
}
```

`character_id` is required for title update so the browser proves it is operating on the expected
single-character context. Character validation belongs in `SessionService`.

Lifecycle command DTO:

```json
{
  "character_id": "aemeath"
}
```

`end`, `resume`, and `archive` require `character_id` in the Web API. `SessionService` validates
character before calling the current lifecycle store methods; the existing lifecycle persistence
interfaces are not expanded with optional `character_id`.

### Response DTOs

Single Session mutation/create responses return the existing `SessionResponseBody`.

Session list responses keep the existing public `sessions` field and add cursor fields:

```json
{
  "sessions": [],
  "next_cursor": "opaque string or null",
  "has_more": false
}
```

Message history responses keep the existing public `messages` field and add cursor fields:

```json
{
  "messages": [],
  "next_cursor": "opaque string or null",
  "has_more": false
}
```

Internal service code may use a generic `Page[T]`, but the HTTP DTOs must not expose a generic
`items` field and do not need transition aliases.

## Title Rules

Title writes must use one deterministic canonicalization helper shared by create and patch. The
helper lives in `app/session/titles.py`.

1. `null` means clear the title and stores `NULL`.
2. A string is normalized to Unicode NFC.
3. Every Unicode whitespace run is folded to one ASCII space.
4. Leading and trailing whitespace are removed.
5. An empty result stores `NULL`.
6. Non-whitespace C0/C1 control characters are rejected as `INVALID_INPUT`.
7. The post-canonical title must be at most 80 Unicode code points and at most 256 UTF-8 bytes.
8. Internal non-whitespace Unicode is preserved after NFC normalization.

The API never auto-generates titles in v0.1. Existing historical stored titles are read as-is; new
create/update writes follow the canonical helper.

Title update is allowed only for `active` and `ended` Sessions owned by the user and matching the
requested character. `archived` Sessions are immutable through the browser and return
`SESSION_ARCHIVED` / HTTP 409 for title update.

Title update must be concurrency-safe. `SessionService` first validates the current record's owner
and character through `get_session()`, then the persistence update must constrain the write by
`session_id + user_id + allowed status IN ('active', 'ended')` in the same SQL update. The store
must not write a title if the Session was concurrently archived between service validation and the
update. If the update affects zero rows, the implementation rereads the Session and maps the result
to a stable error:

- missing: `SESSION_NOT_FOUND`;
- different owner: `SESSION_OWNERSHIP_DENIED`;
- archived: `SESSION_ARCHIVED`;
- any other unexpected zero-row case: storage/internal mapped through the existing error layer.

## Lifecycle Semantics

These transitions remain the v0.1 contract:

```text
active -> ended
ended -> active
active -> archived
ended -> archived
```

Command behavior:

- `end`: `active` becomes `ended`; already `ended` is idempotent; `archived` returns
  `SESSION_ARCHIVED`.
- `resume`: only `ended` becomes `active`; `active` returns `SESSION_INVALID_TRANSITION`;
  `archived` returns `SESSION_ARCHIVED`.
- `archive`: `active` or `ended` becomes `archived`; already `archived` is idempotent and returns
  the archived record.

For all commands, missing Sessions return `SESSION_NOT_FOUND`; cross-user or cross-character access
returns `SESSION_OWNERSHIP_DENIED` with the existing generic public message.

No browser hard delete is added.

## Archived History Reads

Recommended Web API rule: explicitly addressed archived Sessions may be read, but not resumed,
sent to, or edited.

Rationale:

- archive is not deletion;
- v0.1 has no hard-delete/export UI;
- browser users may need to view archived conversation history;
- read access still verifies owner and character before loading messages.

Concrete behavior:

- `GET /v1/sessions?status=active` hides archived Sessions.
- `GET /v1/sessions?status=archived` and `status=all` may list archived Sessions for the owner.
- `GET /v1/sessions/{session_id}/messages?character_id=...` returns archived history if owner and
  character match.
- `POST /v1/chat` and `POST /v1/chat/stream` continue to reject archived Sessions through the turn
  preflight contract.

Implementation must not add a new persistence method such as `require_session_for_history()`.
`SessionService` uses `session_store.get_session(session_id=...)`, checks `user_id` and
`character_id`, allows `active`, `ended`, and `archived` for this read-only history path, then calls
the paginated message query. Message rows are not read before ownership and character validation.

## Cursor Contract

Cursor pagination is keyset pagination. Offset pagination is not part of the public contract.

Cursor tokens are opaque to clients and must be treated as untrusted pagination state. They are not
authorization capabilities. Every paged query still rechecks authenticated `user_id`, endpoint
filters, and Session ownership/character where applicable.

Cursor encode/decode lives in `app/session/pagination.py`, separate from HTTP routes and persistence
repositories.

### Cursor Encoding

Cursor strings are at most 2048 ASCII characters. Any non-ASCII character, overlong value, malformed
base64url, or malformed JSON is rejected as an invalid cursor.

Payload is base64url encoding of canonical JSON. It uses a strict flat field whitelist; unknown
fields, missing fields, non-string/non-integer field types, arrays, unexpected objects, and malformed
values are rejected. The only accepted fields for a Session-list cursor are:

```json
{
  "v": 1,
  "kind": "sessions",
  "order": "effective_updated_at_desc_id_desc",
  "filter": "sha256 fingerprint of endpoint filters and authenticated user",
  "anchor_time": "2026-08-07T01:02:03.000000Z",
  "anchor_id": "session_..."
}
```

The only accepted fields for a message-history cursor are:

```json
{
  "v": 1,
  "kind": "messages",
  "order": "created_at_desc_id_desc",
  "filter": "sha256 fingerprint of endpoint filters and authenticated user",
  "anchor_time": "2026-08-07T01:02:03.000000Z",
  "anchor_id": "msg_..."
}
```

Cursor time is UTC RFC3339 with fixed six-digit microsecond precision and a literal `Z`, matching:

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

The decoder rejects invalid dates, leap-second values, timezone offsets other than `Z`, missing
microseconds, extra precision, naive local times, and non-canonical re-serializations. The decoded
anchor time is exposed to callers as a timezone-aware UTC `datetime`, not a raw string.

Filter fingerprints bind cursors to:

- endpoint kind;
- authenticated `user_id`;
- `session_id` for messages;
- `character_id` where applicable;
- `status` for Session lists.

The page `limit` is not part of the fingerprint, so clients may change limit between requests.

Invalid cursor cases:

- not base64url;
- not JSON object;
- unsupported `v`;
- wrong `kind`;
- wrong `order`;
- missing or malformed anchor time/id;
- unknown fields;
- wrong field types;
- unexpected nested objects or arrays;
- illegal or non-canonical RFC3339 time;
- filter fingerprint mismatch;
- cursor too long.

All invalid or expired cursors map to `INVALID_INPUT` / HTTP 400 with public message
`Invalid cursor.` Internal logs may record safe reason codes such as `malformed_base64`,
`filter_mismatch`, or `unsupported_version`, but never raw cursor text.

Cursor expiry: no time-based expiry in v0.1. Cursors become invalid only when the version/order/filter
contract changes or the cursor is malformed/mismatched. A future cursor `v=2` may intentionally
invalidate v1.

Both raw SQLite and SQLAlchemy-on-SQLite currently store Session/Message times in `TEXT` columns, and
`utc_now()` persists timezone-aware `datetime.isoformat()` strings such as
`2026-08-07T01:02:03.000000+00:00`. Therefore both backends use the same shared helper flow:

1. Cursor externally uses fixed six-digit microsecond UTC `Z` format.
2. Decoder returns a timezone-aware UTC `datetime`.
3. Repository code formats the decoded `datetime` back to the existing storage-compatible ISO string
   before keyset comparison.
4. Repositories never compare the raw cursor string.

Raw/SQLAlchemy parity tests must cover this time boundary, including stored `+00:00` values.

### Session List Ordering

Sort key:

```text
effective_updated_at DESC, session_id DESC
```

`effective_updated_at` is the public read-model value already returned as `SessionRecord.updated_at`:
the newer of lifecycle `sessions.updated_at` and newest persisted message timestamp.

Tie-breaker is `session_id DESC`. IDs remain opaque; the tie-breaker is only for deterministic
ordering when timestamps collide.

Keyset predicate after a cursor:

```text
effective_updated_at < anchor_time
OR (effective_updated_at = anchor_time AND session_id < anchor_id)
```

The store fetches `limit + 1` rows. Service returns at most `limit`; `has_more` is true when the
extra row exists. `next_cursor` is anchored to the actual last returned item, never to the extra
probe row.

### Message History Ordering

Fetch sort key:

```text
created_at DESC, message_id DESC
```

Response item order:

```text
created_at ASC, message_id ASC
```

This gives the browser a newest page that displays chronologically, while `next_cursor` walks toward
older history. The cursor is anchored to the oldest returned message in the page.

Keyset predicate after a cursor:

```text
created_at < anchor_time
OR (created_at = anchor_time AND message_id < anchor_id)
```

The store queries `limit + 1` rows in descending order. Service drops the extra probe row, anchors
`next_cursor` to the actual last returned descending row, and only then reverses the returned page
into ascending chronological response order.

## Concurrent Change Semantics

Pagination is read-committed keyset pagination, not a snapshot export.

Session list:

- A Session updated after page 1 may move ahead of the cursor and will not appear again in the same
  traversal.
- An unseen older Session that receives a new message or lifecycle update may move ahead of the
  cursor and may be missed by the ongoing traversal.
- Clients should refresh the first page to observe newly updated Sessions.

Message history:

- New messages appended after page 1 do not affect older-history pagination.
- Clients should refresh the newest page or consume streaming events for new messages.
- Messages with identical timestamps remain stable through `message_id` tie-breaker.

Lifecycle changes during pagination:

- A Session archived between pages may disappear from `status=active` traversal.
- A resumed or ended Session may move according to its new effective update timestamp.
- This is acceptable for UI browsing and avoids long-lived server snapshots.

Concurrent lifecycle commands must enter the same dialogue admission boundary used by synchronous
and streaming chat before any owner, character, or lifecycle read. The lease is held through
persistence and App Log completion, so an active stream or terminal replay cannot be overtaken by
`end`, `resume`, or `archive`. Persistence still uses expected-status conditional updates; zero-row
updates are reread and classified into the stable lifecycle errors above. Idempotent commands remain
idempotent. Invalid transitions return `SESSION_INVALID_TRANSITION`.

## Service And Port Changes

### SessionService

Add methods:

```python
update_title(user_context, session_id, character_id, title) -> SessionRecord
end_session(user_context, session_id, character_id) -> SessionRecord
resume_session(user_context, session_id, character_id) -> SessionRecord
archive_session(user_context, session_id, character_id) -> SessionRecord
list_sessions_page(user_context, character_id, status, limit, cursor) -> Page[SessionRecord]
list_messages_page(user_context, session_id, character_id, limit, cursor) -> Page[MessageRecord]
```

`SessionService` owns:

- title canonicalization;
- cursor encode/decode/validation;
- owner/character/lifecycle orchestration;
- dialogue admission acquisition for lifecycle mutations;
- App Log summary events;
- hiding store-specific query shape from routes.

For lifecycle mutations, `SessionService` receives the existing `DialogueAdmissionPort` and API
composition passes `agent_service.admission`, the same instance used by sync chat and
`AgentStreamService`. `SessionService` acquires `(user_id, session_id)` admission before validating
owner and `character_id` with `get_session()`, then calls the existing persistence methods while
holding the lease. Success and failure App Log records are emitted before the lease is released.
The persistence lifecycle methods keep their current signatures and remain responsible for atomic
owner/status transition behavior.

### SessionStorePort

Add:

```python
def update_session_title(
    *,
    session_id: str,
    user_id: str,
    title: str | None,
) -> SessionRecord: ...

def list_sessions_page(
    *,
    user_id: str,
    character_id: str | None,
    status: SessionStatus | None,
    limit: int,
    after_updated_at: datetime | None = None,
    after_id: str | None = None,
) -> list[SessionRecord]: ...
```

No new `require_session_for_history()` method is added. Archived history is a `SessionService`
read-only policy implemented with `get_session()` plus explicit owner/character checks.

Existing `list_sessions(...)` can remain for CLI/tests and delegate to the new page method with no
cursor.

Existing lifecycle store methods are not expanded with optional `character_id`. Character validation
stays in the service layer.

### MessageStore

Add:

```python
def list_messages_by_session_page(
    *,
    session_id: str,
    limit: int,
    before_created_at: datetime | None = None,
    before_id: str | None = None,
) -> list[MessageRecord]: ...
```

The store returns rows in fetch order `created_at DESC, id DESC`, with `limit + 1` supported. The
service reverses returned page items for chronological display.

## Persistence Change Request

No table or column schema change is required for v0.1 correctness.

Deferred index change request for deterministic keyset performance:

```text
PCR-T0144-01:
  Add idx_messages_session_created_id on messages(session_id, created_at DESC, id DESC).
  Add idx_sessions_user_status_updated_id on sessions(user_id, status, updated_at DESC, id DESC).
  Add idx_sessions_user_character_status_updated_id on
      sessions(user_id, character_id, status, updated_at DESC, id DESC).
```

Manager decision: defer PCR-T0144-01. Existing indexes are sufficient for the small-scale first Web
release; future index/materialized-projection changes should be based on observed query data. T-0144
must not modify `schema.sql`, SQLAlchemy model indexes, or migration/index contracts for this PCR.

The deferred PCR is not sufficient to cover `effective_updated_at` when it is derived from messages,
so queries will still need the existing aggregation or a future materialized projection if revisited.

## Error Contract

Use existing codes where possible:

- bad title, bad limit, malformed/invalid cursor: `INVALID_INPUT` / 400;
- missing Session: `SESSION_NOT_FOUND` / 404;
- cross-user or cross-character: `SESSION_OWNERSHIP_DENIED` / 403;
- archived mutation/resume/chat: `SESSION_ARCHIVED` / 409;
- invalid lifecycle transition: `SESSION_INVALID_TRANSITION` / 409;
- CSRF failure in Web Session mode: existing `CSRF_INVALID` / 403;
- Bearer/Cookie hybrid: existing `AUTH_CREDENTIAL_CONFLICT` / 400;
- storage failures: existing storage mappings.

Manager decision: no new cursor error code is added in v0.1. Cursor errors use `INVALID_INPUT` /
HTTP 400.

Public responses must not expose ownership details, raw cursor text, SQL errors, titles rejected for
content, cookies, CSRF token values, or Authorization values.

## App Log Contract

`SessionService` should receive optional `AppLoggerPort`, defaulting to `NoopAppLogger`, and record
summary-only events:

- `api.session.created`
- `api.session.listed`
- `api.session.messages_listed`
- `api.session.title_updated`
- `api.session.ended`
- `api.session.resumed`
- `api.session.archived`
- `api.session.failed`

Safe fields:

- `request_id`;
- `user_id`;
- `session_id` when known;
- `character_id`;
- `status_before` and `status_after`;
- `title_action` as `set` or `cleared`;
- `title_length`, never title text;
- `limit`;
- `has_more`;
- `cursor_version`;
- `cursor_error_reason`;
- `error_code`;
- `duration_ms`.

Forbidden fields:

- raw cursor;
- message content;
- title text;
- metadata bodies if user supplied;
- Cookie, CSRF token, Authorization, browser session token, raw IP, User-Agent body, prompt, response,
  Memory/Evidence content, provider bodies.

The existing API middleware request log remains responsible for method/path/status summary.

## Test Matrix

Store contract parity:

- raw SQLite and SQLAlchemy title update set/clear/canonicalization;
- lifecycle end/resume/archive idempotency and invalid transitions;
- cross-user and cross-character rejection for all mutations;
- Session keyset pages with identical effective timestamps and `session_id` tie-breaker;
- message keyset pages with identical timestamps and `message_id` tie-breaker;
- cursor page `limit + 1` has_more behavior anchored to the actual last returned row;
- Session API limit defaults and maximums remain `50` / `200`;
- message-history API limit defaults and maximums remain `100` / `500`;
- archived history is readable through explicit message-history reads.

Service tests:

- title Unicode NFC, whitespace folding, empty clear, length/byte limits, control rejection;
- cursor decode rejects malformed, non-ASCII, over-2048-character, wrong version, wrong kind/order,
  unknown fields, wrong types, nested objects/arrays, illegal times, and filter mismatch;
- cursor from another user/session/filter is invalid;
- service does not read messages before ownership/character validation;
- archived history read allowed only for explicit message history, not chat/resume/title update.

API tests:

- `PATCH`, `end`, `resume`, `archive` happy paths under dev Bearer;
- Web Session mutation requests require provenance and CSRF;
- dev Bearer remains independent and does not require CSRF;
- Bearer plus Cookie hybrid fails closed;
- routes call `SessionService` only, verified with a fake runtime service where useful;
- paginated Session responses expose `sessions`, `next_cursor`, `has_more`;
- paginated message responses expose `messages`, `next_cursor`, `has_more`;
- invalid cursor returns `INVALID_INPUT` / 400 without echoing cursor;
- cross-user access uses existing safe public ownership message;
- no browser hard-delete route exists.

Regression:

- existing chat and stream ended/archived behavior remains unchanged;
- lifecycle mutations share admission identity with sync chat and streaming chat;
- active streams and terminal replay delivery block lifecycle mutations until their admission lease
  is released;
- existing `SessionStorePort.ensure_session` behavior remains unchanged for Controller paths;
- existing CLI Session behavior remains compatible;
- raw SQLite and SQLAlchemy store contract suites both pass.

Recommended focused commands after implementation:

```text
pytest tests/store_contract/test_session_message_contract.py tests/test_session_store.py tests/test_api.py tests/test_api_auth.py tests/test_api_streaming.py
pytest tests/test_agent_service.py tests/test_agent_stream_service.py
ruff check app tests
python -m compileall -q app
```

Full regression remains required before delivery.

## Expected Modified Files After Approval

Implementation is expected to touch:

- `app/services/session_service.py`
- `app/session/pagination.py`
- `app/session/titles.py`
- `app/session/ports.py`
- `app/api/routes/sessions.py`
- `app/api/schemas.py`
- `app/api/dependencies.py`
- `app/agent/message_store.py`
- `app/db/sqlite_store.py`
- `app/db/sqlalchemy_store.py`
- `app/db/repositories/sessions.py`
- `app/db/repositories/messages.py`
- focused API/service/store tests listed above

Cross-module review needed:

- Manager approval of this revised design before implementation starts;
- API/security review for Web Session CSRF/provenance coverage;
- Frontend contract review before T-0145 consumes the page DTO.

## Manager Decisions Applied

1. Explicit archived Session history reads are approved; chat, resume, and title modification remain
   forbidden for archived Sessions.
2. Public pagination responses keep `sessions` and `messages`; no public `items` field or transition
   alias is added.
3. Invalid cursors use `INVALID_INPUT` / HTTP 400; no new error code is added.
4. PCR-T0144-01 index changes are deferred.
5. `character_id` validation stays in `SessionService`; existing lifecycle store method signatures
   are not expanded.
