# Aemeath Agent Invitation-only Web MVP Contract v0.1

Date: 2026-08-04

Status: **Manager contract frozen; implementation remains task-gated**

Owner: Manager Session

This document defines the product, security, HTTP, streaming, persistence, frontend, and deployment
boundaries for the first browser-based Aemeath Agent release. It does not authorize a Worker to
change runtime code before the corresponding task design is reviewed.

## 1. Release Goal

The v0.1 Web MVP is a small invitation-only closed test in which an invited user can:

1. open the Aemeath website in a modern desktop or mobile browser;
2. accept a one-time invitation and choose a private password;
3. log in and log out without handling an API token;
4. create, list, resume, end, and archive Aemeath dialogue sessions;
5. load message history with bounded pagination;
6. send a text message and see Aemeath's response stream progressively;
7. recover cleanly from validation, rate-limit, provider, disconnect, and session-state errors.

The first release is single-character, text-only, same-origin, single-node, and CPU-only. The active
Character, World Info, Memory, Context Budget, model, and state behavior remains unchanged unless a
separate task explicitly changes it.

## 2. Explicit Non-goals

The following are not part of this Web MVP:

- public self-registration;
- email delivery, email verification, or self-service password recovery;
- OAuth, social login, passkeys, refresh-token/JWT flows, or third-party identity providers;
- a browser admin panel, billing, subscriptions, or payments;
- multi-character selection;
- Audio/TTS, the Memory Worker, or managed vector RAG activation;
- rich model-authored HTML, arbitrary Markdown plugins, file upload, image upload, or tools/MCP UI;
- WebSocket infrastructure, multi-node deployment, Kubernetes, PostgreSQL, or Redis;
- exposing model reasoning, prompt text, Memory content, Evidence content, vectors, or provider bodies.

An operator-assisted password-reset token is allowed because it preserves an invited user's account
and dialogue history without adding an email system.

## 3. Frozen Product And Topology Decisions

```text
Browser
  -> Caddy on one HTTPS origin
       -> immutable hashed frontend assets
       -> SPA navigation fallback
       -> /v1/*, /health, /ready reverse proxy
  -> FastAPI HTTP adapter
       -> Browser Auth adapter -> UserContext
       -> AgentService / future AgentStreamService
       -> SessionService
  -> existing Controller and domain ports
```

Decisions:

- Frontend: React + TypeScript + Vite static application under `web/`.
- Production serving: Caddy serves the built static assets and reverse-proxies API paths. No Node.js
  process exists in the production runtime image.
- Origin: frontend and API use the same scheme, host, and port. Production CORS remains empty.
- Authentication: production Web uses an opaque server-side browser session. The browser never
  receives the shared dev Bearer token.
- Transport: HTTPS only. The production cookie is host-only, `Secure`, `HttpOnly`, `SameSite=Strict`,
  and `Path=/`.
- Streaming: `POST /v1/chat/stream` returns a bounded domain event stream over
  `text/event-stream`. The browser uses `fetch()` streaming; v0.1 does not require `EventSource` or
  WebSocket.
- Rendering: model output is plain text with preserved line breaks. Rich HTML/Markdown is deferred.
- Runtime: one Uvicorn worker. Existing process-local admission, rate-limit, and provider gates
  remain valid only under that boundary.

## 4. Identity And Session Boundaries

### 4.1 Three Different Identities

The following must never be conflated:

| Identity | Purpose | Owner |
|---|---|---|
| `account_id` / `user_id` | stable user identity and ownership | Account module |
| Web Auth Session | temporary authenticated browser login | Auth module |
| Dialogue `session_id` | Aemeath conversation lifecycle and history | Session module |

A Web Auth Session may access many dialogue sessions belonging to the same account. Logging out or
revoking a Web Auth Session does not archive or delete dialogue sessions.

### 4.2 Invitation Flow

1. An operator runs a non-interactive-safe admin CLI to create an invitation bound to an approved
   ASCII username, display name, and role list. v0.1 roles are exactly `["user"]`.
2. The CLI creates at least 256 bits of cryptographically secure random token material, stores only
   its SHA-256 digest, and displays the invitation URL exactly once.
3. The URL carries the token in the SPA fragment, not a server-visible query string. The frontend
   removes the fragment with `history.replaceState()` before making other requests.
4. The user submits the token and a new password to `POST /v1/auth/invitations/accept`.
5. One transaction validates expiry/revocation/use, creates the Account and credential, consumes the
   invitation, creates a Web Auth Session, and sets the production cookie.
6. Reuse, expiry, revocation, username collision, or partial failure cannot create a second account
   or leave a reusable token.

Default invitation lifetime is 24 hours. Production configuration may shorten it or extend it to a
maximum of seven days. Raw invitation tokens must not enter logs, traces, exceptions, database
columns, shell history, URLs received by Caddy, or task reports.

### 4.3 Username And Password Rules

- Username is immutable in v0.1 and matches `^[a-z0-9][a-z0-9_.-]{2,31}$`.
- `accounts.username_normalized` is the lowercase username and has a unique constraint.
- Display name may contain Unicode and is not an authentication identifier.
- Passwords are treated as exact user input: no trimming, case conversion, or silent truncation.
- Password length is 12 to 128 Unicode code points and no more than 1024 UTF-8 bytes.
- All printable characters and spaces are allowed; v0.1 does not impose composition rules.
- Passwords use Argon2id through a maintained library. Parameters are versioned in the encoded hash
  and meet or exceed the current OWASP minimum at implementation time.
- A login may rehash a valid password when the approved work factor changes.

No plaintext password, password-derived diagnostic, or password hash may enter App Log, Trace Log,
Prompt Log, API responses, or delivery reports.

### 4.4 Browser Session Rules

- Cookie name: `__Host-aemeath_session` in production.
- Cookie attributes: `Secure; HttpOnly; SameSite=Strict; Path=/`; no `Domain` attribute.
- Session token: at least 256 random bits; only a digest is persisted.
- Default expiry: seven-day idle timeout and thirty-day absolute timeout, both server-enforced and
  configurable only within reviewed bounds.
- `last_seen_at` is write-coalesced, with a default five-minute update interval.
- Login always creates a fresh token. Logout, account disable, password reset, and password change
  revoke the required browser sessions atomically.
- Unknown, expired, revoked, malformed, or disabled-account sessions do not produce `UserContext`.
- A request containing both production Web credentials and a dev Bearer credential fails closed;
  it never chooses one implicitly.

Production Web mode is selected explicitly. Dev Bearer Auth remains a compatibility adapter for CLI,
tests, and explicit local development, but is not accepted by the production Web origin.

### 4.5 CSRF Contract

Cookie authentication introduces ambient credentials, so every state-changing Web request must pass
all applicable checks:

- exact configured `Origin` validation, with a strict `Referer` fallback where specified;
- `Sec-Fetch-Site` rejection for cross-site state-changing requests when the header is present;
- a per-Web-Session synchronizer token sent as `X-CSRF-Token`;
- JSON content type for JSON APIs; no state change on `GET` or `HEAD`.

`GET /v1/auth/csrf` returns the existing session's synchronizer token to same-origin JavaScript. The
frontend keeps it in memory, not in `localStorage`, the URL, or logs. The server stores only the
required verifier/digest. Login and invitation acceptance have no ambient authenticated session but
still require strict origin/content-type validation and pre-auth rate limiting.

## 5. Persistence Contract

The exact SQL is owned by the Auth Worker, but the semantic records are frozen:

| Record | Required meaning |
|---|---|
| `accounts.username_normalized` | unique immutable login key |
| `account_credentials` | account id, encoded password hash, change/version timestamps |
| `account_invitations` | digest, pre-bound identity/roles, expiry, consumed/revoked state |
| `account_password_resets` | account-bound one-time digest, expiry, consumed/revoked state |
| `web_auth_sessions` | account id, token digest, CSRF verifier, idle/absolute expiry, revocation |
| `chat_request_idempotency` | account/session/client-message identity and terminal turn outcome |

Requirements:

- raw SQLite and SQLAlchemy-on-SQLite expose equivalent repository/Facade behavior and contract tests;
- migration is incremental and idempotent; it never drops historical accounts, Sessions, Messages,
  Memory, or relationship state;
- username backfill fails loudly on normalized collisions instead of silently renaming accounts;
- token consumption, Account creation, credential creation, and Web Session creation use one atomic
  transaction;
- raw tokens and passwords are never persisted;
- D6 backup/restore treats these tables as ordinary primary database state and records the schema
  contract/version in its manifest.

## 6. HTTP API Contract

All success/error payloads reuse the existing request-id and `ErrorInfo` conventions. No endpoint
returns internal exception text.

### 6.1 Auth Endpoints

| Method | Path | Auth | CSRF | Purpose |
|---|---|---|---|---|
| `POST` | `/v1/auth/invitations/accept` | invite token | origin only | create invited Account and login |
| `POST` | `/v1/auth/login` | username/password | origin only | create Web Auth Session |
| `POST` | `/v1/auth/logout` | Web Session | required | revoke current Web Session |
| `GET` | `/v1/auth/me` | Web Session | no | return safe account summary |
| `GET` | `/v1/auth/csrf` | Web Session | no | return per-session CSRF token |
| `POST` | `/v1/auth/password/change` | Web Session | required | change password and revoke other sessions |
| `POST` | `/v1/auth/password-reset/accept` | reset token | origin only | operator-assisted reset and session revocation |

Login, invitation, and reset failures use generic public messages. Public responses do not reveal
whether a username, invitation digest, reset token, or disabled account exists. Internal reason codes
may appear only in content-safe App Log fields.

### 6.2 Dialogue Session Endpoints

Existing endpoints remain compatible:

```text
GET  /v1/sessions
POST /v1/sessions
GET  /v1/sessions/{session_id}/messages
POST /v1/chat
```

Web MVP adds:

```text
PATCH /v1/sessions/{session_id}
POST  /v1/sessions/{session_id}/end
POST  /v1/sessions/{session_id}/resume
POST  /v1/sessions/{session_id}/archive
POST  /v1/chat/stream
```

Message and Session listing use opaque cursor pagination with bounded limits. Offset pagination is
not part of the stable public contract. Every operation verifies owner and character before reading
or changing state. v0.1 does not expose hard deletion through the browser.

## 7. Streaming Contract

### 7.1 Stable Events

The response uses UTF-8 `text/event-stream`. Events carry JSON data and exactly one terminal event.

| Event | Required safe data |
|---|---|
| `turn.started` | request id, turn id, dialogue session id |
| `response.delta` | answer text delta only |
| `turn.completed` | turn id, final status, warnings, public state delta |
| `turn.failed` | stable public error code/message/retryable flag |
| comment heartbeat | no user/model content |

Provider `reasoning_content`, prompt sections, Memory/Evidence content, source IDs, vectors, token
material, and provider raw chunks are never sent to the browser. Delta text is never written to App
Log or Trace Log.

### 7.2 Lifecycle And Persistence

- Pre-stream auth, validation, ownership, Session status, quota, and admission failures return the
  normal JSON error envelope with the correct HTTP status.
- After response headers are sent, failure is represented by one `turn.failed` event followed by
  stream close; the HTTP status is not rewritten.
- The `(user_id, session_id)` admission lease and global LLM lease cover the complete stream.
- One validated `client_message_id` is required per send. The same account/session/id may never
  invoke the Provider twice.
- The user message and idempotency record are committed once before Provider execution. The assistant
  message is committed only after a complete successful answer is available.
- A failed/cancelled stream keeps the user message and a failed terminal idempotency state, but does
  not create a completed assistant message.
- Browser disconnect triggers best-effort provider cancellation and always releases leases/resources.
- Retrying an in-progress, completed, or failed `client_message_id` returns a deterministic outcome;
  the frontend must use a new id only for an intentional new attempt.
- The synchronous `POST /v1/chat` remains for compatibility. Web UI uses only the stream endpoint.

The streaming implementation must have a separately reviewed internal design. HTTP/SSE types may not
enter `DialogueController`, model providers, Character, Memory, RAG, Prompt, or State ports.

## 8. Frontend Contract

### 8.1 Required Screens And States

- invitation acceptance;
- login;
- chat workspace with Session list, message history, composer, send, and stop controls;
- Session title edit, end, resume, and archive actions;
- loading, empty, offline, rate-limited, session-ended, provider-unavailable, and retry states;
- explicit logout and expired-session redirect.

The first screen after authentication is the usable chat workspace, not a marketing landing page.
The layout must support desktop and mobile without overlapping controls or text. Keyboard, focus,
screen-reader labels, reduced motion, and color contrast are acceptance requirements.

### 8.2 Browser Security And State

- No session id, Bearer token, invitation token, password, CSRF token, prompt, or response body is
  stored in `localStorage` or analytics.
- Session cookie is inaccessible to JavaScript.
- CSRF token and in-flight message state live in memory and are recreated after reload.
- Model text is rendered as text, not assigned to `innerHTML`.
- No third-party CDN script, font, tracker, analytics, or remote image is allowed in v0.1.
- Browser history must not retain invitation/reset token fragments after consumption.

### 8.3 Static Delivery

- `vite build` produces hashed assets under a controlled build stage.
- The final Caddy image contains static output but no Node.js runtime, package cache, source map, or
  frontend secret.
- Hashed assets use long immutable cache headers; the SPA entry document uses revalidation/no-cache.
- API, auth, `/health`, and `/ready` responses are never served by SPA fallback or static cache.
- Caddy applies reviewed CSP, `X-Content-Type-Options`, frame-ancestor, referrer, and HTTPS headers.

## 9. Cost, Abuse, And Capacity Contract

Before D8 Web staging, the system must have:

- strict pre-auth limits for login, invite acceptance, and reset acceptance;
- existing authenticated user/IP rate limits on chat;
- the existing global dialogue/LLM/Embedding gates;
- a per-account daily turn/token budget behind a `UsageQuotaPort` or equivalent Service boundary;
- atomic reserve/settle behavior so failed streams do not create unlimited retries or overcharge;
- safe `429`/quota errors and `Retry-After` where applicable;
- operator commands to disable an Account, revoke all Web Sessions, revoke invitations, and inspect
  safe usage summaries.

No limiter or quota key stores raw IP, password, invitation/reset token, Cookie, prompt, or response.
The single-process limiter remains acceptable only while production uses one application worker.

## 10. Observability And Privacy

Required content-safe events include:

- invitation issued/accepted/revoked terminal status;
- login succeeded/failed, logout, session expired/revoked;
- stream started/completed/failed/cancelled;
- time to first answer delta, total duration, delta count, final response length, provider error code;
- quota reserved/settled/rejected;
- Session lifecycle transitions.

Events may include request, account, Web Session correlation digest, dialogue session, and turn IDs.
They must not include raw browser session IDs, CSRF/invite/reset tokens, passwords/hashes, raw IP,
user-agent body, prompt, message body, response delta/body, Memory/Evidence content, or provider body.
Production Prompt Log remains disabled.

For the invitation-only release, account export/deletion may be an operator runbook rather than a Web
screen, but it must use owner-scoped application/store interfaces, never manual ad hoc SQL.

## 11. Module And Ownership Boundaries

| Area | Expected ownership | Forbidden coupling |
|---|---|---|
| Browser Auth | `app/accounts/`, `app/auth/`, Auth repositories | no Controller/Character/Memory logic |
| HTTP Auth adapter | `app/api/dependencies.py`, auth routes | no raw Cookie/token below Auth boundary |
| Streaming Service | `app/services/` plus model/domain stream ports | no FastAPI/SSE types in Controller/domain |
| Session Web API | `SessionService`, API routes/DTOs | no direct route-to-SQL access |
| Frontend | `web/` | no embedded secret or direct Provider call |
| Persistence | raw/SQLAlchemy repositories and Facades | no Auth policy in Facade |
| Deployment | Caddy/Compose/build/runbook | no business-policy changes |

Stable boundary intent:

```python
class BrowserSessionAuthPort(Protocol):
    def resolve_session_token(
        self,
        *,
        token: str | None,
        request_id: str,
    ) -> UserContext: ...


class AgentStreamServicePort(Protocol):
    def stream_chat(
        self,
        *,
        user_context: UserContext,
        request: StreamChatRequest,
    ) -> Iterable[ChatStreamEvent]: ...
```

These signatures freeze responsibility, not final field-level implementation. Each Worker must submit
its DTO/error/storage design before code changes.

## 12. Implementation Sequence

1. **T-0142 Browser Auth And Invitations v0.1**
   Freeze schemas/ports/errors, then implement invitation, credentials, Web Sessions, CSRF, admin CLI,
   raw/SQLAlchemy parity, and auth API.
2. **T-0143 Streaming Chat Runtime And API v0.1**
   Freeze domain events and Controller integration, then implement true DeepSeek-to-browser streaming,
   cancellation, idempotency, persistence, and safe observability.
3. **T-0144 Web Session API Completion v0.1**
   Add lifecycle commands, title update, cursor pagination, and ownership tests.
4. **T-0145 React/Vite Web Frontend v0.1**
   Implement against frozen mock contracts, then integrate Auth, Session, and streaming APIs.
5. **T-0146 Web Security, Quota, And E2E Gate v0.1**
   Add quota, headers, security fault tests, Playwright browser matrix, and deployment integration.
6. **T-0140 D6/D7/D8 Deployment continuation**
   D6 may proceed in parallel after the Auth schema contract is frozen. D7 observes final Web events;
   D8 is the final real-cloud Web acceptance gate.

Auth must precede real frontend integration so the UI is never built around a production Bearer token.
Streaming and Session API designs may proceed in parallel after Auth/HTTP credential boundaries are
frozen. Frontend visual implementation may use mocks after the external contracts are approved.

## 13. Acceptance Matrix

### Auth

- invite, replay, expiry, revoke, collision, transaction rollback, login, logout, idle/absolute expiry;
- Argon2 valid/invalid/rehash paths and bounded input cost;
- session fixation/rotation, account disable, password reset/change, revoke-all;
- CSRF token/origin/fetch-metadata matrix;
- generic public errors and zero credential leakage.

### Multi-user Isolation

- cross-user Session list/read/update/archive is denied;
- cross-user Memory/relationship behavior remains owner-scoped;
- invitation/reset/browser session tokens cannot resolve another Account;
- idempotency identities include account and dialogue session.

### Streaming

- one Provider call, ordered deltas, exactly one terminal event;
- no reasoning exposure, duplicate assistant message, or duplicate Provider call;
- disconnect/cancel/provider-timeout/client-slow/error-after-headers behavior;
- same-session serialization and different-session/global gate behavior;
- App/Trace/Prompt outputs contain no stream content.

### Frontend

- Playwright Chromium desktop and mobile viewport workflows;
- invitation, login, chat, stop, retry, Session lifecycle, expiry, logout;
- no overlap/overflow, stable controls, accessible keyboard/focus behavior;
- XSS payloads remain text; no credential in browser storage/history/logs;
- production static cache and SPA/API routing contract.

### Deployment

- full Python and frontend checks;
- reproducible linux/amd64 multi-stage build with no Node runtime in final image;
- non-root/read-only container and only approved writable mounts;
- D6 backup/restore includes Auth/Web state;
- D8 reboot, image replacement, HTTPS, Cookie, CSRF, streaming, quota, and provider smoke.

## 14. Required Worker Handoffs

Each task must submit a design before implementation and a delivery report after implementation. The
report must include modified files, interfaces/errors/schema, migration behavior, security tests,
full regression, skipped real-provider/browser/environment checks, and unresolved risks. Workers do
not mark their task done; Manager performs final acceptance and updates shared architecture/status.

## 15. References

- [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Vite production build](https://vite.dev/guide/build)
- [Vite static deployment](https://vite.dev/guide/static-deploy.html)
- [FastAPI StreamingResponse](https://fastapi.tiangolo.com/advanced/custom-response/)
- [DeepSeek Chat Completion streaming](https://api-docs.deepseek.com/api/create-chat-completion)
