# T-0143 Streaming Chat Runtime And API v0.1 Manager Design Review

Date: 2026-08-04

Status: **S1/S2/S3 Checkpoint Accepted / S4-S7 Authorized In Sequence**

## S2 Re-review Acceptance

Manager re-review on 2026-08-05 found no remaining blocking or non-blocking defect in S2.

Accepted repairs:

- the persisted terminal-reason vocabulary is frozen to the 12 provider-neutral domain values and
  now aligns with the accepted cancellation contract;
- provider-specific parser reasons remain outside persistence and will be mapped by S4 to
  `model_invalid_response` or `model_failed`;
- completed records require exactly the integer `trust`, `affection`, and `familiarity` deltas,
  while `in_progress` and `failed` require the canonical empty object;
- the strict v0.1-to-v0.2 migration verifies the old schema, maps the two legacy aliases, copies
  rows, rebuilds indexes, writes the new marker, and commits as one SQLite writer transaction;
- raw SQLite and SQLAlchemy-on-SQLite continue to share the same DTO/policy and contract suite.

Independent Manager verification:

- S2 focused suite: 58 passed;
- complete store-contract suite: passed;
- full suite: passed with one existing skip;
- `python -m ruff check app tests`, `python -m compileall -q app tests`, and scoped
  `git diff --check`: passed.

The private SQL-helper import remains a non-blocking persistence-internal cleanup item. It does not
affect the accepted runtime contract.

The S1/S2/S3 prerequisite checkpoint is now accepted. Worker 19 may proceed through S4-S7 in the
frozen order. T-0143 remains `in_progress`; only final Manager delivery review may mark it `done`.

## S2 Persistence Checkpoint Review

Manager review on 2026-08-05 accepts the S2 table direction, narrow persistence port, migration
gate, transaction boundaries, raw SQLite/SQLAlchemy-on-SQLite parity, latest-row relationship
updates, and concurrency evidence. Two changes are required before S2 acceptance.

### Blocking: terminal reason vocabulary is not aligned with the frozen stream contract

`STREAM_TURN_TERMINAL_REASONS` currently accepts aliases such as `cancelled` and
`runtime_shutdown`, but rejects the frozen cancellation reasons `user_cancelled`,
`sse_generator_closed`, and `runtime_closing`. It also cannot directly persist DeepSeek's safe
parser reasons. This would make S4 fail while attempting to terminalize otherwise expected
cancellation or provider-error paths.

Freeze the persistence vocabulary as provider-neutral domain categories:

- cancellation: `user_cancelled`, `client_disconnected`, `sse_generator_closed`,
  `consumer_slow`, `runtime_closing`;
- model/preparation/runtime: `model_failed`, `model_invalid_response`, `preparation_failed`,
  `runtime_interrupted`, `runtime_orphaned`;
- Session lifecycle: `session_archived`, `session_ended`.

S4 must map provider-specific safe reasons explicitly: `MODEL_INVALID_RESPONSE` persists as
`model_invalid_response`; other model failures persist as `model_failed`. Provider-specific parser
reasons may remain in content-safe model observability, but are not persistence vocabulary. Update
the DTO, SQL CHECK/model contract, strict migration validation, and dual-backend tests together.

### Medium: completed replay accepts an incomplete state delta

`parse_stream_turn_state_delta()` accepts any subset of `trust`, `affection`, and `familiarity`.
Consequently a completed row with `state_delta_json='{}'` or one missing a key is returned as a
valid completed replay, even though the public terminal contract requires all three fields.

Record hydration must validate status-dependent payload shape: completed records require exactly
the three integer delta keys; `in_progress` and `failed` records require the canonical empty object.
Add raw SQLite and SQLAlchemy-on-SQLite corruption/replay tests that fail closed with
`STORAGE_MIGRATION_REQUIRED`.

The import of private SQL-normalization helpers from `browser_auth_migrations.py` is a non-blocking
persistence-internal cleanup opportunity. It may be moved to a shared SQLite schema-contract helper
later and does not need to expand this repair.

Independent Manager verification:

- S2 focused suite: 24 passed;
- complete store-contract suite: 100 passed;
- full suite: passed with one existing skip;
- `python -m ruff check app tests` and `python -m compileall -q app tests`: passed.

S2 remains unaccepted until both required changes pass dual-backend focused and full regression
tests. S4-S7 remain blocked.

## S1/S3 Checkpoint Acceptance

Manager review on 2026-08-04 found no blocking or non-blocking defect in the authorized S1/S3
scope.

Accepted S1 evidence:

- `DialogueTurnEngine` centralizes the shared Character, Session, Semantic Query, Memory, Evidence,
  prompt, state-decision, trace, and prompt-log stages without introducing a second Controller.
- `DialogueController.handle_user_message()` remains the synchronous compatibility facade and
  preserves the characterized success order, Session-validation boundary, and failed model-call
  terminal order.
- Existing dependency replacement behavior is preserved through compatibility properties.

Accepted S3 evidence:

- The model streaming contract is provider-neutral and keeps prompt/delta content out of object
  representations.
- The DeepSeek parser requires an accepted `finish_reason`, a nonblank accumulated answer, and the
  final `[DONE]`; it rejects unsupported outcomes and malformed/incomplete streams without exposing
  raw provider data.
- `reasoning_content` is not emitted or accumulated, retries stop after a 200 stream is accepted,
  and response/session cleanup is idempotent in the delivered scope.

Manager independently ran:

- `python -m pytest -q tests/test_dialogue_turn_engine.py tests/test_model_streaming.py`: 25 passed;
- `python -m pytest -q`: 682 passed, 1 skipped;
- `python -m ruff check app tests`: passed;
- `python -m compileall -q app tests`: passed;
- scoped `git diff --check`: passed, with only existing CRLF conversion notices.

Repository-wide Ruff remains blocked only by the pre-existing, out-of-scope
`frontend/capture_frontend.py:17:1 I001`; this checkpoint does not authorize changing `frontend/`.
The characterization suite fixes the most important synchronous success and failure boundaries;
the complete streaming failure/cancellation/race matrix remains mandatory in S4-S7.

This acceptance is only for S1/S3. Persistence S2 has not been delivered to this Manager
checkpoint, so S4-S7 remain unauthorized and T-0143 remains `in_progress`.

## Manager Re-review Acceptance

The v0.2 repair closes all six required design changes:

- fingerprint v1 is versioned and covers only immutable client business payload;
- DeepSeek `[DONE]`, `finish_reason`, truncation, unsupported outcomes, and empty responses have
  explicit fail-closed semantics;
- Runtime Guardrails exposes an explicit thread-safe, idempotently closeable admission handle while
  preserving the synchronous context-manager facade;
- the producer/SSE bridge uses an event-loop-safe bounded `asyncio.Queue` contract;
- replay revalidates owner and Session lifecycle before returning content;
- completion applies relationship deltas to the latest durable row inside the narrow UoW.

Manager additionally freezes the per-key registry hard cap at `128`. The persistence record uses
`active_character_id` for the fallback-resolved character; the exact requested `character_id`
remains part of fingerprint v1.

Phased authorization:

- Worker 19 may implement S1 and S3 only.
- The Persistence owner may implement S2 under the accepted contract with raw SQLite and
  SQLAlchemy-on-SQLite parity.
- S4-S7 remain blocked until Manager accepts the S1/S2/S3 checkpoint.

T-0143 moves to `in_progress`; final `done` remains a Manager delivery decision.

## Review Scope

This review checked the proposed streaming domain boundary, Controller reuse, durable idempotency,
completion ordering, cancellation/resource ownership, Runtime Guardrails, SSE adapter, DeepSeek
stream parsing, observability, and compatibility with the accepted Web MVP contract.

The initial findings below are retained as review history and are resolved by the accepted v0.2
revision. The overall direction is to retain the synchronous path, add a provider-neutral streaming
capability, extract a shared `DialogueTurnEngine`, keep HTTP/SSE at the API adapter, and require a
durable idempotency record before implementation. The following contracts must be corrected before
Persistence or runtime code is authorized.

## Resolved Required Changes

### 1. The idempotency fingerprint includes volatile request metadata

The design says the fingerprint covers `character_id + message + canonical metadata`, reuses the
final merged turn metadata, and also promises that a new HTTP `request_id` does not affect replay.
The current `AgentService._turn_metadata(...)` injects `request_id` into that merged metadata. A
network retry therefore receives a different fingerprint and becomes `CHAT_REQUEST_CONFLICT`
instead of replaying the original outcome.

Required revision:

- define an immutable idempotency payload before server-derived metadata is merged;
- fingerprint only the requested `character_id`, exact message, and validated client-supplied
  metadata;
- exclude `request_id`, Auth metadata, IP data, timestamps, runtime IDs, and all server-derived
  fields;
- freeze canonical JSON rules and a domain-separated fingerprint version;
- persist the fingerprint version or permanently freeze an explicitly versioned v1 algorithm;
- add tests proving that a new HTTP request ID replays, while changed message/client metadata
  conflicts.

### 2. `[DONE]` is a framing terminator, not sufficient proof of a successful answer

The proposed parser marks any well-formed stream ending in `data: [DONE]` as successful and does
not freeze `finish_reason` handling. DeepSeek exposes `stop`, `length`, `content_filter`,
`tool_calls`, and `insufficient_system_resource`; these outcomes cannot all commit the same durable
assistant result.

Required revision:

- track exactly one supported choice (`index == 0`) and its terminal `finish_reason`;
- require both an accepted finish reason and `[DONE]` before completion;
- accept `stop` with a non-empty final answer;
- accept `length` with a non-empty answer only with a stable truncation warning;
- fail closed for `content_filter`, unsupported `tool_calls`,
  `insufficient_system_resource`, unknown/missing finish reason, or EOF before `[DONE]`;
- keep `reasoning_content` excluded from deltas, persistence, and logs;
- treat an empty final answer as a failed turn and do not insert an empty assistant message.

The event grammar allows zero deltas before a terminal event; it does not require zero-delta
`turn.completed`.

### 3. The existing admission contract is not a transferable streaming lease

`DialogueAdmissionPort.acquire(...)` currently returns an `AbstractContextManager` whose yielded
`DialogueAdmissionLease` contains timing data only. The design acquires this context before the
HTTP response is returned and later releases it from a producer thread through `ChatStreamHandle`.
That ownership transfer is not represented by the existing port and must not be implemented by
manually carrying `__enter__`/`__exit__` across threads.

Required revision:

- add an explicit, idempotently closeable, thread-safe admission handle, or provide another design
  that keeps acquire/release within one owner while preserving pre-header admission failures;
- keep the current synchronous context-manager API as a compatibility facade if the port changes;
- define response-construction failure ownership so a reserved turn cannot lose its handle before
  iteration starts;
- add same-thread reentrancy, cross-thread close, repeated close, response-construction failure,
  and runtime-shutdown tests.

Runtime Guardrails owns this contract. It must be reviewed with the streaming implementation and
must not be hidden inside the SSE adapter.

### 4. The sync-producer to async-SSE bridge is underspecified

The design proposes a producer thread plus a bounded queue, but does not specify how the async SSE
generator waits for queue events. Calling blocking `queue.Queue.get()` from the single Uvicorn event
loop would stall unrelated API requests and disconnect detection.

Required revision:

- freeze an event-loop-safe bridge such as an `asyncio.Queue` populated via
  `loop.call_soon_threadsafe`, or an explicitly cancellation-safe AnyIO thread bridge;
- do not poll with real sleeps or block the event-loop thread;
- define queue shutdown/sentinel behavior and terminal ordering under simultaneous cancel/failure;
- prove `/health` and another authenticated request remain responsive while one stream is idle or
  backpressured;
- prove disconnect cancellation wakes both the async consumer and sync producer without leaking
  the Session/dialogue or LLM permit.

### 5. Replay must preserve current Session lifecycle rules

The outcome matrix allows completed/failed replay before dialogue admission, but does not require a
current Session ownership/lifecycle check. A stored assistant response must not bypass the existing
archived-Session read restriction.

Required revision:

- every lookup/replay must remain owner-scoped and validate the current Session;
- an ended Session may replay an existing read-only outcome but may not reserve a new turn;
- an archived Session must return `SESSION_ARCHIVED` and must not replay message content;
- add cross-user, ended, archived, and lifecycle-race tests for both storage backends.

### 6. Relationship completion must apply deltas to the latest durable row

Admission is scoped by `(user_id, session_id)`, so two Sessions for the same user and character may
complete concurrently. The narrow completion UoW must not overwrite relationship state using the
snapshot read during prompt preparation.

Required revision:

- inside the completion transaction, read/update the latest relationship row and apply/clamp the
  approved deltas atomically;
- preserve the current mood, emotion, and last-topic semantics without duplicating policy in only
  one backend;
- add a two-Session concurrency contract test proving no relationship delta is lost.

## Manager Decisions

1. **Persistence change: approved in principle, not yet authorized for implementation.** Revise the
   fingerprint, lifecycle, admission, and completion contracts first. Persistence work must then be
   performed by the Persistence owner with raw SQLite and SQLAlchemy-on-SQLite parity.
2. **Completion UoW: approve the narrow option.** Atomically commit assistant message, latest-row
   relationship update, and idempotency completion. Long-term Memory remains post-commit ancillary
   work and may not reverse a durable completed turn.
3. **Controller extraction: approved.** Add one shared `DialogueTurnEngine`; keep
   `DialogueController.handle_user_message(...)` as the synchronous facade and do not create a
   copied streaming controller.
4. **Replay/retention: approved with lifecycle correction.** Terminal records live with the
   dialogue Session, have no short TTL, and never rerun the Provider for the same key.
5. **Public error codes: approved.** `CHAT_STREAM_CANCELLED` and
   `CHAT_STREAM_INTERRUPTED` remain `retryable=false`; an intentional regeneration uses a new key.
6. **Resource defaults: provisionally approved.** Heartbeat 15 seconds, queue 32, slow-consumer
   timeout 5 seconds, response limits 32768 characters/98304 UTF-8 bytes, and public delta maximum
   4096 UTF-8 bytes require bounded configuration and fault tests.
7. **Zero-delta success: rejected.** Empty final answer is a failed turn. Zero deltas followed by
   `turn.failed` remains valid event grammar.
8. **Streaming Prompt Log: approved.** It is summary-only and best-effort; sync/CLI Prompt Log
   behavior remains unchanged. Logger failure must not alter durable outcome or leak resources.

## Additional Clarifications

- The per-key registry must be bounded/ref-counted and removed after use. If its lock is held while
  waiting up to five seconds for Session admission, the design must state that duplicate callers can
  wait that long; it must not promise immediate rejection.
- Post-commit Memory work must be bounded and must not indefinitely delay terminal delivery or hold
  admission. The first production Web profile still has long-term Memory disabled.
- Caddy's documented behavior supports immediate flushing for `text/event-stream`; do not add
  `flush_interval -1`, because that mode also disables backend cancellation on early client
  disconnect. This still requires a real proxy fault test before production activation.

## Verification State

This was a design and current-code review. No runtime code, schema, test, deployment configuration,
`frontend/`, or production Web profile was changed. No pytest run is claimed for this design round.

The initial review left T-0143 `pending`. The Manager re-review above supersedes that state and
authorizes only the stated S1/S2/S3 phase. Do not enter S4-S7 or mark the task complete before the
next Manager checkpoint.

Primary references:

- DeepSeek Chat Completion streaming: <https://api-docs.deepseek.com/api/create-chat-completion/>
- Caddy reverse proxy streaming: <https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming>
