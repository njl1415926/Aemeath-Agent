# Aemeath Agent

Aemeath Agent is a CLI roleplay dialogue agent based on Aemeath from Wuthering
Waves. Its v0.3 baseline and current development checkpoint combine a composable
Character system, World Info RAG, first-class Sessions, recent conversation,
optional extracted long-term memory, a shared turn-level semantic query,
DeepSeek-compatible model calls, and local prompt debugging.

## v0.3 Baseline And Current Checkpoint Features

- CLI multi-turn dialogue.
- Character v0.2 manifest loaded from `app/character/Aemeath.v0.2/`.
- World Info keyword RAG through `WorldInfoEvidenceProvider`.
- First-class Session identity, ownership, lifecycle, and message statistics.
- Recent conversation as the always-available short-term memory baseline.
- Optional LLM-extracted long-term memory with explicit and window triggers.
- Scope-aware memory recall: `user_global`, `character`, and `session`.
- Approved deterministic Hash embeddings for tests and explicit development;
  SiliconFlow and local BGE-M3 remain qualification-gated.
- Standalone memory extraction worker and manual extraction command.
- Prompt context assembled as structured sections before model calls.
- Optional deterministic prompt context budget with protected role and safety sections.
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
AEMEATH_EMBEDDING_PROFILE=deterministic_hash_v1
AEMEATH_EMBEDDING_ALLOW_HASH=true
```

The deterministic hash provider has no additional model dependency and is
approved only for tests and explicit development. The legacy
`AEMEATH_MEMORY_EMBEDDING_PROVIDER=hash` setting remains available for one
compatibility release and resolves through the same approved-profile gate.
SiliconFlow and local BGE-M3 profiles cannot be selected by the normal runtime
until their qualification profiles are separately approved.

The CLI can extract recent messages synchronously:

```text
/memory extract
/memory extract 20
```

Queued extraction jobs are processed by a separate worker:

```bash
python -m app.memory.worker
```

The worker repairs missing or invalid exact-signature Memory artifacts in
bounded batches. Repair cadence and backoff use
`AEMEATH_MEMORY_REPAIR_BATCH_SIZE`, `AEMEATH_MEMORY_REPAIR_INTERVAL_SECONDS`,
and `AEMEATH_MEMORY_REPAIR_MAX_BACKOFF_SECONDS`.

When long-term Memory is enabled, the dialogue runtime constructs one turn-local
semantic query and shares its resolved embedding with Memory recall. The query
and vector are not persisted. Memory does not issue a second query embedding;
keyword and explicit-recall fallbacks remain available when semantic resolution
is skipped or temporarily unavailable. Canonical RAG and Character example
selection do not consume this context yet.

## Prompt Context Budget

The context budget is disabled by default. Enable the deterministic v0.1 policy with:

```env
AEMEATH_ENABLE_CONTEXT_BUDGET=true
AEMEATH_CONTEXT_BUDGET_MAX_CHARS=24000
AEMEATH_CONTEXT_BUDGET_RECENT_MESSAGE_LIMIT=8
AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT=4
AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT=4
```

The policy trims optional Style, Voice, Canon, Memory, and Recent sections in that order. Character
identity, role rules, safety boundaries, relationship state, unknown sections, and the current user
message are never truncated. This is a deterministic character-count guardrail rather than an exact
model-token limit. Prompt-run metadata records kept/dropped ids and counts without storing removed
content.

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

Managed embedding RAG tooling exists under `app/retrieval/`, but vector RAG is
not the active provider in the main runtime. Each managed generation has an
isolated SQLite database, canonical inventory, checkpoint, and ready manifest.
The active runtime remains `WorldInfoEvidenceProvider`.

Build an explicit development Hash generation:

```bash
python -m app.retrieval.manage_rag_index build --profile deterministic_hash_v1 --allow-hash
```

Inspect or query managed generations:

```bash
python -m app.retrieval.manage_rag_index status
python -m app.retrieval.manage_rag_index query --profile deterministic_hash_v1 --allow-hash "query"
```

The managed root defaults to `data/rag_indexes` and can be changed with
`AEMEATH_RAG_INDEX_ROOT`. Hash requires explicit approval on every build/query;
local BGE-M3 and SiliconFlow profiles remain unavailable until separately
approved.

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

Current development-checkpoint validation:

```text
438 passed
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
