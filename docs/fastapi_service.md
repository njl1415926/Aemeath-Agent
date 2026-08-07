# FastAPI Service v0.1

FastAPI is an HTTP adapter around the existing application service boundary:

```text
HTTP routes
  -> AuthService / LocalDefaultAccountResolver
  -> UserContext
  -> AgentStreamService / AgentService / SessionService
  -> existing controller and store ports
```

`DialogueController` does not handle HTTP, headers, cookies, tokens, account status, or API
logging.

## Runtime

`app.api.server.create_app(settings: Settings | None = None)` builds one explicit
`ApplicationRuntime` with `surface="api"`. FastAPI lifespan closes its owned DeepSeek and
Embedding resources exactly once; adapters never inspect Controller or wrapper internals.

Run locally with:

```bash
pip install -e .[dev,api]
uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000
```

The deployment image instead runs `scripts/deployment/start_api.py`. That launcher fixes
`workers=1`, disables reload and access logs, enables proxy-header processing only for
`AEMEATH_TRUSTED_PROXY_IPS`, and gives in-flight requests a 60-second graceful shutdown window.
Production requires exactly one fixed Caddy IP and rejects wildcards, networks, or multiple
trusted addresses.

Runtime Guardrails v0.1 is process-local. Production must run exactly one Uvicorn worker. A
multi-worker launch gives each process independent Session locks, concurrency limits, and rate
buckets and is therefore outside the v0.1 guarantee.

`AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS` must remain exactly `1` in v0.1. The inner synchronized
Embedding adapter is intentionally serial and does not provide a second bounded queue.

## Auth

`GET /health` and `GET /ready` are public low-sensitivity probes. `/v1/*` endpoints require
`Authorization: Bearer <dev token>` by default.
The raw Authorization header is passed to `AuthService.resolve_bearer_token(...)`; raw tokens are
not logged or returned.

Local default-user access is disabled by default:

```env
AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER=false
```

It may be enabled explicitly for local development only.

API prompt-run logging is disabled by default even though CLI prompt-run logging keeps its
historical local-debug default:

```env
AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false
AEMEATH_API_ENABLE_MODEL_FALLBACK=false
```

Synchronous prompt logs contain full prompt/messages/sections/response snapshots and should be
treated as sensitive local debugging artifacts. Streaming prompt logs are always summary-only:
their messages and sections are empty and response/error bodies are omitted. Enable API prompt logs
only for controlled debugging.

`AEMEATH_DEV_AUTH_TOKEN` is treated as the configured dev-token source of truth in development.
Production accepts only `AEMEATH_DEV_AUTH_TOKEN_FILE`; the corresponding DeepSeek contract is
`DEEPSEEK_API_KEY_FILE`. The application, not Compose, implements this `_FILE` convention. It
rejects dual sources and never includes the resolved value in `Settings` repr or errors. When it is unset
on startup, the configured `AEMEATH_DEV_AUTH_USER_ID` has its stored `dev_token_hash` cleared.

API model fallback is disabled by default. Provider auth, rate-limit, timeout, unavailable, or
invalid-response failures should surface through the shared API error shape instead of returning a
mock fallback response. CLI runtime keeps model fallback enabled by default for local continuity.
Setting `AEMEATH_API_ENABLE_MODEL_FALLBACK=true` is rejected as `CONFIG_INVALID`.

## Runtime limits

`POST /v1/chat` and `POST /v1/chat/stream` share one authenticated in-process user token bucket and an
auxiliary client-IP bucket. The defaults are `12/minute, burst 4` per user and `60/minute, burst
20` per IP. Rejections return `API_RATE_LIMITED` (HTTP 429) with integer `Retry-After`. CLI does
not pass through this HTTP limiter.

The shared `AgentService` rejects messages over 4,000 characters or 12,000 UTF-8 bytes and
metadata whose deterministic JSON encoding exceeds 8,192 UTF-8 bytes. These checks happen before
message persistence and provider calls. They do not limit the raw HTTP body before JSON parsing.
Production must configure a reverse-proxy request-body limit with suitable JSON/envelope headroom.
The approved D3 Caddy contract sets the raw limit to exactly `128 KiB` before proxying. It remains
separate from the parsed message and metadata limits.

The deployment may use Uvicorn proxy-header processing only for explicitly enumerated trusted
proxy addresses. Do not use `--forwarded-allow-ips="*"`; when there is no trusted proxy, disable
proxy headers. `request.client.host` after that deployment boundary is the auxiliary IP-rate key
and is never written to logs.

## Endpoints

```text
GET  /health
GET  /ready
POST /v1/chat
POST /v1/chat/stream
GET  /v1/sessions
POST /v1/sessions
GET  /v1/sessions/{session_id}/messages
```

`/health` is liveness only and never touches storage or Providers. `/ready` uses an injected narrow
readiness port: the lifespan-owned state must be `ready`, the configured SQLite file must pass a
short read-only schema check, and required filesystem roots must pass a cached non-writing
free-space check. Both `sqlite_raw` and the existing dev/test SQLAlchemy-on-SQLite backend have
backend-appropriate schema markers; production still permits only `sqlite_raw`. It has a 500 ms
total deadline, a 200 ms DB budget, and a 5-second filesystem cache by default. Failure is `503`
with only a stable reason enum. Neither endpoint calls DeepSeek, SiliconFlow, embeddings, or other
paid Providers; both send `Cache-Control: no-store`.

Production CORS is fixed to the empty list. Dev/test may provide a JSON array through
`AEMEATH_API_CORS_ALLOWED_ORIGINS`; accepted origins are exact, credentials are disabled, methods
are limited to `GET, POST, OPTIONS`, and headers are limited to `Authorization, Content-Type,
X-Request-ID`.

`POST /v1/chat` calls `AgentService.handle_chat(...)`. It does not call `DialogueController`
directly. If the supplied `session_id` does not exist, v0.1 follows the existing
`SessionStorePort.ensure_session(...)` behavior and creates the session during the chat turn.

`POST /v1/chat/stream` calls `AgentStreamService.stream_chat(...)` and requires a bounded
`client_message_id`. A successful preflight returns UTF-8 `text/event-stream` in the order
`turn.started -> response.delta* -> turn.completed | turn.failed`; 15-second heartbeat comments are
HTTP-adapter framing rather than domain events. Pre-header failures use the normal JSON error
envelope. Post-header failures use the single safe terminal event, and client disconnect triggers
first-wins cancellation plus off-loop Provider/handle cleanup. Cookie Web Auth requires the existing
CSRF/provenance checks; dev Bearer remains a separate non-hybrid mode.

Session routes use a thin `SessionService`. Message listing verifies that the session belongs to
the current user and character before reading messages.

## Errors

API errors reuse `map_exception(...)` and `http_status_for_error(...)`.

```json
{
  "error": {
    "code": "SESSION_ENDED",
    "message": "Session has ended. Resume it before sending messages.",
    "retryable": false
  },
  "request_id": "req_..."
}
```

FastAPI validation errors are mapped to `INVALID_INPUT`; the default FastAPI `detail` payload is
not exposed.

## Logging

`X-Request-ID` is accepted only when it matches
`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`. Missing or invalid values are replaced with a generated
`req_<id>`; the invalid original is neither echoed nor logged.
The request id is passed through `UserContext` and added to turn metadata so trace/prompt metadata
can be correlated with API app logs.

API app logs record summary fields only: method, path, status code, duration, user id, request id,
and error code. They do not record request bodies, Authorization headers, cookies, prompts, model
messages, or raw provider responses. App Log, Trace Log, and Prompt Log remain separate artifacts.

## Deferred

- Web UI
- OAuth
- refresh tokens
- production Web Profile activation
- SSE offset/`Last-Event-ID` resume
- PostgreSQL / Alembic
