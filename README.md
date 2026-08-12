# Aemeath Agent

Aemeath Agent is an invitation-only, single-character roleplay dialogue system based on Aemeath
from Wuthering Waves. `v1.0.0` is the first production release: it combines the existing CLI and
modular agent runtime with a browser client, streaming chat, durable Sessions, browser
authentication, usage quotas, observability, and a single-node Docker deployment.

## Release Status

- Current release: `v1.0.0`.
- The core real-ECS path is accepted: Ubuntu 22.04 `linux/amd64`, non-root API/Caddy containers,
  public DNS, automatic trusted HTTPS, private API port, and provider-free health/readiness.
- Reboot persistence, image replacement, D6 restore, and scheduled host-operations drills remain
  tracked operational follow-ups; they do not expand the frozen small-launch product boundary.
- The first public profile is CPU-only, text-only, single-node, and runs one Uvicorn worker.
- Long-term Memory Worker, local BGE-M3, managed vector RAG, audio, and public registration remain
  disabled in production.

See [the v1.0.0 release notes](docs/releases/v1.0.0.md) and
[the deployment runbook](docs/deployment.md) for the exact boundary.

## Current Capabilities

- Character v0.2 manifest and structured prompt assembly.
- Curated World Info keyword evidence through `WorldInfoEvidenceProvider`.
- First-class dialogue Sessions with ownership, title, lifecycle, history, and cursor pagination.
- Recent conversation, optional extracted long-term Memory, and relationship state.
- Shared turn-level semantic query and deterministic context budget.
- DeepSeek-compatible synchronous and streaming model clients.
- Durable SSE streaming with idempotency, cancellation, replay, and disconnect-safe cleanup.
- Invitation-only browser accounts, Cookie Sessions, CSRF, password reset, and password change.
- Per-account daily turn/token-estimate quotas and pre-auth abuse controls.
- React/TypeScript/Vite Web client for login, Session management, and streaming chat.
- JSONL App/Trace logs plus optional local prompt-run debugging.
- SQLite raw and SQLAlchemy-on-SQLite persistence adapters with parity tests.
- Non-root Docker Compose deployment with Caddy HTTPS ingress and static SPA hosting.
- SQLite backup, verification, restore, log retention, and host health operations.

## Architecture

```text
Browser
  -> Caddy: HTTPS, static SPA, security headers, API proxy
  -> FastAPI: Browser Auth, CSRF, rate limit, quota, request ID
  -> AgentStreamService / SessionService
  -> DialogueTurnEngine
  -> Character / Semantic / Memory / Evidence / Prompt / Model / State ports
  -> SQLite

CLI
  -> AgentService
  -> the same DialogueTurnEngine and domain ports
```

`DialogueController` and `DialogueTurnEngine` orchestrate stable ports. HTTP, Cookie, SSE, storage,
provider, and MCP details remain outside the Character, Memory, RAG, Prompt, and State policies.
See [architecture.md](docs/architecture.md) and [interfaces.md](docs/interfaces.md).

## Local CLI

Install development dependencies:

```bash
pip install -e .[dev,api]
```

Copy `.env.example` to `.env`, then use the mock model when no paid Provider call is wanted:

```env
AEMEATH_USE_MOCK_LLM=true
```

Run:

```bash
python -m app.cli
```

Useful commands include `/help`, `/session list`, `/session new`, `/memory list`, and `/exit`.

## Local Web

Start FastAPI in Web Session mode on `127.0.0.1:8000`, then start Vite:

```powershell
uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000
cd web
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/v1`, `/health`, and `/ready` to FastAPI so browser
cookies remain same-origin. Browser accounts and invitation/reset tokens are managed through:

```bash
python -m app.auth.admin --help
```

The public Web flow uses `POST /v1/chat/stream`. Synchronous `/v1/chat` remains available for
development compatibility but is denied by both FastAPI and Caddy in the production Web profile.

## Production Profile

The tracked deployment target is:

- Ubuntu 22.04 x86_64 / `linux/amd64`.
- One ECS host, one API container, one Caddy container, one Uvicorn worker.
- `sqlite_raw`, invitation-only `web_session`, Context Budget enabled.
- DeepSeek over HTTPS; no mock or model fallback.
- App/Trace logs enabled; full prompt-run logs disabled.
- Long-term Memory, Memory Worker, Hash embedding, vector RAG, and audio disabled.
- Daily invitation-test quota of 50 turns and 200,000 `prompt_utf8_v0_1` estimated tokens.

Start with [deploy/prod.env.example](deploy/prod.env.example). Production secrets are mounted files;
never commit `.env`, API keys, CSRF keys, databases, logs, generated indexes, or rendered Compose
configurations containing secrets.

Build the frontend before the Caddy image:

```bash
cd web
npm ci
npm test
npm run build
cd ..
python scripts/deployment/check_deploy_lock.py
docker compose --env-file /etc/aemeath/prod.env config -q
docker compose --env-file /etc/aemeath/prod.env up -d api
docker compose --env-file /etc/aemeath/prod.env up -d proxy
```

Do not enable the `memory` profile in the first release. Backup and restore use the one-shot `ops`
and `restore` profiles under the D6 runbook.

## Optional Development Systems

Long-term Memory can use the approved SiliconFlow BGE-M3 Profile in development:

```env
AEMEATH_ENABLE_LONG_TERM_MEMORY=true
AEMEATH_EMBEDDING_PROFILE=siliconflow_pro_bge_m3_v1
AEMEATH_EMBEDDING_ALLOW_HASH=false
SILICONFLOW_API_KEY=your_siliconflow_api_key
```

The deterministic Hash Profile is restricted to tests and explicit development. Provider failure
never silently switches to Hash. Managed embedding RAG tooling exists under `app/retrieval/`, but
the dialogue runtime continues to use World Info keyword evidence.

## Validation

Backend:

```bash
pytest -q
python -m ruff check .
python -m compileall -q app scripts tests
python scripts/deployment/check_deploy_lock.py
git diff --check
```

Frontend:

```bash
cd web
npm test
npm run build
```

The core real-ECS HTTPS path is verified. Before inviting additional users, complete the remaining
reboot persistence, image replacement, D6 restore, and scheduled host-operations drills in the
deployment runbook.

## Project Coordination

This repository is developed through multiple coding sessions. New sessions must read `AGENTS.md`,
`docs/architecture.md`, `docs/interfaces.md`, and `docs/task_log.md`. The Manager session owns
cross-module contracts, task status, and release acceptance.
