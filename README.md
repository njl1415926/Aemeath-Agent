# Aemeath Agent

Aemeath Agent v0.3 is a CLI roleplay dialogue agent based on Aemeath from
Wuthering Waves. The runtime now combines a composable Character system, World
Info RAG, first-class Sessions, recent conversation, optional extracted
long-term memory, DeepSeek-compatible model calls, and local prompt debugging.

## v0.3 Features

- CLI multi-turn dialogue.
- Character v0.2 manifest loaded from `app/character/Aemeath.v0.2/`.
- World Info keyword RAG through `WorldInfoEvidenceProvider`.
- First-class Session identity, ownership, lifecycle, and message statistics.
- Recent conversation as the always-available short-term memory baseline.
- Optional LLM-extracted long-term memory with explicit and window triggers.
- Scope-aware memory recall: `user_global`, `character`, and `session`.
- Hash embeddings by default, with optional local BGE-M3 embeddings.
- Standalone memory extraction worker and manual extraction command.
- Prompt context assembled as structured sections before model calls.
- Local JSONL prompt-run logs under `.aemeath/sessions/`.
- Prompt log inspector for debugging prompt, RAG, memory, and response behavior.
- Mock model mode for local testing without an API key.

## Quick Start

Install dependencies:

```bash
pip install -e .[dev]
```

Copy the environment template:

```bash
copy .env.example .env
```

Run with the mock model:

```env
AEMEATH_USE_MOCK_LLM=true
```

Start the CLI:

```bash
python -m app.cli
```

Useful CLI commands:

```text
/help
/exit
/session current
/session new
/session list
/session resume <session_id>
/session end [session_id]
/session archive [session_id]
/memory list
/memory delete <memory_id>
/memory disable <memory_id>
/memory extract [limit]
/memory debug on|off
```

## FastAPI Service

Install API dependencies:

```bash
pip install -e .[dev,api]
```

For local dev-token auth, set:

```env
AEMEATH_DEV_AUTH_TOKEN=change-me-local-dev-token
AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER=false
AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false
AEMEATH_API_ENABLE_MODEL_FALLBACK=false
```

Start the API:

```bash
uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000
```

`GET /health` is public. `/v1/*` endpoints require
`Authorization: Bearer <dev token>` by default. Local default-user access is only enabled when
`AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER=true`.

For API runtime, prompt-run logging is disabled by default through
`AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false`. Prompt logs contain full prompt/message snapshots and
should be treated as sensitive local debugging artifacts. Removing `AEMEATH_DEV_AUTH_TOKEN` from
the environment revokes the configured dev token hash on startup for the configured
`AEMEATH_DEV_AUTH_USER_ID`.

API runtime disables model fallback by default through `AEMEATH_API_ENABLE_MODEL_FALLBACK=false`,
so provider auth/rate-limit/timeout errors are surfaced as stable API errors instead of mock
fallback responses. CLI runtime keeps graceful fallback enabled by default.

Current v0.1 endpoints:

```text
GET  /health
POST /v1/chat
GET  /v1/sessions
POST /v1/sessions
GET  /v1/sessions/{session_id}/messages
```

`POST /v1/chat` is synchronous and calls the service layer. If the supplied `session_id` does not
exist yet, v0.1 follows the existing runtime behavior and creates it through `ensure_session`.
Token-by-token streaming is deferred for a future `/v1/chat/stream` endpoint.

## DeepSeek Runtime

To call DeepSeek, edit `.env`:

```env
AEMEATH_USE_MOCK_LLM=false
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_THINKING=disabled
DEEPSEEK_REASONING_EFFORT=
```

Do not commit `.env` or real API keys.

## Long-term Memory

Long-term memory is disabled by default. Enable the v0.3 runtime in `.env`:

```env
AEMEATH_ENABLE_LONG_TERM_MEMORY=true
AEMEATH_MEMORY_EMBEDDING_PROVIDER=hash
```

The deterministic hash provider has no additional model dependency and is
appropriate for development. `bge-m3` is available as an opt-in local semantic
embedding provider, but its model and runtime dependencies must be installed
separately.

The CLI can extract recent messages synchronously:

```text
/memory extract
/memory extract 20
```

Queued extraction jobs are processed by a separate worker:

```bash
python -m app.memory.worker
```

## Prompt Logs

Prompt-run logging is enabled by default:

```env
AEMEATH_ENABLE_PROMPT_RUN_LOG=true
AEMEATH_PROMPT_RUN_LOG_ROOT=.aemeath/sessions
```

After at least one successful CLI turn, inspect the latest prompt log:

```bash
python scripts/inspect_prompt_log.py
```

Common options:

```bash
python scripts/inspect_prompt_log.py --limit 3
python scripts/inspect_prompt_log.py --show-system
python scripts/inspect_prompt_log.py --hide-sections
```

Logs are local runtime data and are ignored by git.

## RAG And Data

The active v0.3 runtime uses the curated World Info file:

```text
app/retrieval/data/Aemeath_World_Info.json
```

The `source_doc/` directory is treated as local source material and is not part
of the release branch.

Embedding RAG utilities exist under `app/retrieval/` and `scripts/`, but vector
RAG is not the active provider in the main runtime yet.

## Upgrading From v0.2

Back up `data/aemeath.db` before first starting v0.3. SQLite initialization
automatically creates Session records for legacy dialogue data and applies the
versioned long-term-memory scope/provenance migration. Existing memories remain
visible as `user_global` memories.

Detailed release and migration notes are in
[`docs/releases/v0.3.md`](docs/releases/v0.3.md).

## Tests

Run:

```bash
pytest
ruff check .
```

Current release validation:

```text
95 passed
All checks passed
```

## Project Coordination

This project is managed as a multi-session coding effort. New coding sessions
should read:

```text
AGENTS.md
docs/architecture.md
docs/interfaces.md
docs/task_log.md
```

The manager session owns architecture, task splitting, and interface stability.
