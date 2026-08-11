# Web Security, Quota, And E2E Gate v0.1 Design

Status: Phase 3 Lite code complete; Docker/Caddy environment smoke remains open.

Task: T-0146

Date: 2026-08-10

Owner: Web Security / Quota / E2E Worker

## Manager Review Revision

This revision addresses the 2026-08-10 Manager review:

- Quota and stream idempotency must share a narrow Persistence UoW; Service-layer sequential store
  calls are not acceptable for first-release consistency.
- Quota write access is available only through `StreamQuotaTurnPersistencePort`; policy calculation
  and read-only summaries are separate Ports.
- Crash recovery must persist enough owner/runtime, stream-record, quota-day, and charging-phase
  data to avoid timeout-only release decisions.
- Token quota is an internal deterministic estimate only. The estimator algorithm and version must
  be frozen before any token/day limit is approved.
- Production Web v0.1 exposes only `POST /v1/chat/stream` to the browser. Synchronous `/v1/chat`
  remains for CLI/dev/API-only compatibility and is deferred from production Web quota.
- Static cache and CSP must account for current image assets and React inline-style behavior without
  relaxing to a broad `unsafe-inline`.

## Scope And Boundary

T-0146 is the final Web MVP gate for production static hosting, persistent daily usage quota,
browser security headers, unified authentication-expiry behavior, and real browser validation
through Caddy/Compose.

This design preserves these boundaries:

- Do not modify `DialogueController`, Character, Memory, RAG, prompt strategy, state strategy, or
  model-provider strategy.
- Do not read or modify real `.env`, production secrets, or `data/aemeath.db`.
- Do not modify the captured `frontend/` reference directory or current role images.
- Do not change `docs/task_log.md` status from this Worker.
- Any Persistence schema change requires Manager approval before implementation.
- D6/D7/D8 remain separate Deployment gates; T-0146 supplies required inputs and evidence, not final
  cloud acceptance.

## Current Risks

1. Production is still D4 API-only. `Settings._validate_production()` currently requires
   `api_auth_mode == "dev_bearer"` and `AEMEATH_DEV_AUTH_TOKEN_FILE`, while the Web MVP production
   contract requires `web_session`, same-origin Cookie auth, and a Web CSRF secret.
2. `deploy/Caddyfile` currently reverse-proxies every route to FastAPI. It does not serve
   `web/dist/`, does not provide SPA fallback, and does not split API routes from static routes.
3. The Caddy image build context is `./deploy` and `deploy/Caddy.Dockerfile.dockerignore` excludes
   everything, so the current proxy image cannot copy Vite output.
4. There is no persistent per-account daily turn/token quota. Existing pre-auth and chat limiters are
   process-local rate controls only.
5. Streaming idempotency is durable, but the current implementation commits the user message and
   `chat_request_idempotency` `in_progress` row before any quota decision. T-0146 must replace this
   with one Persistence UoW for new stream reservations.
6. The frontend restores auth state on startup, but there is no global interceptor/state transition
   that redirects all later `401`/auth-expiry/revocation/disabled-account responses to `/login`.
7. Security headers are not yet defined at the production ingress. The Web MVP currently relies on
   app behavior and same-origin design, not a reviewed CSP/frame/referrer/header contract.
8. Authenticated browser E2E through real Caddy/Compose is absent; frontend tests are local jsdom
   and backend Caddy tests do not exercise the full Web auth/session/streaming path.
9. Admin usage summaries do not exist, so operators cannot inspect quota state without ad hoc SQL.
10. D7 observability inputs for quota/auth expiry/stream terminal outcomes are incomplete.

## Modules And Port

### New Quota Module

Add a narrow quota module rather than moving policy into the Controller:

```text
app/quota/
  schemas.py
  ports.py
  service.py
  admin.py
```

Split quota responsibilities so callers cannot bypass the stream+quota UoW:

```python
class UsageQuotaPolicyPort(Protocol):
    def plan_stream_reservation(
        self,
        *,
        request: UsageQuotaPlanRequest,
    ) -> UsageQuotaPlan: ...

    def estimate_prompt(
        self,
        *,
        request: UsagePromptEstimateRequest,
    ) -> UsagePromptEstimate: ...

    def estimate_output(
        self,
        *,
        request: UsageOutputEstimateRequest,
    ) -> UsageOutputEstimate: ...


class UsageQuotaQueryPort(Protocol):
    def get_daily_usage(self, *, request: UsageQueryRequest) -> UsageDailySummary: ...
```

Policy/query DTO fields stay intentionally narrow:

- `UsageQuotaPlanRequest`: `user_id`, `quota_day`, `surface`, `character_id`,
  `turn_limit`, `token_limit`, `estimator_version`, Context Budget input ceiling, and configured
  max output.
- `UsagePromptEstimateRequest`: `estimator_version`, complete prepared prompt-message UTF-8 byte
  count, and configured max output.
- `UsageOutputEstimateRequest`: `estimator_version`, response UTF-8 byte count, `delta_count`, and
  configured max output.
- `UsageQueryRequest`: `user_id`, optional `quota_day` range, optional disabled/archived account
  inclusion flag.

Idempotency keys, stream record ids, turn ids, terminal status, and terminal reasons belong to
`StreamQuotaTurnPersistencePort` DTOs only.

`UsageQuotaPolicyPort` owns deterministic estimation and limit planning, but it does not write the
database. `UsageQuotaQueryPort` exposes read-only daily summaries for admin/operator use. API routes
and Caddy do not calculate quota.

`StreamQuotaTurnPersistencePort` below is the only v0.1 write path for quota and stream state.

### Stream Quota UoW

Add a narrow UoW dedicated to production Web streaming. It can live in `app/quota/ports.py` or
`app/db/stream_quota.py`; the important contract is that stream idempotency and quota mutate in one
database transaction.

```python
class StreamQuotaTurnPersistencePort(Protocol):
    def resolve_or_reserve_with_quota(
        self,
        *,
        request: StreamQuotaResolveOrReserve,
    ) -> StreamQuotaResolveOutcome: ...

    def complete_with_quota(
        self,
        *,
        request: StreamQuotaComplete,
    ) -> StreamQuotaTerminalRecord: ...

    def fail_with_quota(
        self,
        *,
        request: StreamQuotaFail,
    ) -> StreamQuotaTerminalRecord: ...

    def mark_provider_started_with_quota(
        self,
        *,
        request: StreamQuotaProviderStarted,
    ) -> StreamQuotaReservationRecord: ...
```

`AgentStreamService` must not call `StreamTurnPersistencePort.resolve_or_reserve(...)` and then call
any separate quota write method. The new UoW owns:

- validating the owned dialogue Session;
- resolving existing `(user_id, session_id, client_message_id)` stream records;
- inserting the user message;
- inserting the `chat_request_idempotency` row;
- reserving quota counters and the quota reservation row;
- returning replay/conflict/in-progress outcomes without a second quota debit.
- applying quota reserve, provider-started adjustment, settle, and release counter mutations.

Production Web streaming:

```text
POST /v1/chat/stream
  -> Auth/CSRF/provenance
  -> existing ApiRateLimiterPort
  -> AgentStreamService.stream_chat
       -> stream request fingerprint/key registry
       -> StreamQuotaTurnPersistencePort.resolve_or_reserve_with_quota
       -> mark_provider_started_with_quota immediately before Provider iteration
       -> Provider streaming
       -> complete_with_quota or fail_with_quota during first-wins terminalization
```

Atomic outcomes:

- New request with sufficient quota: user message, stream idempotency row, and quota reservation
  commit together.
- Quota insufficient: none of those three records are written; API returns JSON `429`.
- Replay, conflict, or in-progress: return the existing stream outcome; quota counters do not
  change.
- Stream complete/fail: assistant message/state/idempotency terminal and quota settle/release commit
  together.

Synchronous `/v1/chat` is out of production Web quota scope for v0.1 because it has no stable
`client_message_id`. Production Web must block it in two layers: Caddy rejects exact path
`POST /v1/chat`, and FastAPI also rejects `POST /v1/chat` when `environment == "prod"` and
`api_auth_mode == "web_session"`. CLI and explicit API-only/dev profiles keep the existing
synchronous path unchanged. Sync API quota/idempotency is deferred.

Session routes, Auth routes, and admin routes do not consume daily turn quota.

### Error Codes

Add one stable code:

- `USAGE_QUOTA_EXCEEDED` -> HTTP `429`, retryable true, public message such as
  `Daily usage limit reached. Please try again later.`

Keep existing `API_RATE_LIMITED` for short-window token buckets and `MODEL_RATE_LIMITED` for
provider-side rate limits. All three can return `429`, but logs and public codes must distinguish
the source.

## Quota State Machine

Reservation state is terminal and first-wins:

```text
none
  -> reserved
reserved
  -> settled
  -> released
settled/released
  -> same terminal record on idempotent replay
```

Definitions:

- `reserved`: daily counters hold `reserved_turns` and `reserved_tokens` for a unique request.
- `settled`: the request consumed quota. This is used for completed turns and failures after the
  Provider was or may have been invoked.
- `released`: no Provider call occurred and the reservation should not count against the user.

Recommended charging policy:

- Auth, CSRF, provenance, JSON validation, and existing short-window rate-limit failures happen
  before quota and do not reserve.
- Quota rejection happens inside `resolve_or_reserve_with_quota` before inserting the stream record
  or user message, then returns JSON `429`.
- Streaming replay of an existing completed/failed `client_message_id` does not reserve or settle
  again.
- Streaming conflict or live in-progress replay does not reserve again.
- Preparation/session failures before Provider start release the reservation.
- Completed turns settle one turn and deterministic estimated tokens.
- Provider-started failures, user stop after Provider start, client disconnect after Provider
  start, consumer-slow after Provider start, and runtime-closing after Provider start settle one turn
  plus estimated tokens. This prevents repeated late cancellations from becoming a free retry loop.
- Failed idempotency replay returns the existing failure and quota terminal state unchanged.
- Provider-before orphan recovery becomes `released` with
  `terminal_reason=runtime_orphaned_pre_provider`.
- Provider-after orphan recovery becomes `settled` with
  `terminal_reason=runtime_orphaned_after_provider`.

Provider-started is a persisted charging phase, not an inference from elapsed time. The service must
advance the reservation from `reserved_pre_provider` to `provider_started` in the same transaction
that marks the request chargeable. This happens after `turn_engine.prepare()` has produced complete
prompt messages and immediately before `model_client.open_stream()` is called. If a crash happens
after this phase transition, recovery settles rather than releases.

Token accounting is a quota estimate, not provider billing truth. The estimator is versioned and
deterministic:

```text
estimator_version = prompt_utf8_v0_1
context_budget_max_input_utf8_ceiling = context_budget_max_chars * 4
reserve_input_estimate = ceil(context_budget_max_input_utf8_ceiling / 3)
reserved_tokens = reserve_input_estimate + deepseek_max_output_tokens
prompt_input_estimate = ceil(prompt_messages_utf8_bytes / 3)
output_estimate = ceil(response_utf8_bytes / 3)
settled_tokens = min(reserved_tokens, prompt_input_estimate + output_estimate)
```

Reserve time occurs before Prompt construction, so v0.1 reserves against the approved Context Budget
maximum input size plus configured max output. It does not try to estimate only the user message.
This conservatively covers Character, system instructions, recent history, and any enabled
non-provider context sources.

After `turn_engine.prepare()` completes, `mark_provider_started_with_quota()` computes the full
prepared `prompt_messages` UTF-8 byte count without storing message bodies, adjusts the reservation
from the Context Budget reserve estimate to the actual prompt estimate, and persists
`prompt_messages_utf8_bytes`. This adjustment must commit before `model_client.open_stream()` is
called. If the adjustment would exceed the daily token limit, the stream fails before the Provider is
started and the reservation is released with a quota error.

Production Web v0.1 must keep paid Embedding consumers disabled. Otherwise a paid Provider could be
invoked during prompt preparation before the quota phase reaches `provider_started`. Long-term
Memory Worker, vector RAG, and paid embedding-backed Semantic consumers therefore remain outside the
production Web profile.

Settle time uses safe persisted/observed counts: `prompt_messages_utf8_bytes`,
`response_utf8_bytes`, `delta_count`, `deepseek_max_output_tokens`, and the recorded
`estimator_version`. DeepSeek streaming currently requests `include_usage=false`, so no provider
usage field is available or required. The estimator must not log or persist prompt, user message,
response body, provider chunks, or provider bodies. It may persist counts only.

Manager decisions already received:

- `quota_day` is UTC.
- Token quota is an internal deterministic estimate only.
- Provider-started failure, cancellation, and disconnect charge; pre-provider failures release.
- `50` turns/day is an acceptable invitation-test candidate.
- Token/day limit remains unapproved until estimator qualification evidence exists.

## Data Structures And Migration

Schema change is required. Implementation should add a new `usage_quota_v0_1` persistence migration
using the fields and constraints below.

### Tables

`usage_quota_daily`:

```text
user_id TEXT NOT NULL
quota_day TEXT NOT NULL              -- YYYY-MM-DD, recommended UTC
turn_limit INTEGER NOT NULL
token_limit INTEGER NOT NULL
estimator_version TEXT NOT NULL
reserved_turns INTEGER NOT NULL DEFAULT 0
settled_turns INTEGER NOT NULL DEFAULT 0
released_turns INTEGER NOT NULL DEFAULT 0
reserved_tokens INTEGER NOT NULL DEFAULT 0
settled_tokens INTEGER NOT NULL DEFAULT 0
released_tokens INTEGER NOT NULL DEFAULT 0
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
PRIMARY KEY (user_id, quota_day)
CHECK all counters >= 0
CHECK (reserved_turns + settled_turns <= turn_limit)
CHECK (reserved_tokens + settled_tokens <= token_limit)
```

Daily rows freeze `turn_limit`, `token_limit`, and `estimator_version` when the row is first
created. Later configuration changes apply only to the next UTC quota day. The same user/day cannot
mix estimator versions because the daily row has exactly one `estimator_version`; a mismatch between
the policy plan and existing daily row fails closed as configuration/schema drift.

`usage_quota_reservations`:

```text
id TEXT PRIMARY KEY
user_id TEXT NOT NULL
quota_day TEXT NOT NULL
surface TEXT NOT NULL CHECK (surface = 'stream_chat')
idempotency_key TEXT NOT NULL
session_id TEXT NOT NULL
character_id TEXT NOT NULL
stream_request_id TEXT NOT NULL
turn_id TEXT
owner_runtime_id TEXT NOT NULL
status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released'))
charging_phase TEXT NOT NULL CHECK (
  charging_phase IN ('reserved_pre_provider', 'provider_started', 'terminalized')
)
reserved_turns INTEGER NOT NULL
reserved_tokens INTEGER NOT NULL
settled_turns INTEGER NOT NULL DEFAULT 0
settled_tokens INTEGER NOT NULL DEFAULT 0
estimator_version TEXT NOT NULL
context_budget_max_input_utf8_ceiling INTEGER NOT NULL
reserved_input_tokens INTEGER NOT NULL
reserved_output_tokens INTEGER NOT NULL
prompt_messages_utf8_bytes INTEGER NOT NULL DEFAULT 0
prompt_input_tokens INTEGER NOT NULL DEFAULT 0
output_utf8_bytes INTEGER NOT NULL DEFAULT 0
output_tokens INTEGER NOT NULL DEFAULT 0
delta_count INTEGER NOT NULL DEFAULT 0
terminal_error_code TEXT
terminal_reason TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
provider_started_at TEXT
settled_at TEXT
released_at TEXT
UNIQUE (user_id, quota_day, idempotency_key)
UNIQUE (stream_request_id)
FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE RESTRICT
FOREIGN KEY (stream_request_id) REFERENCES chat_request_idempotency(id) ON DELETE RESTRICT
```

Indexes:

- `idx_usage_quota_reservations_user_day_status`
- `idx_usage_quota_reservations_turn`
- `idx_usage_quota_reservations_runtime_phase`
- `idx_usage_quota_reservations_stale_reserved`

The `quota_day` is fixed at reserve time and never recomputed during settle/release. A request that
crosses UTC midnight settles against its original `quota_day`.

Counter mutation rules:

- reserve increments `usage_quota_daily.reserved_turns` and `reserved_tokens`;
- settle first decrements reserved counters by the reservation's reserved amounts, then increments
  settled counters by the settled amounts;
- release first decrements reserved counters by the reservation's reserved amounts, then increments
  released counters by those reserved amounts;
- terminal operations are idempotent and never decrement below zero;
- terminal reservation rows set `charging_phase='terminalized'`.

### Atomic UoW Details

New stream request with quota:

```text
BEGIN IMMEDIATE
  require owned active Session
  resolve existing chat_request_idempotency by user/session/client_message_id
  if existing terminal/live/conflict -> return outcome, no quota mutation
  compute UTC quota_day and Context Budget reserve counts
  upsert/select usage_quota_daily for user/day
  reject if reserved + settled counters plus requested reservation exceed limits
  insert user message
  insert chat_request_idempotency in_progress
  insert usage_quota_reservations reserved_pre_provider
  increment usage_quota_daily reserved counters
COMMIT
```

Terminal stream UoW:

```text
BEGIN IMMEDIATE
  require matching stream record by record_id/turn_id/user/session/owner_runtime_id
  require matching quota reservation by stream_request_id and owner_runtime_id
  if stream already terminal -> return current terminal, no quota mutation
  if completion -> insert assistant message, update relationship, mark stream completed
  if failure before provider_started -> mark stream failed and release reservation
       with reason such as quota_rejected_after_prepare or runtime_orphaned_pre_provider
  if failure after provider_started -> mark stream failed and settle reservation
       with reason such as model_failed or runtime_orphaned_after_provider
  decrement reserved counters before incrementing settled/released counters
  update usage_quota_daily counters in the same transaction
COMMIT
```

Provider-started UoW:

```text
BEGIN IMMEDIATE
  require matching stream record is in_progress and owner_runtime_id matches
  require matching quota reservation is reserved_pre_provider and owner_runtime_id matches
  require complete prepared prompt_messages UTF-8 byte count
  recompute prompt estimate using reservation estimator_version
  reject and release if adjusted reservation would exceed daily token limit
  adjust daily reserved_tokens from Context Budget reserve to prompt estimate + max output
  persist prompt_input_tokens and refreshed reserved_tokens
  set charging_phase = 'provider_started'
  set provider_started_at = now
  persist prompt_messages_utf8_bytes
  update owner/runtime-safe app-log counts after commit only
COMMIT
```

The complete/fail methods must be first-wins and CAS-equivalent to the current stream persistence:
a late cancel cannot rewrite a durable completion, and quota follows the durable stream terminal.

### Crash Recovery

Recovery must not release reservations solely because `updated_at` is old.

Persisted facts:

- `owner_runtime_id` on both stream and quota reservation;
- `stream_request_id` linking the reservation to `chat_request_idempotency`;
- `quota_day` frozen at reserve time;
- `charging_phase`;
- `provider_started_at` when phase becomes `provider_started`;
- safe count fields and `estimator_version`.

Startup recovery for a new `owner_runtime_id`:

- find `reserved` quota reservations whose linked stream record is still `in_progress` and whose
  `owner_runtime_id` differs from the new runtime;
- if `charging_phase == 'reserved_pre_provider'`, fail the stream as `CHAT_STREAM_INTERRUPTED` /
  `runtime_orphaned_pre_provider` and release quota in one transaction;
- if `charging_phase == 'provider_started'`, fail the stream as `CHAT_STREAM_INTERRUPTED` /
  `runtime_orphaned_after_provider` and settle quota in one transaction using reserved or observed
  counts;
- if the stream record is already terminal, reconcile quota to the same terminal status;
- never inspect prompt/response/provider body while recovering.

Long active requests owned by the current runtime are not released by time alone. v0.1 remains
single-worker; cross-process fencing is the persisted `owner_runtime_id` plus current startup
orphan recovery, not distributed liveness.

### Raw SQLite / SQLAlchemy Parity

Implement parallel behavior in both storage backends:

- raw SQLite methods on `SQLiteStore`;
- SQLAlchemy model plus repository, exposed through `SqlAlchemyStore`;
- shared DTOs in `app/quota/schemas.py`;
- store contract tests covering raw SQLite and SQLAlchemy-on-SQLite;
- schema validators that fail closed on missing marker, column drift, invalid checks, broken
  indexes, or foreign-key violations.

Migration rules:

- Incremental, idempotent, and no changes to historical Sessions, Messages, Auth, Memory, or
  relationship state.
- Do not let `schema.sql`, ORM `create_all`, or a later startup recreate a marked-but-invalid quota
  schema.
- No migration touches real `data/aemeath.db` during tests; tests use temp DBs.
- D6 backup/restore treats quota tables as ordinary primary DB state after migration approval.

## Caddy, Docker, And SPA Contract

### Caddy Route Contract

Production Caddy should become a same-origin static/API splitter:

```text
/v1/*   -> reverse_proxy api:8000, 128 KiB request body limit, no static fallback
/v1/chat exact POST -> reject before proxy in production Web profile
/health -> reverse_proxy api:8000, no-store
/ready  -> reverse_proxy api:8000, no-store
/assets/* hashed Vite output -> file_server from /srv/aemeath-web, immutable cache
all other GET/HEAD paths -> try_files {path} /index.html for SPA routes
other non-API non-static methods -> 404/405, not SPA fallback
```

Cache policy:

- hashed assets: `Cache-Control: public, max-age=31536000, immutable`;
- non-hashed static files, if any remain: `Cache-Control: no-cache`;
- `index.html`: `Cache-Control: no-cache`;
- auth/API/SSE/health/ready: preserve or set `Cache-Control: no-store`;
- no source maps in final image unless Manager explicitly approves them for staging only.

The current `web/public/assets/bg.jpg` and `web/public/assets/denia.jpg` are not content-hashed when
served directly from `public/`. Before applying one-year immutable cache, move character images into
the Vite module graph through imports from `web/src/config/character.ts` or equivalent so the
production filenames are hashed. Do not apply immutable cache to unhashed image URLs.

Security response headers, applied at Caddy for static and proxied responses:

```text
Content-Security-Policy:
  default-src 'self';
  script-src 'self';
  style-src 'self';
  img-src 'self' data:;
  font-src 'self';
  connect-src 'self';
  object-src 'none';
  base-uri 'none';
  form-action 'self';
  frame-ancestors 'none'
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
Strict-Transport-Security: max-age=15552000; includeSubDomains
```

The current frontend must remove React inline `style` usage such as CSS-variable background
assignment before enforcing `style-src 'self'`. The preferred fix is to express these values through
hashed Vite assets and CSS classes/custom properties set in stylesheets. Do not broaden the policy
to global `style-src 'unsafe-inline'`.

HSTS is enabled only in D8 HTTPS-domain environments after confirming all covered subdomains are
controlled. `includeSubDomains` is not enabled for local Compose or unreviewed staging domains.

### Docker Contract

The current proxy image cannot copy `web/dist/`. Recommended change after approval:

- Change proxy build context from `./deploy` to repository root.
- Convert `deploy/Caddy.Dockerfile` to a multi-stage build:
  - pinned `node` build stage;
  - `npm ci` under `web/`;
  - `npm run build`;
  - copy only `web/dist/` into `/srv/aemeath-web`;
  - final Caddy stage remains non-root `10002:10002`, read-only root, no Node/npm/package cache.
- Replace `deploy/Caddy.Dockerfile.dockerignore` with a least-privilege allowlist for `web/`,
  `deploy/Caddyfile`, and lockfiles.

Production API profile changes after Manager approval:

- `AEMEATH_API_AUTH_MODE=web_session`;
- exact `AEMEATH_WEB_PUBLIC_ORIGIN=https://<domain>`;
- `AEMEATH_WEB_AUTH_CSRF_KEY_FILE=/run/secrets/web_auth_csrf_key`;
- remove resident production dev Bearer auth and `AEMEATH_DEV_AUTH_TOKEN_FILE` from the Web
  production profile. Development and API-only profiles may retain dev Bearer.
- reject browser access to synchronous `POST /v1/chat` in production Web mode at both Caddy and
  FastAPI; the Web app uses only `POST /v1/chat/stream`.

## Frontend Authentication-Expiry Strategy

Add a single auth invalidation path in `web/src/auth/AuthContext.tsx` and `web/src/api/client.ts`.

Rules:

- Treat `401` with `AUTH_REQUIRED`, `AUTH_INVALID_CREDENTIALS`, `AUTH_SESSION_EXPIRED`, or
  `AUTH_SESSION_REVOKED` as browser session invalid.
- Treat `403 AUTH_ACCOUNT_DISABLED` as browser session invalid plus a safe disabled-account message.
- Clear in-memory CSRF state and account state exactly once.
- Abort any active stream before redirecting.
- Redirect protected routes to `/login` with `replace: true`.
- Do not redirect invite/reset pages while they are submitting token credentials; show their public
  error instead.
- Do not store session state, CSRF tokens, invite/reset tokens, prompts, responses, or quota data in
  `localStorage` or URLs.

The API client should expose an optional `onAuthInvalid` callback or event emitter rather than
letting individual pages duplicate `401` handling.

## Security And E2E Test Matrix

### Unit / Contract Tests

- stream+quota UoW atomicity: success writes stream record, user message, and quota reservation
  together.
- quota rejection writes no stream record, no user message, and no quota reservation.
- replay/conflict/in-progress outcomes do not mutate quota counters.
- complete/fail terminal UoW updates stream terminal and quota terminal in the same transaction.
- current-runtime live `in_progress` records are not released by timeout.
- startup orphan recovery releases `reserved_pre_provider` records and settles `provider_started`
  records.
- cross-UTC-midnight requests settle against the original reserve-time `quota_day`.
- `UsageQuotaPolicyPort` plan determinism and `UsageQuotaQueryPort` read-only daily summaries.
- quota rejection returns `USAGE_QUOTA_EXCEEDED`, HTTP `429`, and integer `Retry-After`.
- daily rollover uses the approved day boundary.
- estimator `prompt_utf8_v0_1` qualification with ASCII, Chinese, emoji, full prompt-message byte
  counts, Context Budget reserve ceilings, and max output-token configurations.
- raw SQLite and SQLAlchemy quota parity.
- migration validates columns, checks, indexes, markers, and foreign keys.
- orphan recovery transitions stale reservations according to owner and charging phase, never by
  timeout alone.
- logs contain counts/status/reason only, not credentials, prompts, responses, raw IP, or provider
  bodies.
- Caddy contract tests for API/static route isolation, SPA fallback, cache headers, CSP, referrer,
  `nosniff`, frame denial, HSTS domain gating, SSE pass-through, and 128 KiB body limit.
- frontend tests for global auth invalidation and no token persistence.

### Playwright Through Real Caddy/Compose

Run against an isolated staging/test database and generated test secrets, never real `.env` or
`data/aemeath.db`.

Desktop Chromium:

- accept invitation from fragment and verify fragment is removed;
- login and restore `/v1/auth/me`;
- create Session, rename, end, resume, archive;
- stream chat with ordered deltas and exactly one terminal event;
- stop a stream and verify safe failed/cancelled UI plus backend resource release;
- provider failure path shows public error and keeps history coherent;
- exhaust quota and verify `429`, `Retry-After`, disabled composer/retry timing;
- direct browser `POST /v1/chat` is unavailable/rejected in production Web profile;
- logout clears account state and returns to login.

Mobile viewport:

- login, session drawer, send/stop stream, quota exhausted, logout;
- no overlapping controls or clipped text.

Security browser checks:

- cross-site Origin/Referer/Fetch-Metadata state-changing requests rejected;
- Bearer plus Cookie conflict rejected;
- revoked/expired session and disabled account redirect to login;
- XSS payloads render as text;
- no credentials/CSRF/invite/reset tokens in `localStorage`, `sessionStorage`, or URL after
  acceptance;
- `/v1/*`, `/health`, and `/ready` are never served by SPA fallback;
- static cache headers are correct.

## Phased Implementation Order

1. Manager approval of schema/config/Caddy change requests.
2. Add quota DTOs, `UsageQuotaPolicyPort`, `UsageQuotaQueryPort`, error code, configuration bounds,
   and tests.
3. Add stream+quota UoW DTOs and raw SQLite migration/store implementation.
4. Add SQLAlchemy model/repository parity and store contract tests.
5. Integrate the UoW into streaming only, including provider-started phase and recovery behavior.
6. Add admin usage query command and safe JSON output.
7. Add frontend global auth invalidation and remove inline-style paths that conflict with CSP.
8. Convert character images to hashed Vite assets.
9. Convert Caddy/Docker static hosting and update deployment docs/tests.
10. Add Playwright E2E harness through real Caddy/Compose with isolated temp state.
11. Run focused quota/auth/stream/Caddy/frontend/E2E validation, then full regression.
12. Submit delivery report with evidence; Manager owns task acceptance/status.

## Expected Modified Files

Likely Python/runtime files:

- `app/quota/__init__.py`
- `app/quota/schemas.py`
- `app/quota/ports.py`
- `app/quota/service.py`
- `app/quota/admin.py`
- `app/errors/codes.py`
- `app/errors/http.py`
- `app/config.py`
- `app/main.py`
- `app/api/dependencies.py`
- `app/api/routes/chat.py` only to reject production Web sync chat if implemented at API boundary
- `app/api/routes/chat_stream.py`
- `app/services/agent_service.py` should remain unchanged for sync quota unless the rejection
  boundary requires a small wiring adjustment
- `app/services/agent_stream_service.py`
- `app/db/stream_quota.py` or equivalent narrow UoW module
- `app/db/schema.sql`
- `app/db/models.py`
- `app/db/session.py`
- `app/db/sqlite_store.py`
- `app/db/sqlalchemy_store.py`
- `app/db/repositories/usage_quota.py`
- `app/db/store_factory.py`

Likely Web/deployment files:

- `web/src/api/client.ts`
- `web/src/api/stream.ts`
- `web/src/auth/AuthContext.tsx`
- `web/src/pages/ChatPage.tsx`
- `web/package.json`
- `web/package-lock.json`
- `deploy/Caddyfile`
- `deploy/Caddy.Dockerfile`
- `deploy/Caddy.Dockerfile.dockerignore`
- `compose.yaml`
- `deploy/prod.env.example`
- `Dockerfile` only if API image needs web profile metadata or new secret path checks

Likely tests/docs:

- `tests/test_usage_quota.py`
- `tests/store_contract/test_usage_quota_contract.py`
- `tests/test_api_quota.py`
- `tests/test_api_streaming.py`
- `tests/test_browser_auth.py`
- `tests/deployment/test_ingress_contract.py`
- `tests/deployment/test_compose_contract.py`
- `tests/deployment/test_production_config.py`
- `tests/e2e/` Playwright specs and harness
- `web/src/**.test.tsx`
- `docs/deployment.md`
- `docs/frontend.md`
- `docs/interfaces.md`
- `docs/web_mvp_contract.md`
- `docs/observability.md`
- delivery report under `docs/agents_communication/`

Do not modify `docs/task_log.md` status from the Worker.

## D6, D7, D8 Boundary And Required Evidence

D6 boundary:

- T-0146 owns the approved quota schema addition and must keep it compatible with the D6
  backup/restore contract.
- D6 backup/restore remains the owner of backup implementation and rollback rules.
- After quota implementation, D6 evidence must show Auth, stream idempotency, and quota tables are
  included in SQLite backup/verify/restore using the whole DB/WAL/SHM safety contract.

D7 boundary:

- T-0146 supplies safe event names and fields for quota/auth/stream terminal behavior.
- D7 owns retention, monitoring, alerting, dashboards, and production log handling.
- Required event families: `quota.reserved`, `quota.settled`, `quota.released`,
  `quota.rejected`, `auth.session_invalidated`, `api.auth_redirect_required`,
  `agent_stream.completed`, `agent_stream.failed`.

D8 boundary:

- T-0146 E2E can validate local/staging Compose and Caddy behavior.
- D8 owns final ECS/cloud acceptance: HTTPS certs, domain, reboot persistence, image replacement,
  backup/restore drill, real provider smoke, cookie/CSRF over the public origin, quota exhaustion,
  and mobile browser smoke.
- Final D8 evidence should include non-sensitive identifiers, image digests, config hashes,
  Caddy route/header checks, Playwright report summary, and sanitized app-log excerpts only.

## Deferred

- Public registration, email delivery, OAuth, passkeys, billing, payment, or subscription logic.
- Redis/PostgreSQL/distributed quota or multi-node rate limiting.
- Memory Worker, Audio/TTS, vector RAG activation, tools/MCP UI, or model strategy changes.
- Exact provider billing reconciliation unless a later Provider usage contract is approved.
- Browser admin panel for usage; v0.1 uses CLI/operator JSON only.
- Rich Markdown/HTML rendering.
- Permanent replacement of temporary character visuals.

## Manager Decisions Applied

- The revised narrow Port split is the implementation contract: policy/query are read-only or
  calculation-only, and `StreamQuotaTurnPersistencePort` is the only quota write path.
- The quota schema direction and v0.1 fields are approved for implementation after these narrow
  revisions: `owner_runtime_id`, `stream_request_id TEXT NOT NULL`, `charging_phase`,
  `estimator_version`, and safe count fields.
- Daily constraints are `reserved_turns + settled_turns <= turn_limit` and
  `reserved_tokens + settled_tokens <= token_limit`.
- Daily `turn_limit`, `token_limit`, and `estimator_version` freeze on first row creation; config
  changes apply from the next UTC quota day.
- No independent quota `expired` state; orphan recovery uses `released` or `settled` terminal
  states with explicit runtime-orphan reasons.
- `quota_day` is UTC.
- Token quota is an internal deterministic estimate and does not claim provider billing parity.
- `prompt_utf8_v0_1` is the initial deterministic estimator for implementation and qualification.
- Production Web v0.1 disables paid Embedding consumers so Prompt preparation cannot be the first
  paid Provider call.
- Provider-started failures, cancellation, and disconnects charge; pre-provider failures release.
- Production Web profile switches to `web_session` and uses `_FILE` CSRF secret.
- Production Web profile removes dev Bearer entirely; development/API-only profiles may retain it.
- Production Web rejects synchronous `POST /v1/chat` at both Caddy and FastAPI.
- Caddy build context and multi-stage static build are approved in principle.
- HSTS is only for D8 HTTPS-domain environments; `includeSubDomains` requires subdomain-control
  confirmation.
- Local Compose E2E uses Mock Provider; real Provider smoke is reserved for D8.
- `50` turns/day is an invitation-test candidate.
- After this narrow document revision, implementation may proceed without a third full design
  review.

## Future Manager Decisions Requested

1. Set the initial token/day limit after estimator qualification evidence. This design no longer
   proposes any token/day default.
2. Confirm D8 HTTPS domain and subdomain inventory before enabling HSTS `includeSubDomains`.
3. Confirm final ECS staging evidence expectations after implementation validation begins.
