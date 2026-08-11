# Frontend Web MVP v0.1 Delivery Report

## Status

Manager-required repairs complete, pending final Manager acceptance and Deployment integration
approval.

This delivery provides a locally runnable Web frontend against the existing FastAPI Browser Auth,
Session, and Streaming Chat contracts. It does not claim production deployment readiness yet.

## Objective

Build the first usable browser experience for Aemeath Agent:

```text
Invitation / Login
  -> Session list and history
  -> POST SSE streaming dialogue
  -> Session and account operations
```

The captured Dania page is used only as a visual reference. Its backend logic, injected browser
content, music system, and third-party APIs are not reused.

## Delivered Capabilities

### Browser Authentication

- Username/password login.
- Invitation acceptance through `/invite#token=...`.
- Password-reset acceptance through `/reset-password#token=...`.
- Current-account restoration through the HttpOnly browser Session Cookie.
- Logout and password change.
- In-memory CSRF token acquisition and attachment to mutating requests.
- Public backend errors displayed without exposing raw response bodies or credentials.

Invitation and reset tokens arrive in the URL fragment rather than the query string, so browsers
do not send them to the static frontend server. The page captures the token in component memory and
immediately removes the fragment with `history.replaceState`.

### Session Management

- Create and select Sessions.
- Read Session history.
- Update Session titles.
- End, resume, and archive Sessions.
- Display active, ended, and archived states.
- Disable Session switching while a streaming request owns the active dialogue flow.
- Responsive desktop sidebar and mobile drawer.

The frontend uses only `/v1/sessions*` routes. It does not access stores, repositories, or database
adapters.

### Streaming Dialogue

- `POST /v1/chat/stream` through `fetch()` and `ReadableStream`.
- SSE heartbeat comments ignored safely.
- Unicode-safe incremental decoding.
- `turn.started`, `response.delta`, `turn.completed`, and `turn.failed` handling.
- Unique `client_message_id` generated with `crypto.randomUUID()`.
- Optimistic user message and streaming assistant placeholder.
- Explicit stop action through `AbortController`.
- Canonical message history refreshed after durable completion.
- Failed-turn history may be refreshed without clearing the public terminal error.
- No client-side fabrication of a successful terminal event.

### Visual System

- Full-background authentication surface.
- Dense chat workspace with stable desktop and mobile dimensions.
- Character header, presence state, message bubbles, typing indicator, composer, account dialog,
  Session status labels, and safe error presentation.
- Reduced-motion support.
- Lucide icons for commands and tooltips for icon-only controls.

Temporary character visuals are isolated in `web/src/config/character.ts`:

- `web/public/assets/bg.jpg`: temporary Dania background, JPEG.
- `web/public/assets/denia.jpg`: temporary Dania avatar, JPEG.

Replacing the character visuals later does not require changes to Auth, Session, or Chat components.

## Main Files

### Project and Build

- `web/package.json`
- `web/package-lock.json`
- `web/vite.config.ts`
- `web/tsconfig.json`
- `web/tsconfig.app.json`
- `web/tsconfig.node.json`
- `web/index.html`

### Application

- `web/src/App.tsx`
- `web/src/main.tsx`
- `web/src/styles.css`
- `web/src/config/character.ts`

### API Boundary

- `web/src/api/types.ts`
- `web/src/api/client.ts`
- `web/src/api/stream.ts`

### Authentication and Pages

- `web/src/auth/AuthContext.tsx`
- `web/src/pages/LoginPage.tsx`
- `web/src/pages/TokenPasswordPage.tsx`
- `web/src/pages/ChatPage.tsx`
- `web/src/components/AuthShell.tsx`
- `web/src/components/FormError.tsx`

### Tests and Documentation

- `web/src/api/stream.test.ts`
- `web/src/pages/TokenPasswordPage.test.ts`
- `web/src/test/setup.ts`
- `web/README.md`
- `docs/frontend.md`

## Backend Contract Mapping

| Frontend workflow | Backend endpoint |
| --- | --- |
| Accept invitation | `POST /v1/auth/invitations/accept` |
| Login | `POST /v1/auth/login` |
| Restore account | `GET /v1/auth/me` |
| Acquire CSRF | `GET /v1/auth/csrf` |
| Logout | `POST /v1/auth/logout` |
| Change password | `POST /v1/auth/password/change` |
| Accept reset | `POST /v1/auth/password-reset/accept` |
| List/create Sessions | `GET/POST /v1/sessions` |
| Read history | `GET /v1/sessions/{id}/messages` |
| Rename Session | `PATCH /v1/sessions/{id}` |
| Lifecycle operations | `POST /v1/sessions/{id}/{end|resume|archive}` |
| Streaming dialogue | `POST /v1/chat/stream` |

All calls use `credentials: "include"`. Mutating authenticated calls obtain and attach the current
CSRF token.

## Validation Evidence

Executed from `web/`:

```text
npm test
  3 test files passed
  9 tests passed

npm run build
  TypeScript checks passed
  Vite production build passed
  JS bundle: approximately 61.84 kB gzip
```

Additional checks:

- `npm ls --depth=0` completed with the reviewed dependency set.
- `react-router-dom` is pinned to `6.28.1` for the current Node 18 development environment.
- `git diff --check` passed, with only the repository's existing Windows line-ending warning.
- Desktop login page was inspected in a real browser.
- Background and avatar assets loaded successfully.
- The Vite development server returned HTTP 200 at `http://127.0.0.1:5173/login`.

## Local Test Procedure

Required development settings:

```env
AEMEATH_API_AUTH_MODE=web_session
AEMEATH_WEB_PUBLIC_ORIGIN=http://127.0.0.1:5173
AEMEATH_WEB_AUTH_CSRF_KEY=<local secret of at least 32 bytes>
```

Start the API from the repository root:

```powershell
uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000
```

Start the frontend:

```powershell
cd web
npm run dev
```

Issue an invitation from the repository root:

```powershell
python -m app.auth.admin --origin http://127.0.0.1:5173 create-invite --username rover --display-name 漂泊者
```

The invitation URL is displayed once. After accepting it and setting a password, later logins use
the same username and password at `http://127.0.0.1:5173/login`.

## Debug Finding: Database Path Depends on Working Directory

During local invitation testing, an invitation was accidentally issued from the parent directory.
Because the development database path is relative (`data/aemeath.db`), this created or used:

```text
C:\Users\Administrator\Desktop\project_wuwa\data\aemeath.db
```

while the API used:

```text
C:\Users\Administrator\Desktop\project_wuwa\Aemeath-Agent\data\aemeath.db
```

The API correctly returned the generic invalid-or-expired invitation error because its database did
not contain that token digest. Local operator commands must therefore run from the repository root,
or use a reviewed absolute `AEMEATH_DB_PATH`. No invitation token, CSRF secret, password, password
hash, or Provider credential is recorded in this report.

## Boundaries Preserved

- No backend Python files changed.
- No Persistence schema or migration changed.
- No Caddy, Compose, Docker, or production profile changed.
- No public registration implemented.
- No music, TTS, image-generation, gallery, or portal functionality implemented.
- `frontend/capture_frontend.py` and external raw captures remain untouched.
- Original captured JavaScript is not included in the runtime bundle.
- Browser extension injection, Cloudflare Turnstile, original API paths, and remote music resources
  are not included.

## Manager Review Response

The two required local-runtime repairs are complete:

1. `turn.failed` now refreshes canonical history with `clearError: false`, preserving the public
   failure reason for the user. Successful completion still clears stale errors.
2. Invitation and password-reset credentials are captured once in component state, then the URL
   fragment is removed while preserving the path and query string.

Regression coverage was added in `ChatPage.test.ts` and `TokenPasswordPage.test.ts`. The Manager's
production follow-ups remain deferred: global auth-expiry navigation, Caddy E2E, SPA hosting, and
visible older-history pagination.

## Remaining Risks and Follow-ups

### Required Before Production

1. Deployment approval to serve `web/dist/` from Caddy.
2. SPA fallback for `/login`, `/invite`, `/reset-password`, and `/chat`.
3. Production Web Session profile, CSRF file secret, and Browser Auth operator runbook integration.
4. Authenticated end-to-end test through the deployed Caddy/API path.
5. Review usage rights for temporary Dania images before public release.

### Recommended Frontend Follow-ups

1. Add authenticated component and browser E2E tests for Session lifecycle and streaming dialogue.
2. Add desktop/mobile visual regression screenshots for the chat workspace.
3. Add older-history pagination controls to the current first-page history UI.
4. Add route-level lazy loading if future frontend modules materially increase bundle size.
5. Replace temporary Dania assets through the existing character visual configuration.

## Acceptance Recommendation

Accept this repaired delivery as the Frontend Web MVP local-runtime baseline. Keep production deployment and
authenticated Caddy E2E as separate Manager/Deployment gates rather than expanding the frontend
task across ownership boundaries.
