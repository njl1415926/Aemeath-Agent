# T-0143 Streaming Chat Runtime And API v0.1 Manager Design Review

Date: 2026-08-04

Status: **S1-S7 Accepted / T-0143 Done**

## Final Manager Acceptance

Manager final review on 2026-08-06 accepts the complete T-0143 delivery. S7 introduced no runtime or
schema change and completed the frozen regression/documentation phase. The implemented system keeps
sync CLI and `/v1/chat` compatibility, uses one shared `DialogueTurnEngine`, provides durable
idempotent streaming through `AgentStreamService`, and keeps HTTP/SSE/Auth details outside the core
domain modules.

Final evidence:

- S1-S6 focused regression: 331 passed;
- complete suite with real Caddy 2.10.2: 805 passed;
- scoped Ruff, compileall, deployment lock validation, and `git diff --check`: passed;
- Manager-owned architecture, interface, Web contract, review, and task documents were reconciled
  with the implemented `ChatStreamHandle` and active `/v1/chat/stream` boundary.

No blocking or non-blocking T-0143 finding remains. The known single-process, no-offset-resume,
cooperative-cancellation, and pre-production browser/cloud limits are explicit deferred work, not
v0.1 contract violations. T-0143 is complete; T-0144 and deployment D6 are the next independent
launch-path tasks.

## S6 Manager Checkpoint Acceptance

Manager review on 2026-08-06 found no blocking or non-blocking S6 defect:

- the narrow pure-ASGI boundary catches only post-header `OSError` / Starlette
  `ClientDisconnect` on `/v1/chat/stream`, notifies the response owner, preserves the first-wins
  `client_disconnected` intent, completes off-loop cleanup, and suppresses the downstream delivery
  exception;
- direct-owner and full FastAPI middleware-chain tests prove that an uncommitted turn becomes durable
  `client_disconnected` with no assistant message and zero admission/LLM-gate/registry ownership;
- a completion which wins the persistence CAS remains completed when terminal delivery later fails;
- success/failure marker scans cover App Log, Trace, streaming Prompt Summary, terminal objects, and
  safe repr output without retaining prompt, delta, credential, vector, provider body, or exception
  content;
- the real Caddy 2.10.2 test verifies live HTTP/1.1 and HTTP/2 delta delivery, heartbeat framing,
  downstream disconnect propagation, the 128 KiB body boundary, h1/h2 ALPN, and no h3 advertisement.
  `deploy/Caddyfile` remains unchanged and contains no `flush_interval -1`.

Independent Manager verification:

- the five-file S6/API/observability/Caddy targeted suite: 59 passed;
- full suite with the pinned Caddy binary enabled: 805 passed;
- `python -m ruff check app tests`, `python -m compileall -q app tests`, deployment lock validation,
  and `git diff --check`: passed.

The first focused invocation did not reach application code because the chosen Windows `--basetemp`
parent did not exist; after creating the local ignored parent, the unchanged test selection passed.
This is a test-harness prerequisite, not a product defect.

S6 is accepted. Worker 19 may proceed to S7 focused/full regression and final delivery reporting.
S7 must not add new feature scope, modify `frontend/`, activate the production Web Profile, or mark
T-0143 `done`; final task status remains a Manager decision.

## S5 Final Re-review Acceptance And S6 Entry Gate

Manager final S5 re-review on 2026-08-06 found no remaining defect in the requested cancellation-order
repair:

- `ChatStreamHandle.request_cancel()` now performs only the first-wins atomic intent write and never
  touches the Provider transport;
- both response-finally paths establish the classified intent before aborting the bridge, then run
  potentially blocking transport cancellation and handle close off the event loop;
- the actual-handle capacity-one test blocks the producer in a pending bridge put and proves that
  token, transport, durable persistence, and Trace all retain `client_disconnected`, with no assistant
  message and complete admission/LLM-gate/registry release.

Independent Manager verification:

- the new test's five internal race iterations passed in three consecutive external rounds;
- expanded S1-S5/API/Auth/runtime/store suite: 192 passed;
- full suite: 799 passed, 1 existing skip;
- `python -m ruff check app tests`, `python -m compileall -q app tests`, and `git diff --check`:
  passed.

S5 is accepted. Worker 19 may proceed to S6 only; S7 remains blocked and T-0143 remains
`in_progress`.

### S6 mandatory error-boundary input: ASGI send disconnect still escapes

During final review, a direct ASGI 2.4 fault probe made `send()` raise `OSError` after response start.
The complete app call propagated `OSError`; a direct `OwnedStreamingResponse` probe propagated
Starlette `ClientDisconnect` and classified the close as `sse_generator_closed`. This is exactly
within the frozen S6 observability/error/Caddy targeted-test phase, so it does not reopen S5, but it
is blocking for S6 acceptance.

S6 must:

- treat raw `OSError` and Starlette `ClientDisconnect` from post-header send as
  `client_disconnected`;
- preserve first-wins cancellation intent, abort the bridge, complete off-loop cleanup, and suppress
  the post-header delivery exception so it cannot become a raw ASGI traceback or proxy 502;
- add direct response-owner and full-app ASGI 2.4 fault tests asserting no escaped exception, durable
  `client_disconnected` when completion has not won, no partial assistant, and zero
  admission/gate/registry ownership;
- retain the existing rule that a durable completion which wins first is not rewritten by delivery
  failure.

## S5 Re-review

Manager re-review on 2026-08-06 confirms that both original High findings are substantially fixed:

- `handle.start()` now runs through `asyncio.to_thread()`. A deterministic `Thread.start()` failure
  keeps persistence work off the event loop and produces `turn.started -> turn.failed` with no
  Provider call or assistant message;
- bridge abort remains loop-safe, while blocking transport cancellation and `handle.close()` now run
  together in a worker thread. `/health` remains responsive during an injected blocking cancel, and
  admission, LLM-gate, and registry ownership all return to zero.

One cancellation-order defect remains before S5 acceptance.

### Medium: bridge backpressure can overwrite the real disconnect classification

The response currently calls `bridge.abort_from_any_thread()` before the off-loop
`handle.cancel(reason="client_disconnected")`. If the producer is blocked in
`bridge.put_from_producer()` when the browser disconnects, abort cancels that pending put first.
`AgentStreamService._accept_delta()` then classifies the bridge exception as
`sse_generator_closed` and wins the first-write cancellation token. The later adapter cancellation
cannot replace it, so the durable terminal reason and Trace status no longer describe the observed
client disconnect.

An independent actual-handle probe held the initial event in a capacity-one bridge, blocked the
producer on its first delta, then applied the adapter's abort-before-cancel order. All five runs
produced both token and durable terminal reason `sse_generator_closed`, not
`client_disconnected`. The delivered blocking-cancel test covers a producer waiting in model read,
so it does not exercise this queue-write interleaving.

Repair requirements:

- atomically establish the allowlisted cancellation intent before bridge abort can wake the
  producer, without performing synchronous transport close on the event loop;
- keep bridge abort prompt and execute the potentially blocking transport cancel/close work off the
  event loop; a narrow two-phase handle operation is acceptable if needed;
- add a deterministic pending-bridge-put disconnect test that asserts durable reason
  `client_disconnected`, no assistant message, no Provider retry, and zero admission/gate/registry
  ownership after cleanup.

Independent Manager verification:

- both repaired fault tests passed in five consecutive rounds;
- expanded S1-S5/API/Auth/runtime/store suite: 192 passed;
- full suite: 798 passed, 1 existing skip;
- `python -m ruff check app tests` and `python -m compileall -q app tests`: passed;
- the pending-bridge-put probe reproduced the remaining classification defect in five of five runs.

S5 remains `Changes Requested`; S6-S7 stay blocked and T-0143 remains `in_progress` until this final
ordering repair passes focused/full regression and Manager re-review.

## S5 API/SSE Checkpoint Review

Manager review on 2026-08-06 accepts the endpoint boundary, SSE frame format, bounded FIFO bridge,
normal terminal ordering, Auth/CSRF/request-ID integration, shared sync/stream rate limiter, and the
main disconnect cleanup direction. Two blocking event-loop and terminal-delivery defects must be
repaired before S5 acceptance.

### High: producer thread-start failure runs terminalization on the event loop and loses the terminal

`OwnedStreamingResponse._iterate_sse()` calls `handle.start()` directly on the Uvicorn event-loop
thread. When `Thread.start()` fails, the accepted S4 handle synchronously enters failed persistence
terminalization. Its attempt to deliver `turn.failed` is then rejected by `AsyncStreamBridge`, because
`put_from_producer()` correctly forbids producer puts from the event-loop thread. Cleanup enqueues only
the private producer-closed sentinel, so the HTTP 200 response ends after `turn.started` without the
required unique business terminal.

This also violates the frozen rule that thread-start/submission failure must not run SQLite,
Controller, or terminal cleanup on the event loop. An independent fault probe reproduced exactly one
outbound frame (`turn.started`) and no `turn.failed` or `turn.completed`.

Repair requirements:

- perform `handle.start(sink=bridge)` through a non-event-loop path, or otherwise guarantee that its
  complete failure terminalization runs outside the event loop;
- a post-header thread-start failure must complete failed persistence and emit exactly
  `turn.started -> turn.failed`, without an adapter-invented second terminal;
- add a deterministic `Thread.start()` failure test that also asserts no Provider call, no assistant
  message, failed durable state, responsive event loop, and zero admission/gate/registry ownership.

### High: disconnect cleanup invokes synchronous upstream close on the event loop

Both `OwnedStreamingResponse.__call__()` and `_iterate_sse()` call `handle.cancel(reason=...)`
directly on the event-loop thread. `ChatStreamHandle.cancel()` delegates to the active model session,
and the production DeepSeek session implements cancellation by synchronously closing the HTTPX
response stack. A slow socket/transport close can therefore stall `/health` and every other request
on the single Uvicorn worker. Offloading only the later `handle.close()` does not protect this path.

An independent blocking-cancel probe delayed the event loop for about 251 ms, matching the injected
cancel delay.

Repair requirements:

- abort/wake the async bridge immediately, but execute `handle.cancel(reason)` and `handle.close()` in
  a non-event-loop cleanup path while preserving the classified cancellation reason;
- retain idempotent ownership between generator-finally and response-finally paths;
- add a barrier-based blocking-cancel test proving `/health` remains responsive and eventual cleanup
  leaves zero LLM permits, dialogue admission entries, and active registry handles.

Independent Manager verification:

- expanded S1-S5/API/Auth/runtime/store suite: 191 passed;
- full suite: 797 passed, 1 existing skip;
- `python -m ruff check app tests`, `python -m compileall -q app tests`, and `git diff --check`:
  passed;
- two deterministic fault probes reproduced the defects above; the delivered S5 tests do not cover
  either failure mode.

S5 remains unaccepted and T-0143 remains `in_progress`. S6-S7 stay blocked until both repairs pass
focused and full regression tests and Manager re-review accepts this checkpoint.

## S4 Re-review Acceptance

Manager re-review on 2026-08-05 found no remaining blocking or non-blocking defect in S4.

Accepted repairs:

- a handle remains service-owned until successful registry registration; registration failure now
  terminalizes a reserved record, releases admission/gate ownership, and preserves
  `turn_started -> turn_failed` Trace order;
- the handle now uses one condition-protected `created -> starting/running -> closing -> closed`
  state machine, so a closer cannot observe an unstarted producer and a close that wins from
  `created` prevents every later `start()`;
- completed and failed replay delivery errors or cancellation no longer enter new-turn failure
  terminalization, synthesize a different domain terminal, mutate the durable record, or call the
  Provider; they emit only a content-safe delivery-aborted App Log summary.

Independent Manager verification:

- the nine deterministic registry/start-close/replay fault cases passed;
- expanded S1-S4/runtime/store suite: 144 passed;
- full suite: 787 passed, 1 existing skip;
- `python -m ruff check app tests`, `python -m compileall -q app tests`, and `git diff --check`:
  passed.

The S4 checkpoint is accepted. Worker 19 may implement S5 API/SSE adapter only, then must stop for
the next Manager checkpoint. S6-S7 remain blocked, and T-0143 remains `in_progress` until final
delivery acceptance.

## S4 Runtime Checkpoint Review

Manager review on 2026-08-05 accepts the S4 module direction, frozen fingerprint implementation,
shared turn-engine reuse, S2 persistence integration, bounded response accumulation, provider-neutral
terminal mapping, post-commit Memory boundary, summary-only observability, and runtime resource
registration order. Three blocking race/terminal issues must be repaired before S4 acceptance.

### High: registry-close race leaks a reserved turn and dialogue admission

`AgentStreamService.stream_chat()` assigns the new handle before calling
`ActiveStreamRegistry.register()`, but its exception cleanup closes only the admission when no handle
was constructed. If the registry enters `closing` between the initial `is_open` check and
`register()`, registration raises after reserve and leaves both the durable row `in_progress` and the
Session/dialogue admission held.

Repair requirements:

- every failure after a reserved handle is constructed must terminalize/close that handle even when
  registration itself failed;
- order the pre-return ownership steps so runtime close cannot produce `turn_failed` before a later
  `turn_started` trace;
- add a barrier-based registry-close/register race test asserting no model call, a non-live terminal
  persistence state, zero admission entries, zero gate permits, and zero registry handles.

### High: `start()` and `close()` do not form one atomic handle state machine

`start()` publishes `running` and the `Thread` object before calling `Thread.start()`. A concurrent
`close()` can therefore call `join()` on an unstarted thread and raise `RuntimeError`. Separately,
`close()` observes `created` without first transitioning it to a closing state; while failed
terminal persistence is blocked, another thread can still successfully call `start()`. The observed
result was a terminal event written to a sink after that sink had already been closed.

Repair requirements:

- introduce an explicit atomic starting/closing transition, or start the producer while the handle
  condition still prevents a closer from observing an unstarted producer;
- a closer that wins from `created` must make all later `start()` calls fail before it performs
  persistence or other external work;
- add deterministic barrier tests for both `running-before-Thread.start()` and
  `created-close-before-start` interleavings, including terminal ordering and resource release.

### High: completed replay can be converted into a new failed domain terminal

Completed replay sends stored assistant deltas through the generic producer exception path. A
single replay sink timeout is mapped to `MODEL_TIMEOUT` and emits `turn.failed`, even though the
immutable durable outcome is completed. Cancellation or delivery failure during replay can create
the same mismatch. This violates the frozen `completed -> replay completed` idempotency contract.

Repair requirements:

- completed/failed replay terminal identity must always derive from the immutable durable record;
- replay delivery failure or cancellation may prevent network delivery and may produce a safe App
  Log delivery status, but must not synthesize a different business terminal;
- add completed-replay slow-consumer/disconnect/cancel tests and failed-replay equivalents, proving
  the Provider is not called and the durable/domain terminal identity is not rewritten.

Independent Manager verification:

- focused S1-S4/runtime/store suite: 135 passed;
- full suite: 778 passed, 1 existing skip;
- `python -m ruff check app tests`, `python -m compileall -q app tests`, and `git diff --check`:
  passed;
- three deterministic fault probes reproduced the defects above. Existing tests do not cover those
  interleavings.

S4 remains unaccepted and T-0143 remains `in_progress`. S5-S7 are blocked until the worker repairs
these findings and Manager re-review accepts the S4 checkpoint.

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
