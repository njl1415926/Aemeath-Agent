# T-0143 Streaming Chat Runtime And API v0.1 Final Delivery

Date: 2026-08-06

Status: **S7 Accepted by Manager / T-0143 Done**

Baseline: `44eddf3`

Manager final review accepted this delivery and marked T-0143 `done`.

## Final Scope

S7 adds no business or runtime capability. It performs the authorized S1-S6 focused regression,
the complete suite with real Caddy 2.10.2, static/build/deployment-lock validation, and final
documentation alignment within Worker ownership.

No S7 change was made to persistence schema, `frontend/`, production Web Profile, Caddy topology or
Caddyfile, model fallback policy, Character/Memory/RAG/Embedding behavior, synchronous CLI/chat
behavior, or Manager-owned review/task status.

## Delivered Interfaces

### Shared turn orchestration

`DialogueTurnEngine` owns shared Character/Session validation, Semantic/Memory/Evidence collection,
prompt compilation, state/memory decisions, and Trace/Prompt lifecycle. The existing
`DialogueController.handle_user_message()` remains the synchronous compatibility facade; streaming
does not copy or fork a second Controller flow.

### Provider-neutral model streaming

- `StreamingChatModelClient.open_stream(ModelStreamRequest) -> ContextManager[ModelStreamSession]`;
- `ModelStreamSession` is iterable, cancellable, and closeable;
- public model events are `ModelTextDelta` and `ModelStreamFinished(complete|output_limit)`;
- DeepSeek `[DONE]` is framing only, finish reasons are validated, blank output fails, and
  `reasoning_content` is never emitted, persisted, or logged;
- the existing synchronous `ChatModelClient` remains unchanged and supported.

### Persistence and idempotency

`StreamTurnPersistencePort` provides atomic `resolve_or_reserve`, `complete`, `fail`, and bounded
orphan recovery across raw SQLite and SQLAlchemy-on-SQLite. Fingerprint v1 covers only original
`character_id`, message, and validated client metadata. Its scope is
`(user_id, session_id, client_message_id)`; request ID and server/auth metadata are excluded.

The completion unit of work atomically commits the assistant message, latest relationship-state
delta application, and completed idempotency record. Failure/cancellation stores no partial
assistant. CAS protects completed-vs-cancel races and replay never invokes the Provider again.

### Streaming service and handles

`AgentStreamServicePort.stream_chat(...) -> ChatStreamHandle` returns a handle with:

- immutable `turn.started` identity;
- `start(sink)`;
- transport-free, first-wins `request_cancel(reason)`;
- blocking-capable `cancel(reason)` and idempotent `close()`.

Domain events are exactly `turn.started`, `response.delta`, `turn.completed`, and `turn.failed`.
`ActiveStreamRegistry`, the 128-entry reference-counted per-key registry, dialogue admission lease,
global LLM gate, Provider session, sink, and producer thread have explicit idempotent owners.

### HTTP/SSE adapter

`POST /v1/chat/stream` accepts `session_id`, `character_id`, `message`, `client_message_id`, and
validated metadata. It reuses the existing Cookie/Bearer Auth, CSRF/provenance, shared chat rate
limit, and request-ID boundaries without hybrid authentication.

Successful preflight returns `text/event-stream` in the order:

```text
turn.started -> response.delta* -> exactly one turn.completed | turn.failed
```

The adapter emits a content-free comment heartbeat every 15 seconds. Pre-header failures retain the
normal HTTP JSON `ErrorResponse`; post-header business failures use only `turn.failed`. Raw ASGI
`OSError`/`ClientDisconnect` after headers becomes `client_disconnected`, never escapes the full app
as a traceback, and cannot rewrite a durable completion.

## Final Runtime Chain

```text
CLI or POST /v1/chat
  -> UserContext -> AgentService -> DialogueController
  -> shared DialogueTurnEngine -> synchronous ChatModelClient

POST /v1/chat/stream
  -> Cookie/Bearer Auth + CSRF/provenance + shared rate limit + request ID
  -> AgentStreamService
  -> fingerprint/key registry -> resolve_or_reserve -> dialogue admission
  -> shared DialogueTurnEngine preflight/prepare
  -> global LLM gate -> StreamingChatModelClient -> Provider session
  -> bounded AsyncStreamBridge -> SSE adapter
  -> completion UoW -> bounded best-effort post-commit Memory -> completed
     or fail/cancel CAS -> failed
  -> response/Provider/gate/admission/registry cleanup
```

Application shutdown closes `ActiveStreamRegistry` before DeepSeek transport, rejects new streams,
cancels active streams, and remains synchronous and idempotent.

## Complete T-0143 File Inventory

Shared orchestration and model streaming:

- `app/agent/dialogue_controller.py`
- `app/agent/turn_engine.py`
- `app/model/__init__.py`
- `app/model/deepseek.py`
- `app/model/mock.py`
- `app/model/mock_streaming.py`
- `app/model/streaming.py`

Persistence:

- `app/db/stream_turns.py`
- `app/db/streaming_chat_migrations.py`
- `app/db/repositories/stream_turns.py`
- `app/db/repositories/__init__.py`
- `app/db/schema.sql`
- `app/db/models.py`
- `app/db/session.py`
- `app/db/sqlite_store.py`
- `app/db/sqlalchemy_store.py`

Streaming service, guardrails, runtime, config, and errors:

- `app/services/stream_schemas.py`
- `app/services/stream_fingerprint.py`
- `app/services/agent_stream_service.py`
- `app/services/__init__.py`
- `app/runtime/streaming.py`
- `app/runtime/guardrails/ports.py`
- `app/runtime/guardrails/admission.py`
- `app/runtime/guardrails/concurrency.py`
- `app/runtime/guardrails/__init__.py`
- `app/runtime/resources.py`
- `app/runtime/__init__.py`
- `app/main.py`
- `app/config.py`
- `app/errors/codes.py`
- `app/errors/http.py`
- `.env.example`

HTTP/SSE and development dependency integration:

- `app/api/sse.py`
- `app/api/routes/chat_stream.py`
- `app/api/routes/__init__.py`
- `app/api/dependencies.py`
- `app/api/schemas.py`
- `app/api/server.py`
- `pyproject.toml`

Primary tests:

- `tests/test_dialogue_turn_engine.py`
- `tests/test_model_streaming.py`
- `tests/store_contract/test_streaming_chat_contract.py`
- `tests/test_streaming_chat_migrations.py`
- `tests/test_agent_stream_service.py`
- `tests/test_stream_runtime_resources.py`
- `tests/test_api_streaming.py`
- `tests/test_runtime_admission.py`
- `tests/test_runtime_concurrency.py`
- `tests/test_runtime_composition.py`
- `tests/test_runtime_config.py`
- `tests/deployment/test_ingress_contract.py`
- `tests/deployment/test_caddy_body_limit_integration.py`
- `tests/deployment/test_caddy_streaming_integration.py`

Worker design/delivery and aligned operational documentation:

- `docs/agents_communication/streaming_chat_runtime_v0_1_design.md`
- `docs/agents_communication/streaming_chat_s1_s3_delivery.md`
- `docs/agents_communication/streaming_chat_persistence_s2_delivery.md`
- `docs/agents_communication/streaming_chat_s4_delivery.md`
- `docs/agents_communication/streaming_chat_s5_delivery.md`
- `docs/agents_communication/streaming_chat_s6_delivery.md`
- `docs/agents_communication/streaming_chat_s7_final_delivery.md`
- `docs/fastapi_service.md`
- `docs/observability.md`
- `docs/error_handling.md`

## Documentation and Configuration Audit

Aligned in S7:

- S1-S6 Worker delivery status lines reflect their phase acceptance; this final report and the
  Manager-owned status documents now record T-0143 as `done`;
- `docs/fastapi_service.md` now documents the live stream endpoint, shared limiter, SSE/error and
  Auth boundaries, and summary-only streaming Prompt Log;
- `docs/observability.md` now records the streaming summary-only and disconnect-classification
  contracts;
- `docs/error_handling.md` now lists stable stream/idempotency codes and the post-header boundary;
- `.env.example` contains every S4 streaming limit with the same defaults as `Settings`;
- `deploy/Caddyfile` still has exact `128KiB`, h1/h2 only, no h3, no access log, and no
  `flush_interval -1`;
- `pyproject.toml` adds h2 only to the development extra; deployment lock remains unchanged and
  valid.

During final acceptance, Manager reconciled `docs/architecture.md`, `docs/interfaces.md`, and
`docs/web_mvp_contract.md` with the implemented streaming path, `ChatStreamHandle`, active endpoint,
and remaining T-0144 through T-0146 gates. Manager also updated `docs/task_log.md` and the Manager
review; the Worker did not modify those files during S7 implementation.

## Final Regression Evidence

The 331-test focused selection covers:

- shared turn characterization and synchronous `DialogueController`, `AgentService`, CLI errors,
  and `/v1/chat` compatibility;
- DeepSeek sync/stream parsing, finish reasons, blank/reasoning handling, cancellation, and close;
- both persistence backends, migrations, idempotency replay/conflict/in-progress, Session lifecycle,
  ownership, and completion/failure CAS;
- stream handle/registry races, same-Session admission, global LLM gate, consumer pressure, shutdown,
  resource release, and content-safe observability;
- `/v1/chat/stream` ordering, heartbeat, Auth, CSRF, request ID, shared limiting, disconnect/send
  faults, and durable-terminal preservation;
- real Caddy 2.10.2 HTTP/1.1 and HTTP/2 streaming, heartbeat, disconnect propagation, h1/h2 ALPN,
  no h3, forwarding behavior, and 128 KiB body enforcement;
- deployment composition, production configuration, Docker context, preflight, and deploy lock.

Final results:

| Check | Result |
| --- | --- |
| S1-S6 focused regression with real Caddy 2.10.2 | PASS: 331 tests |
| Complete `pytest -q` with real Caddy 2.10.2 | PASS: 805 tests |
| `python -m ruff check app tests` | PASS |
| `python -m compileall -q app tests` | PASS |
| `python scripts/deployment/check_deploy_lock.py` | PASS |
| `git diff --check` | PASS |

The global `python -m ruff check .` remains outside the requested acceptance command and reports only
the pre-existing forbidden `frontend/capture_frontend.py:17:1 I001`; S7 did not read or modify that
directory.

## Known Limits and Pre-production Deferred

1. v0.1 is single-process: one Uvicorn worker is mandatory while admission, active-stream registry,
   live idempotency ownership, and rate limits are process-local.
2. Reconnection is request replay, not byte-offset resume. There is no `Last-Event-ID`, delta offset,
   EventSource GET endpoint, or partial-response persistence.
3. Provider cancellation is cooperative and bounded by HTTPX response close/read behavior.
4. Real Caddy validation uses localhost TLS and a deterministic synthetic upstream. Public DNS,
   production certificates, SLB/WAF/proxy chains, browser soak, and real DeepSeek remain D8/E2E work.
5. Production Web Profile activation, Session API completion, frontend Stop/Retry UX, usage quota,
   CSP/static frontend integration, and browser security E2E remain T-0144 through T-0146.
6. D6 backup/restore and broader deployment completion remain outside T-0143.
7. PostgreSQL, Redis/distributed coordination, multi-worker streaming, model fallback, LangGraph,
   tools, audio, and multimodal streaming remain deferred.

S7 stops here. Manager final review accepted the delivery and changed T-0143 from `in_progress` to
`done`.
