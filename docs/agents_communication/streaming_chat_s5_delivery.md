# T-0143 Streaming Chat S5 Delivery

Date: 2026-08-06

Status: **S5 Accepted by Manager / T-0143 In Progress**

Baseline: `44eddf3`

## Scope

This delivery implements only the authorized S5 HTTP/SSE adapter slice:

- `POST /v1/chat/stream`;
- FastAPI composition for the accepted S4 `AgentStreamService`;
- a bounded, event-loop-safe `AsyncStreamBridge`;
- exact provider-neutral domain-event to SSE framing;
- 15-second adapter-only heartbeat;
- existing Auth, CSRF, request ID, and authenticated chat-rate-limit integration;
- disconnect, ASGI cancellation, generator-close, bridge, handle, and producer ownership;
- deterministic API, bridge, responsiveness, disconnect, and resource-release tests.

It does not implement S6 or S7, modify persistence schema or repositories, modify Caddy, read or
modify `frontend/`, enable a production Web Profile, add model fallback, or change the Manager-owned
T-0143 status/review conclusion.

## Manager Changes-Requested Repairs

The 2026-08-06 S5 checkpoint found two synchronous handle calls on the event loop. This revision
changes only the S5 response adapter and its tests:

1. `handle.start(sink=bridge)` now runs through `asyncio.to_thread()`. If producer
   `Thread.start()` fails, the accepted S4 failed-persistence, Trace, terminal-delivery, admission,
   and registry cleanup all execute outside the event loop. The existing bridge-created
   `turn.started` stays first, and the S4 handle supplies exactly one following `turn.failed`; the
   adapter does not invent a terminal.
2. Generator-finally and response-finally establish a lightweight, first-wins cancellation intent,
   abort the async bridge, then offload one synchronous `cancel(reason) + close()` cleanup operation.
   This keeps DeepSeek/HTTPX response close and producer join off the event loop while preserving
   `client_disconnected` versus `sse_generator_closed`. Both ownership paths remain safe through the
   idempotent handle.

The later re-review found that bridge abort could wake a blocked producer before the adapter wrote
the disconnect reason. `ChatStreamHandle.request_cancel()` now performs only the token's atomic
first write: it does not touch the Provider session or any other blocking resource. Both response
ownership paths therefore freeze `client_disconnected` before aborting pending bridge puts. The
worker-thread cleanup then calls `cancel()` to close the model transport using the already-frozen
reason and finally calls `close()`.

Deterministic barrier tests block failed persistence after injected `Thread.start()` failure and
block model-session cancellation as an HTTPX-close analogue. In both cases `/health` completes on
the same event loop before the blocking worker is released. The tests also prove the required failed
SSE terminal, durable failed state, no Provider call or partial assistant, the classified disconnect
reason, and zero admission/gate/registry ownership.

A real S4 handle plus a capacity-one bridge test holds one delta in the queue and blocks the
producer on the next put. It disconnects at that barrier and repeats the race five times, asserting
that the token, Provider cancellation, and durable terminal reason are all
`client_disconnected`, with no partial assistant and no remaining admission, gate, or registry
ownership.

## Endpoint and HTTP Boundary

`POST /v1/chat/stream` accepts:

```json
{
  "session_id": "session_...",
  "character_id": "aemeath",
  "message": "...",
  "client_message_id": "cm_...",
  "metadata": {}
}
```

The route is a synchronous FastAPI endpoint. Dependency resolution and
`AgentStreamService.stream_chat()` therefore run in FastAPI's worker thread, keeping Auth,
rate-limit admission, SQLite/idempotency work, Session admission, Character/Session preflight, and
all other blocking pre-header operations off the Uvicorn event loop.

The successful response is HTTP 200 with:

- `Content-Type: text/event-stream; charset=utf-8`;
- `Cache-Control: no-store`;
- the existing middleware-owned `X-Request-ID`;
- no `Content-Length`, hop-by-hop `Connection`, SSE `id:`, or `retry:` field.

All failures raised before response construction continue through the existing JSON `ErrorResponse`
mapper with their real HTTP status, `X-Request-ID`, and `Retry-After` where applicable. This includes
validation, Auth/CSRF, rate limits, idempotency conflict/in-progress, lifecycle, ownership, and S4
preflight failures.

After headers, model/parser/persistence/cancellation failures remain HTTP 200 and are expressed only
through the S4 `turn.failed` domain terminal. The adapter does not invent a second business terminal
and does not let body-iterator exceptions escape as provider bodies or tracebacks.

## SSE Framing and Ordering

`encode_sse_event()` emits compact, one-line UTF-8 JSON with one blank line after every frame:

```text
event: turn.started
data: {"request_id":"req_...","turn_id":"turn_...","session_id":"session_..."}

event: response.delta
data: {"delta":"..."}

event: turn.completed
data: {"turn_id":"turn_...","status":"completed","warnings":[],"state_delta":{...}}

```

The response binds the bridge on the running event loop, enqueues the handle's existing
`started_event` with loop-side `put_nowait()`, and only then starts the S4 producer. All later events
enter the same FIFO queue, freezing the sequence as:

```text
turn.started -> response.delta* -> exactly one turn.completed | turn.failed
```

Newlines in delta text are JSON-escaped. Prompt, reasoning, provider chunks, final-response copies,
Memory/Evidence/Character content, metadata, credentials, vectors, and raw exceptions never enter
SSE frames. The response delta is the only authorized response-text output.

When the bridge is idle after `turn.started`, the adapter emits exactly:

```text
: heartbeat

```

at a fixed 15-second interval. Heartbeat is not placed in the bridge, domain event union,
persistence, Trace, Prompt Log, or terminal/event counters.

## AsyncStreamBridge

`AsyncStreamBridge` is created only on its owning running loop and contains an
`asyncio.Queue(maxsize=32)`.

Producer-thread puts use `asyncio.run_coroutine_threadsafe()` and wait only in the producer thread
for the S4-supplied bounded put deadline (five seconds in the frozen profile). The Uvicorn event loop
never calls blocking `queue.Queue.get()`, synchronous iterator `next()`, `Future.result()`,
`time.sleep()`, model code, SQLite, or synchronous `handle.start/cancel/close` work.

The bridge tracks every cross-thread future. Queue timeout cancels the pending future and raises
`StreamConsumerSlowError`, which the S4 producer maps to non-retryable
`CHAT_STREAM_CANCELLED/consumer_slow`. Abort is thread-safe and idempotent: it marks the bridge
closed, cancels pending producer puts, clears the nonterminal backlog, and inserts a private wake-up
sentinel. Producer-close uses a different private sentinel so completed/failed terminal delivery is
not rewritten. Neither sentinel is serializable as a domain event or SSE frame.

## Owned Response and Cancellation

`OwnedStreamingResponse` owns the bridge and adapter-side handle cleanup. It observes:

- `Request.is_disconnected()` before every queue wait and after heartbeat timeout;
- Starlette/ASGI `http.disconnect` through an observed receive wrapper;
- body-task `CancelledError`;
- generator close;
- send/body `OSError` and adapter errors.

Cleanup is idempotent and follows:

```text
classify client_disconnected | sse_generator_closed
  -> handle.request_cancel(reason) [atomic intent only]
  -> bridge.abort_from_any_thread()
  -> asyncio.to_thread(handle.cancel(reason) + handle.close())
  -> producer closes upstream response/session
  -> shared LLM permit release
  -> dialogue admission release
  -> sink close and active-registry deregistration
```

The response-level `__call__` is the final owner as well as the generator `finally`. This closes a
real ASGI timing window where the response task could return before async-generator finalization and
temporarily leave dialogue admission live. Repeated close/cancel remains safe through the accepted
S4 idempotent handle state machine.

If durable completion wins before disconnect, S4 completion remains immutable. If disconnect wins
before commit, the partial accumulator is discarded and no assistant message is stored.

## Auth, CSRF, Rate Limit, and Request ID

The endpoint directly reuses `get_rate_limited_user_context`:

- `web_session` accepts the existing secure Cookie flow and requires existing Origin/Referer,
  Fetch Metadata, JSON content type, and `X-CSRF-Token` checks;
- `dev_bearer` accepts the existing Bearer compatibility mode and does not require CSRF;
- Cookie in dev mode, Authorization in web-session mode, duplicate Cookie, or Cookie plus Bearer
  continues to fail closed; there is no hybrid selection;
- sync `/v1/chat` and stream `/v1/chat/stream` consume the same user/IP rate-limit buckets;
- the existing validated/generated request ID appears in the HTTP header and `turn.started` event,
  while the durable business fingerprint remains independent of request ID.

No CORS allowlist or production Web Profile was changed.

## Observability Safety

Adapter App Logs contain only request/turn/session/user identifiers plus safe event type, delivery
status, or exception class. They do not contain delta text, user input, metadata, prompt, reasoning,
provider body/chunk, Cookie, token, API key, Character/Memory/RAG content, vector, or raw exception
text. Heartbeats are not logged as turn events.

The existing S4 service remains the authority for durable terminal, Trace, Prompt summary, chunk
counts, response character/byte counts, and persistence-reason mapping.

## Files Changed

Added:

- `app/api/sse.py`;
- `app/api/routes/chat_stream.py`;
- `tests/test_api_streaming.py`;
- `docs/agents_communication/streaming_chat_s5_delivery.md`.

Modified:

- `app/api/dependencies.py` to expose the accepted S4 stream service in `ApiRuntime`;
- `app/api/schemas.py` for the HTTP-only stream request body;
- `app/api/routes/__init__.py` and `app/api/server.py` for route composition;
- `app/services/stream_schemas.py` for the additive, provider-neutral lightweight cancellation-intent
  method on `ChatStreamHandle`;
- `app/services/agent_stream_service.py` so intent and transport cancellation are separate,
  thread-safe phases and each concrete model session is cancelled at most once.

No S5 change was made to persistence schema/repositories, model/provider parsing,
DialogueController, runtime guardrails, Caddy, deployment, frontend, production profile, or
Manager-owned task/review documents.

## Test Evidence

S5 tests cover:

- exact headers, compact UTF-8 frames, JSON newline escaping, and omission of `id:`/`retry:`;
- `turn.started -> multiple response.delta -> turn.completed` ordering;
- Provider failure before the first delta becoming post-header `turn.failed` without private-body
  leakage;
- pre-header Pydantic validation and idempotency conflict remaining JSON `ErrorResponse`;
- Browser Cookie Auth, CSRF rejection/success, dev Bearer, and hybrid credential rejection;
- one shared authenticated sync/stream rate-limit bucket with `Retry-After`;
- fixed 15-second heartbeat and proof that it is adapter-only;
- bridge capacity/backpressure while the event loop continues to run;
- abort waking both an idle consumer and a pending producer immediately;
- one idle/blocking stream while `/health` and authenticated `/v1/sessions` complete under bounded
  deadlines on the same event loop;
- real ASGI disconnect propagation, upstream cancel/close, no partial assistant persistence, and
  zero remaining dialogue admission, shared LLM gate, or active-registry ownership;
- injected producer `Thread.start()` failure while failed persistence is barrier-blocked, proving
  responsive `/health`, exact `turn.started -> turn.failed`, failed durability, no Provider call,
  no assistant message, and complete resource release;
- barrier-blocked model cancellation, proving `/health` remains responsive while synchronous
  transport close runs off-loop and the eventual cancellation reason is `client_disconnected`;
- a real handle with a capacity-one bridge and pending producer put, repeated five times, proving
  disconnect intent wins before bridge abort and durable/Trace classification cannot degrade to
  `sse_generator_closed`;
- the ASGI disconnect race repeated ten consecutive times without leakage;
- all existing synchronous API, CLI/service, S1-S4, model, runtime, and store contracts.

## Verification

| Check | Result |
| --- | --- |
| Focused S1-S5/API/Auth/runtime/store suite | PASS: 190 tests |
| Full `pytest -q` | PASS: 800 collected, 799 passed, 1 existing skip |
| `python -m ruff check app tests` | PASS |
| `python -m compileall -q app tests` | PASS |
| `git diff --check` | PASS |
| `python -m ruff check .` | BLOCKED only by pre-existing `frontend/capture_frontend.py:17:1 I001` |

The full suite and focused suite used fresh writable local `--basetemp` directories. No real
DeepSeek, browser UI, reverse proxy, Caddy, production profile, or multi-worker test was run.

## Risks and Deferred Before S6

1. Caddy's documented automatic `text/event-stream` flush and real downstream disconnect behavior
   still require the separately scoped proxy/deployment test; S5 intentionally did not change
   Caddy or configure `flush_interval -1`.
2. Real sync HTTPX cancellation remains cooperative and bounded by response close/read timeout. S5
   now performs that blocking close in a cleanup worker; deterministic tests use a barrier-blocked
   cancellable fake rather than a real socket.
3. The existing API middleware duration ends when the response adapter is returned. Full stream
   duration and terminal status remain in S4/S5 stream logs, not `api.request_completed`.
4. Bridge, registry, admission, gates, and idempotency live-owner detection remain single-process;
   production must retain one Uvicorn worker.
5. v0.1 has no `Last-Event-ID`, offset resume, EventSource GET, or partial-delta replay. A reconnect
   repeats the immutable durable completed/failed answer under the existing idempotency key.
6. Frontend fetch cancellation, visible Stop/Retry UX, production Web Profile activation, and real
   browser/proxy E2E remain outside S5.

S5 stops here and requests the Manager checkpoint. S6-S7 remain blocked.
