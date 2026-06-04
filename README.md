# Aemeath Agent

Aemeath Agent v0.2 is a CLI roleplay dialogue agent based on Aemeath from
Wuthering Waves. The current runtime focuses on a small but extensible loop:
character context, World Info RAG, recent conversation memory, prompt assembly,
DeepSeek-compatible model calls, and local prompt-run debugging logs.

## v0.2 Features

- CLI multi-turn dialogue.
- Character v0.2 manifest loaded from `app/character/Aemeath.v0.2/`.
- World Info keyword RAG through `WorldInfoEvidenceProvider`.
- Recent conversation as the default minimal memory baseline.
- Optional long-term memory behind `AEMEATH_ENABLE_LONG_TERM_MEMORY`.
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
/memory list
/memory delete <memory_id>
/memory disable <memory_id>
```

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

The active v0.2 runtime uses the curated World Info file:

```text
app/retrieval/data/Aemeath_World_Info.json
```

The `source_doc/` directory is treated as local source material and is not part
of the release branch.

Embedding RAG utilities exist under `app/retrieval/` and `scripts/`, but vector
RAG is not the active provider in the main runtime yet.

## Tests

Run:

```bash
pytest
ruff check .
```

Current release validation:

```text
pytest
ruff check .
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
