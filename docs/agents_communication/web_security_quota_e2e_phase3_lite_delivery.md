# T-0146 Phase 3 Lite Delivery

Status: Code complete; Ubuntu Docker/Caddy environment smoke remains open.

## Completed

- Production now accepts the reviewed `web_session` profile and rejects mixed dev Bearer
  credentials. The profile requires the `_FILE` CSRF secret and exact HTTPS public origin.
- The invitation-test quota is fail-closed at 50 turns and 200,000 `prompt_utf8_v0_1` estimated
  tokens per UTC day. This is an internal cost guard, not Provider billing parity.
- Compose mounts only the DeepSeek and Web CSRF secrets into API. The production API no longer
  receives the dev Bearer secret.
- Caddy denies synchronous `POST /v1/chat`, proxies `/v1/*`, `/health`, and `/ready`, and serves the
  prebuilt Vite SPA with fallback and reviewed security headers. HSTS remains deferred to D8.
- Character images now enter the Vite module graph and produce content-hashed files. React inline
  style usage was removed so production can keep `style-src 'self'` without `unsafe-inline`.
- The frontend has one authentication invalidation boundary for expired, revoked, missing, and
  disabled-account credentials. Clearing the account unmounts chat and aborts its active stream.

## Validation

- `npm test`: 11 passed.
- `npm run build`: passed; JS gzip approximately 62.02 kB and visual assets were hashed.
- Focused production/config/API/deployment tests: passed.
- Full `pytest -q`: passed with existing skips and deprecation warnings only.
- `docker compose config -q`: passed with the tracked production template and test domain.

## Deferred Environment Gate

- The local Docker CLI is installed, but the Docker daemon is unavailable. The proxy image build,
  real Caddy config validation, and browser-through-Compose smoke were therefore not run.
- D6 backup/restore, D7 operations, D8 public-domain/TLS acceptance, full Playwright matrices,
  quota admin UI/query, and final visual replacement remain outside Phase 3 Lite.
