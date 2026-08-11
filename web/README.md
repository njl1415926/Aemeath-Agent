# Aemeath Web

React/TypeScript frontend for the invitation-only Aemeath Agent Web MVP.

## Local development

Start the FastAPI service in Web Session mode on `127.0.0.1:8000`, then run:

```powershell
cd web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/v1`, `/health`, and `/ready` to the
local FastAPI service, so browser cookies and CSRF requests stay same-origin.

## Validation

```powershell
npm test
npm run build
```

The current visual assets under `src/assets/` are temporary Dania references. Vite emits content-
hashed production files. Their selection and display name are isolated in
`src/config/character.ts` and the shared stylesheet so a later Aemeath asset replacement does not
require chat, Session, or Auth changes.

## Production artifact

Run `npm ci`, `npm test`, and `npm run build` before building the Caddy image. `web/dist/` is copied
into the non-root proxy image; it is generated output and remains outside Git.

## Scope

Implemented:

- invitation acceptance, login, password reset, logout, and password change;
- Session create/list/title/end/resume/archive and history reads;
- POST SSE streaming chat with cancellation;
- responsive desktop and mobile chat layouts.

Deferred: public registration, music, image generation, TTS, gallery, and third-party portal pages.
