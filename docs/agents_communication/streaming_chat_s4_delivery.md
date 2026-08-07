# T-0143 Streaming Chat S4 Delivery

Date: 2026-08-05

Status: **S4 Accepted by Manager / T-0143 In Progress**

Baseline: `44eddf3`

## Scope

This delivery implements only the authorized S4 runtime slice:

- provider-neutral application stream events and `AgentStreamService`;
- fingerprint v1 and bounded per-key coordination;
- transferable dialogue admission handles;
- `ChatStreamHandle`, cancellation, terminal arbitration, and `ActiveStreamRegistry`;
- S2 persistence orchestration, bounded startup orphan recovery, and runtime composition;
- shared-LLM-gate streaming admission and bounded response accumulation;
- streaming-safe Trace and summary-only, best-effort Prompt Log records.

It does not add a FastAPI streaming route, SSE framing, heartbeat, `AsyncStreamBridge`, Caddy changes,
frontend changes, a production Web Profile, model fallback, or persistence schema changes. It does
not change the Manager-owned T-0143 status or review conclusion.

## Manager Changes-Requested Repairs

The 2026-08-05 S4 checkpoint identified three blocking concurrency/replay defects. This revision
changes only `AgentStreamService`, its handle implementation, and S4 tests:

1. A handle remains service-owned until successful active-registry registration and return. Any
   exception after construction now cancels and closes it, so a registry-close/register race CASes
   a reserved record out of `in_progress` and releases dialogue admission. For reserved turns,
   `turn_started` Trace is recorded before registry registration; runtime-close cleanup therefore
   cannot record `turn_failed` before a later `turn_started`.
2. Handle state is one condition-protected
   `CREATED -> STARTING/RUNNING -> CLOSING -> CLOSED` machine. `Thread.start()` occurs while the
   condition excludes closers from observing an unstarted producer. A close that wins from
   `CREATED` publishes `CLOSING` before persistence or Trace work, so every later `start()` fails.
   Concurrent close wait is bounded by the existing producer join timeout.
3. Completed and failed replays bypass new-turn failure terminalization. Backpressure, disconnect,
   or cancellation may abort delivery and writes only the safe
   `agent_stream.replay_delivery_aborted` App Log summary; it never emits a different business
   terminal or mutates the immutable S2 record. A later healthy replay still returns the original
   completed/failed terminal without a Provider call.

No persistence schema, API/SSE adapter, Caddy, frontend, production profile, or S5-S7 code was
changed for these repairs.

## Interfaces

### Application contracts

`app/services/stream_schemas.py` adds:

- `StreamChatRequest`;
- `TurnStarted`, `ResponseDelta`, `TurnCompleted`, and `TurnFailed`;
- `PublicStreamError` and the `ChatStreamEvent` union;
- `StreamEventSink`, `ChatStreamHandle`, and `AgentStreamServicePort`;
- `StreamConsumerSlowError` for bounded producer-to-consumer backpressure.

Message, metadata, client id, and delta fields are excluded from object representations where they
could expose content. Domain DTOs do not import HTTP, FastAPI, SSE, cookies, headers, or DeepSeek
chunk types.

### Fingerprint v1

`fingerprint_chat_request_v1()` implements the accepted version-1 algorithm:

- domain-separated SHA-256;
- only exact requested `character_id`, exact `message`, and validated client metadata;
- type-tagged null/bool/int/finite-float/string/array/object nodes;
- float64 big-endian lowercase hex, including negative zero;
- object keys sorted by UTF-8 bytes and arrays kept in source order;
- no normalization, trimming, server metadata, request ID, Auth/IP data, timestamp, runtime ID, or
  idempotency-scope field.

Committed golden vectors cover Unicode, key ordering, nested values, int/float, and negative zero.
Service tests prove that changing `request_id` replays while changing the business payload
conflicts.

### Runtime Guardrails

`DialogueAdmissionPort.open_handle()` now returns a thread-safe, idempotently closeable handle. The
existing `acquire()` context-manager facade creates that handle, yields the same timing lease, and
closes it in `finally`, so CLI and synchronous `/v1/chat` behavior remain unchanged.

`ConcurrencyGate.open_handle()` supplies the matching cross-thread permit primitive.
`LimitedStreamingChatModelClient` holds the same shared LLM gate used by synchronous dialogue and
memory extraction from upstream open through iterator completion and response close.

## Handle, Registry, and Terminal State Machines

### Chat stream handle

```text
CREATED
  -> STARTING -> RUNNING     start(sink), exactly once; Thread.start is condition-protected
  -> CLOSING -> CLOSED       close wins before start and terminalizes a reserved turn

RUNNING
  -> RUNNING                 zero or more response.delta
  -> CLOSING -> CLOSED       exactly one durable terminal decision and cleanup
```

`started_event` is created by the service and remains the first application-domain event. The S5
adapter will place it into its loop-owned bridge before calling `start()`; S4 does not implement that
bridge or SSE framing.

A lock-protected terminalization flag prevents concurrent close/failure paths from running two
terminal workflows. Durable `complete()`/`fail()` CAS remains the final authority:

- completed CAS wins: emit only `turn.completed`;
- failed/cancel CAS wins: discard the accumulator and emit only `turn.failed`;
- terminal delivery failure never changes an already durable outcome.

`CancellationToken` accepts only `user_cancelled`, `client_disconnected`,
`sse_generator_closed`, `consumer_slow`, or `runtime_closing`. Repeated cancellation and close are
idempotent; invalid arbitrary reasons are rejected.

### Per-key registry

The process-local key is exactly `(user_id, session_id, client_message_id)`. Entries are
reference-counted, deleted at zero references, and fixed to a hard cap of 128. A new key at capacity
fails closed with `RUNTIME_BUSY`; waiting for an existing key uses the configured Session-wait
budget.

### Active stream registry

```text
OPEN -> CLOSING -> CLOSED
```

The registry rejects new handles after closing starts, cancels every registered handle with
`runtime_closing`, closes them outside its registry lock, clears ownership, and tolerates repeated or
concurrent `close()`. It can distinguish a live local in-progress turn from a current-runtime orphan.

## Persistence and Replay

Pre-header S4 ordering is:

```text
validate/copy client payload
  -> fingerprint v1
  -> merge request_id only into turn metadata
  -> existing input/metadata limits
  -> per-key entry
  -> dialogue admission handle
  -> shared engine Character resolution
  -> S2 resolve_or_reserve transaction
  -> reserved: full engine preflight and turn_started Trace
  -> active-handle registration
  -> replay/conflict/in-progress/orphan outcome
```

Character resolution is a narrow shared `DialogueTurnEngine` stage. It allows S2 to revalidate
Session lifecycle before full preflight, which preserves the accepted rule that ended Sessions may
read terminal replay while archived Sessions never return historical content. New active turns still
use the same full engine preflight and preparation path; no streaming Controller was added.

Outcomes:

- `reserved`: user plus `in_progress` already committed; later starts the real streaming model;
- `replay_completed`: re-slices the durable assistant without using the model or LLM gate;
- `replay_failed`: replays the durable public failure without using the model;
- `conflict`: raises non-retryable `CHAT_REQUEST_CONFLICT` before stream start;
- live `in_progress`: raises retryable `CHAT_REQUEST_IN_PROGRESS` before stream start;
- current-runtime in-progress without a live registry handle: CAS to
  `CHAT_STREAM_INTERRUPTED/runtime_orphaned`, then failed replay;
- foreign-runtime in-progress: handled by the accepted S2 orphan contract.

API runtime startup calls the S2 foreign-owner orphan recovery exactly once with a configured limit
of 1-500 (default 100). CLI runtime startup does not recover API stream records.

## Producer and Resource Ownership

For a new turn the producer performs:

```text
engine.prepare(existing durable user_message_id)
  -> shared LLM gate acquire
  -> StreamingChatModelClient.open_stream
  -> bounded exact-text accumulation and Unicode-safe public delta splitting
  -> provider completion proof
  -> upstream response close
  -> LLM gate release
  -> engine.decide_completion
  -> S2 assistant + latest relationship + completed commit
  -> dialogue admission release
  -> work-bounded post-commit Memory persist/enqueue (at most one call each)
  -> streaming-safe Trace and summary Prompt Log
  -> turn.completed
  -> sink close and registry deregistration
```

Response bounds default to 32,768 characters, 98,304 UTF-8 bytes, and 4,096 UTF-8 bytes per public
delta. Configuration may only lower these frozen maxima and must remain positive. Exceeding either
accumulator limit cancels the upstream stream, persists `MODEL_INVALID_RESPONSE` with the
provider-neutral `model_invalid_response` reason, and saves no partial assistant.

The producer put deadline defaults to five seconds. A slow sink becomes non-retryable
`CHAT_STREAM_CANCELLED/consumer_slow`. Provider partial responses stay only in the in-memory
accumulator and are discarded on every failed/cancelled path.

Post-commit Memory work runs only after admission release and is work-bounded to one decision
persist plus one window-enqueue check. Its failure writes only a safe App Log summary; it cannot
reverse a durable completed turn or change replay.

Provider persistence reasons are explicitly collapsed:

- `MODEL_INVALID_RESPONSE` -> `model_invalid_response`;
- every other model error -> `model_failed`;
- provider/parser-private reasons never enter S2 terminal fields.

Shutdown ownership is registered so reverse close is:

```text
ActiveStreamRegistry.close()
  -> cancel/close active stream handles
  -> DeepSeek transport close
  -> remaining older resources
```

`ApplicationRuntime.close()` remains synchronous and idempotent. The API runtime exposes
`agent_stream_service` and `active_stream_registry`; the CLI runtime exposes neither streaming entry
point nor fallback-backed streaming graph.

## Observability Safety

Streaming preflight omits client metadata values from Trace. Streaming terminal Trace records only
status, error code, counts, and lengths. Streaming Prompt Log is always summary-only and
best-effort:

- `messages=[]`;
- `sections=[]`;
- `response=None`;
- only section ids/counts, allowlisted Context Budget counts/status, and response metrics.

The Context Budget summary is accepted only when the runtime compressor is enabled and only from a
field allowlist, so client metadata cannot forge a summary containing private values. Tests use
unique prompt, metadata, credential, provider-error, response, and Memory-error markers and verify
they do not appear in logs, terminal errors, or object representations. Response text appears only
in the authorized `ResponseDelta` value and durable assistant message.

## Files Changed

Added:

- `app/services/stream_schemas.py`
- `app/services/stream_fingerprint.py`
- `app/services/agent_stream_service.py`
- `app/runtime/streaming.py`
- `tests/test_agent_stream_service.py`
- `tests/test_stream_runtime_resources.py`
- `docs/agents_communication/streaming_chat_s4_delivery.md`

Modified:

- `app/agent/turn_engine.py`
- `app/runtime/guardrails/ports.py`
- `app/runtime/guardrails/admission.py`
- `app/runtime/guardrails/concurrency.py`
- `app/runtime/guardrails/__init__.py`
- `app/runtime/resources.py`
- `app/runtime/__init__.py`
- `app/services/__init__.py`
- `app/main.py`
- `app/config.py`
- `app/errors/codes.py`
- `app/errors/http.py`
- `.env.example`
- S4-focused runtime/config/composition tests

No persistence schema, migration, model, store, API route, Caddy, frontend, production profile, or
Manager-owned task/review document was modified.

## Fault and Race Evidence

The S4 tests cover:

- fingerprint golden vectors, request-ID independence, and business-payload conflict;
- reserve, completed replay, failed replay, live in-progress, local orphan, and startup foreign
  orphan recovery;
- ended terminal replay, ended new-send rejection, archived rejection, and cross-user denial;
- normal completion, zero-delta/blank failure, output-limit warning, provider first/partial failure,
  local character/byte limits, and Unicode-safe delta splitting;
- consumer slow, cancel before completion, completion-vs-cancel CAS, close before start, repeated
  start/cancel/close, and unique terminal delivery;
- 128-key capacity and zero-reference deletion;
- cross-thread/concurrent dialogue admission and concurrency-permit close;
- same-Session serialization and one shared sync/stream LLM gate;
- response/model session, LLM permit, dialogue admission, registry, and sink cleanup;
- runtime shutdown cancellation, rejection of new streams, and registry-before-DeepSeek close order;
- post-commit Memory failure without completed-outcome reversal;
- summary-only observability and content-safe representations/errors;
- existing synchronous CLI, `/v1/chat`, S1/S2/S3, Session, deployment-config, and API regression.

The changes-requested regression additions use deterministic barriers to cover:

- registry close after reserve but before registration, proving failed durable CAS, ordered
  `turn_started -> turn_failed` Trace, zero Provider calls, and zero admission/gate/registry owners;
- close while `Thread.start()` is blocked, proving no join-before-start exception, no event after
  sink close, one terminal, and complete cleanup;
- close-from-created while failed persistence is blocked, proving a concurrent `start()` is rejected
  before any sink or Provider activity;
- completed replay timeout/disconnect/cancel and failed replay backpressure/disconnect/cancel,
  proving no synthesized terminal, no durable rewrite, no Provider call, and successful later replay
  of the original terminal identity.

## Verification

| Check | Result |
| --- | --- |
| Expanded focused S1-S4/store/runtime/API suite | PASS: 165 tests |
| Full `pytest -q` | PASS: 788 collected, 787 passed, 1 skipped |
| `ruff check app tests` | PASS |
| `python -m compileall -q app tests` | PASS |
| `git diff --check` | PASS |
| `ruff check .` | BLOCKED only by pre-existing `frontend/capture_frontend.py:17:1 I001` |

The full suite used a fresh writable local `--basetemp` and exited successfully. The collected total
was independently counted from pytest grouped collection output. No real DeepSeek, browser, SSE,
Caddy, or production-profile test was run in S4.

## Risks Remaining Before S5

1. S5 must implement the event-loop-safe bounded `AsyncStreamBridge`; S4 intentionally provides no
   asyncio queue, heartbeat, disconnect polling, or ASGI response ownership.
2. `started_event` ordering is frozen, but S5 must prove it places that event before starting the
   producer and that response-construction/send-start failures close an unstarted handle.
3. Sync HTTPX cancellation remains cooperative and bounded by upstream response close plus the
   configured read timeout; S5 must test actual disconnect propagation without blocking Uvicorn's
   event loop.
4. Post-commit Memory is work-bounded, not forcibly preemptible inside an individual synchronous
   store call. The accepted first Web Profile keeps long-term Memory disabled.
5. Registry, Session locks, gates, and live-owner detection remain single-process. Production must
   retain one Uvicorn worker.
6. Replayed answers preserve text but not original provider chunk boundaries.
7. API rate-limit sharing, Cookie/CSRF/dev-Bearer enforcement, pre/post-header error expression, and
   request-disconnect classification remain S5 work.
8. Real Caddy flushing/disconnect behavior and production Web Profile activation remain explicitly
   deferred.

S4 stops here and requests the Manager checkpoint before any S5 API/SSE implementation.
