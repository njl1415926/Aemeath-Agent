# Worker Session: Observability And Structured Logging

You are working on Aemeath Agent as a worker session. Read `AGENTS.md` first.

Your task id is `T-0124 Observability And Structured Logging v0.1`.

Do not start implementation immediately. First read the files below and reply with a short design
proposal for manager review:

- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `docs/error_handling.md`
- `docs/codex_development_sequence_plan_cn.md`
- `app/observability/`
- `app/errors/`
- `app/config.py`
- `app/main.py`
- `app/agent/dialogue_controller.py`
- `app/memory/worker.py`
- `scripts/inspect_prompt_log.py`
- related tests under `tests/`

## Current Project State

Aemeath Agent is currently a local CLI-first agent runtime. The main turn flow is:

```text
CLI / future API
  -> SessionStorePort
  -> DialogueController
       -> CharacterProviderPort
       -> MemoryReaderPort / MemoryWriterPort
       -> EvidenceProviderPort
       -> ContextBuilderPort
       -> ResponseGeneratorPort
       -> StateUpdaterPort
       -> MessageStore
       -> TraceLoggerPort
       -> PromptRunLoggerPort
```

Important existing observability pieces:

- `ConversationRun`, `TraceEvent`, and `PromptRunRecord` live in `app/observability/schemas.py`.
- `TraceLoggerPort` and `PromptRunLoggerPort` live in `app/observability/ports.py`.
- `NoopTraceLogger` is currently wired in `app/main.py`.
- `JsonlPromptRunLogger` already writes local prompt snapshots under `.aemeath/sessions/...`.
- `DialogueController` already emits several `TraceEvent` records:
  - `session_validation_failed`
  - `turn_started`
  - `character_id_fallback`
  - `context_sources_selected`
  - `prompt_context_built`
  - `response_generated`
  - `state_update_decided`
  - `memory_write_decided`
  - `turn_completed`
  - `turn_failed`
- Error Handling v0.1 is done under `app/errors/`. Reuse `map_exception(...)`,
  `ErrorInfo.to_log_dict()`, and `redact_sensitive(...)`.

## Required Boundary

Keep these three observability artifacts separate:

```text
App Log      process/runtime/application events
Trace Log    one agent turn's internal structured events
Prompt Log   prompt/messages/sections/response snapshot for prompt debugging
```

Do not merge prompt-run logs into trace logs. Prompt logs may contain full prompt/messages and are
for local debugging/eval. Trace and app logs should default to IDs, counts, statuses, durations,
error codes, and summarized metadata, not full prompt text.

## Proposed Scope

Please propose a minimal v0.1 design for:

1. Structured app logging:
   - likely new module: `app/logging/` or `app/observability/app_logger.py`;
   - likely port: `AppLoggerPort.record(record) -> None`;
   - records should support fields such as `event_name`, `level`, `created_at`, `request_id`,
     `turn_id`, `session_id`, `user_id`, `character_id`, `error_code`, and sanitized `payload`.

2. JSONL trace persistence:
   - implement a `JsonlTraceLogger` for existing `TraceLoggerPort`;
   - write UTF-8 JSONL with `ensure_ascii=False`;
   - use a local ignored root such as `.aemeath/traces`;
   - keep `NoopTraceLogger` for disabled logs and tests.

3. Configuration:
   - add enable/disable flags and log roots in `Settings`;
   - do not require external services;
   - local runtime should remain easy to run with `python -m app.cli`.

4. Runtime wiring:
   - wire `app/main.py` to use JSONL trace logger when enabled;
   - keep prompt-run logger behavior unchanged;
   - optionally add minimal structured app logs for CLI startup and memory worker lifecycle, but
     avoid broad rewrites.

5. Tests:
   - JSONL trace logger writes valid one-line JSON with Chinese unescaped;
   - trace/app log payloads redact sensitive keys such as `api-key`, `x-api-key`, `token`,
     `authorization`, and `cookie`;
   - existing prompt-run logger tests still pass;
   - failed turn logs include stable error information without leaking secrets;
   - disabled logging does not create local log files.

## Out Of Scope

Do not implement these in this task:

- FastAPI server;
- Web frontend;
- account/auth system;
- service layer;
- OpenTelemetry or cloud log shipping;
- log query UI;
- context compression;
- changing memory behavior;
- changing character/RAG behavior;
- changing prompt assembly order.

## Deliverables

After manager approves your design, implement the agreed minimal scope and deliver:

- files changed;
- interfaces added or changed;
- config variables added;
- tests added or updated;
- validation results for `pytest` and `ruff check .`;
- any risks or deferred work.

Also update:

- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- create or update `docs/observability.md`

Keep the implementation small and interface-oriented. The goal is to give future FastAPI,
Service Layer, Worker Operations, and deployment work a stable logging foundation without turning
this task into a full observability platform.
