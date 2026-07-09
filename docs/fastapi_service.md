# FastAPI Service v0.1

FastAPI is an HTTP adapter around the existing application service boundary:

```text
HTTP routes
  -> AuthService / LocalDefaultAccountResolver
  -> UserContext
  -> AgentService / SessionService
  -> existing controller and store ports
```

`DialogueController` does not handle HTTP, headers, cookies, tokens, account status, or API
logging.

## Runtime

`app.api.server.create_app(settings: Settings | None = None)` builds the runtime with
`build_agent_service(...)`, then constructs API dependencies around the same store and app logger.

Run locally with:

```bash
pip install -e .[dev,api]
uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000
```

## Auth

`GET /health` is public. `/v1/*` endpoints require `Authorization: Bearer <dev token>` by default.
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

Prompt logs contain full prompt/messages/sections/response snapshots and should be treated as
sensitive local debugging artifacts. Enable API prompt logs only for controlled debugging.

`AEMEATH_DEV_AUTH_TOKEN` is treated as the configured dev-token source of truth. When it is unset
on startup, the configured `AEMEATH_DEV_AUTH_USER_ID` has its stored `dev_token_hash` cleared.

API model fallback is disabled by default. Provider auth, rate-limit, timeout, unavailable, or
invalid-response failures should surface through the shared API error shape instead of returning a
mock fallback response. CLI runtime keeps model fallback enabled by default for local continuity.

## Endpoints

```text
GET  /health
POST /v1/chat
GET  /v1/sessions
POST /v1/sessions
GET  /v1/sessions/{session_id}/messages
```

`POST /v1/chat` calls `AgentService.handle_chat(...)`. It does not call `DialogueController`
directly. If the supplied `session_id` does not exist, v0.1 follows the existing
`SessionStorePort.ensure_session(...)` behavior and creates the session during the chat turn.

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

`X-Request-ID` is accepted and written back. If missing, the API generates `req_<id>`.
The request id is passed through `UserContext` and added to turn metadata so trace/prompt metadata
can be correlated with API app logs.

API app logs record summary fields only: method, path, status code, duration, user id, request id,
and error code. They do not record request bodies, Authorization headers, cookies, prompts, model
messages, or raw provider responses. App Log, Trace Log, and Prompt Log remain separate artifacts.

## Deferred

- Web UI
- token-by-token streaming
- `/v1/chat/stream`
- username/password login
- OAuth
- refresh tokens or cookie sessions
- PostgreSQL / Alembic
