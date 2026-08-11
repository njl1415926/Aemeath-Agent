# Aemeath Web Frontend

## Scope

The Web MVP lives in `web/` and is an independent React/TypeScript/Vite application. It consumes
the existing FastAPI service and does not call `DialogueController`, repositories, or database
adapters directly.

Current user-facing scope:

- invitation acceptance, login, password reset, logout, and password change;
- Session create/list/title/end/resume/archive and message history;
- POST SSE streaming chat with an explicit stop action;
- responsive desktop and mobile layouts.

Public registration, music, image generation, TTS, gallery, and the captured portal pages are not
part of the MVP.

## Architecture

```text
React routes and components
  -> src/api/client.ts       JSON API, Cookie, CSRF, public errors
  -> src/api/stream.ts       POST SSE parsing and cancellation
  -> FastAPI /v1/*
```

Authentication uses the HttpOnly `__Host-aemeath_session` Cookie. JavaScript never reads the
Cookie. CSRF tokens are fetched from `GET /v1/auth/csrf`, held only in memory, and attached to
mutating requests.

Invitation and password-reset tokens are read from the URL fragment into component memory. The
fragment is then removed immediately with `history.replaceState`, preserving only the route and
non-sensitive query string.

All API errors pass through one browser-session invalidation boundary. Expired, revoked, missing,
or disabled-account credentials clear the in-memory account and CSRF token. Protected routes then
return to `/login`, and unmounting the chat view aborts any active stream.

Streaming state remains local to the active chat view. `AbortController` closes the browser fetch;
the backend owns durable cancellation, terminal-event arbitration, and resource cleanup.

## Visual Reference

The Dania background and avatar are temporary reference assets. They live under `web/src/assets`
and enter the Vite module graph, so production filenames are content-hashed. They are selected only
through `web/src/config/character.ts` and the shared stylesheet. Components must not hard-code
public asset paths or display
names. This lets a future Aemeath asset package replace the current visuals without changing chat,
Session, or Auth logic.

The original captured scripts, music files, injected browser-extension nodes, Cloudflare widget,
and third-party API paths are not runtime dependencies.

## Development

```powershell
cd web
npm install
npm run dev
```

Vite listens on `http://127.0.0.1:5173` and proxies API requests to
`http://127.0.0.1:8000`.

Validation:

```powershell
npm test
npm run build
```

## Deployment Boundary

`web/dist/` is the production artifact. The production Caddy image copies this prebuilt tree,
serves hashed `/assets/*` with immutable caching, uses `/index.html` as the GET/HEAD SPA fallback,
and forwards only `/v1/*`, `/health`, and `/ready` to FastAPI. The Web build must complete before
`docker compose build proxy`.
