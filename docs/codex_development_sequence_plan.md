# Aemeath Agent Codex Development Sequence Plan

Last updated: 2026-07-06

This document is the manager-session roadmap for calling future Codex sessions.
It explains which capability to develop next, which worker session should own it,
what files it may touch, and what acceptance checks should pass before moving on.

## Current Baseline

Current released baseline:

- v0.3 text-agent runtime.
- CLI-first usage.
- Session contract and runtime ownership validation.
- Character v0.2 manifest.
- World Info RAG.
- recent conversation.
- optional long-term Memory v0.3 with extraction jobs and worker.
- prompt JSONL logs.
- local Audio / GPT-SoVITS workspace documented but not connected to runtime.

Current architecture principle:

```text
CLI / future API / future Web
  -> Service layer
  -> DialogueController
       -> CharacterProviderPort
       -> MemoryReaderPort / MemoryWriterPort
       -> EvidenceProviderPort
       -> ContextBuilderPort
       -> ResponseGeneratorPort
       -> StateUpdaterPort
       -> MessageStore / SessionStorePort
       -> Observability ports
```

Important current limitation:

- The project has no FastAPI server yet.
- The project has no account system yet.
- The project has no unified production logging yet.
- Context compression is not implemented.
- SQLite migrations exist inside `SQLiteStore`, but there is no standalone migration framework.
- Memory worker exists, but worker operations are still minimal.
- Audio/TTS remains a local GPU-heavy experiment and should stay outside the main runtime.

## Coordination Rules

Before starting any new worker session:

1. Check `git status --short --branch`.
2. Confirm whether another session has active changes in the same files.
3. Read `AGENTS.md`, `docs/architecture.md`, `docs/interfaces.md`, and this plan.
4. If the task changes cross-module contracts, ask the manager session to approve the interface first.
5. Every worker delivery should include:
   - files changed;
   - interfaces added or changed;
   - tests run;
   - risks and deferred work.

Do not run multiple workers on overlapping files unless the manager has explicitly split the scope.

## Phase 0: Stabilize Current In-progress Work

Status: completed on 2026-07-06.

### Goal

Finish and review the current in-progress Error Handling work before opening broad service-side
tasks.

### Why First

Error handling is a dependency for API, service layer, logging, worker operations, and future
frontend user feedback.

### Recommended Codex Call

```text
Worker Session: Error Handling Review And Integration
```

### Inputs

- `docs/error_handling.md`
- `app/errors/`
- `tests/test_errors.py`
- current diffs in:
  - `app/agent/dialogue_controller.py`
  - `app/cli.py`
  - `app/model/deepseek.py`
  - `app/observability/*`

### Scope

- Review the current Error Handling implementation.
- Finish missing tests if necessary.
- Ensure CLI behavior is not broken.
- Ensure prompt logs and trace payloads use sanitized error fields.
- Update `docs/architecture.md`, `docs/interfaces.md`, and `docs/task_log.md` if the
  implementation changed public contracts.

### Acceptance

- `pytest`
- `ruff check .`
- CLI still handles `/session`, `/memory`, and normal turn failures with stable messages.
- API keys, authorization headers, cookies, tokens, passwords, and secrets are not exposed in
  public error payloads or logs.

### Manager Gate

Accepted. Service Layer and FastAPI planning may proceed after the remaining observability and
context-stability work is scheduled.

## Phase 1: Structured Logging And Trace Persistence

### Goal

Build production-friendly structured logging while keeping prompt logs as a separate debugging
artifact.

### Recommended Codex Call

```text
Worker Session: Observability And Structured Logging
```

### Inputs

- `docs/error_handling.md`
- `app/observability/`
- `app/agent/dialogue_controller.py`
- `app/memory/worker.py`
- `scripts/inspect_prompt_log.py`

### Target Design

Separate three observability streams:

```text
App Log      startup, config, requests, CLI/API boundary, worker lifecycle
Trace Log    turn-level agent events
Prompt Log   prompt/messages/sections/response snapshots
```

### Suggested Files

- `app/logging/`
- `app/observability/`
- `app/config.py`
- `docs/observability.md`
- tests under `tests/`

### Interfaces To Consider

- `AppLoggerPort`
- `JsonlTraceLogger`
- `TraceLogRecord`
- reusable sanitizer from `app/errors` if available

### Acceptance

- App log records are structured JSON or structured dicts.
- Log records include useful correlation fields where available:
  - `request_id`
  - `turn_id`
  - `session_id`
  - `user_id`
  - `character_id`
  - `event_name`
  - `error_code`
- Sensitive fields are redacted.
- Existing prompt log behavior still works.
- `pytest`
- `ruff check .`

### Manager Notes

This phase can be developed independently from Context Compression. It should finish before
FastAPI so API request logs reuse the same logger.

## Phase 2: Application Service Layer

### Goal

Insert a stable service boundary between CLI/API/Web and `DialogueController`.

### Why Before FastAPI

FastAPI should not directly call `DialogueController`. A service layer gives one place for:

- request validation;
- user/session ownership checks;
- error mapping;
- response DTOs;
- optional future streaming;
- optional future Audio output;
- app-level logging.

### Recommended Codex Call

```text
Worker Session: Agent Service Layer
```

### Inputs

- `app/main.py`
- `app/cli.py`
- `app/agent/dialogue_controller.py`
- `app/schemas.py`
- `docs/session_contract.md`
- `docs/error_handling.md`

### Suggested Files

- `app/services/__init__.py`
- `app/services/agent_service.py`
- `app/services/session_service.py`
- `app/services/schemas.py`
- tests under `tests/`

### Target Flow

```text
CLI / future API
  -> AgentService.handle_chat(...)
  -> SessionService / permission checks
  -> DialogueController.handle_user_message(...)
  -> AgentService response DTO
```

### Acceptance

- CLI can be migrated to use `AgentService` without changing user-facing behavior.
- `DialogueController` remains orchestration-only.
- Existing tests still pass.
- New tests cover successful chat, invalid session, ended session, archived session, and unknown
  character fallback behavior through the service layer.
- `pytest`
- `ruff check .`

### Manager Gate

Finish this phase before FastAPI, Web, or streaming API work.

## Phase 3: Account System And Owner-only Permission Model

### Goal

Define where `user_id` comes from and enforce owner-only access consistently.

### Recommended Codex Call

```text
Worker Session: Auth And Permission v0.1
```

### Inputs

- `docs/session_contract.md`
- `app/session/`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/services/` after Phase 2

### Suggested Files

- `app/auth/`
- `app/permissions/`
- `docs/auth_and_permissions.md`
- tests under `tests/`

### First-stage Scope

Do not build a full OAuth system first. Start with a minimal local account model:

```text
Account
  id
  username
  password_hash or dev-token identity
  status
  created_at
  last_login_at
```

Recommended permission rule:

```text
owner-only
```

### Acceptance

- A caller identity can be resolved to a stable `user_id`.
- Session list/read/resume/end/archive only works for the owner.
- Memory list/delete/disable/extract only works for the owner.
- Tests cover cross-user denial without leaking ownership details.
- API-ready permission errors map through the unified error system.
- `pytest`
- `ruff check .`

### Deferred

- OAuth.
- refresh tokens.
- admin console.
- shared sessions.
- multi-user rooms.

## Phase 4: Context Compression

### Goal

Add token-budget-friendly conversation summary without replacing raw messages or long-term memory.

### Recommended Codex Call

```text
Worker Session: Context Compression
```

### Inputs

- `app/agent/context_builder.py`
- `app/agent/prompt_context.py`
- `app/agent/dialogue_controller.py`
- `app/model/client.py`
- `docs/interfaces.md`

### Suggested Files

- `app/context_compression/`
- `docs/context_compression.md`
- tests under `tests/`

### Target Boundary

```text
Recent messages
  -> CompressionPolicy
  -> SessionSummary / ContextSummary
  -> PromptContext section
```

### Rules

- Summary is prompt support, not long-term memory.
- Summary never deletes raw messages.
- Summary generation failure does not fail the turn.
- Summary must record source message range.
- `ContextBuilder` receives summary data through a stable schema or port.

### Acceptance

- Long recent history can be summarized into a stable `PromptSection`.
- Raw recent messages are still available.
- Summary has source metadata.
- Tests cover no-summary path, summary injection path, and failure fallback.
- `pytest`
- `ruff check .`

### Manager Notes

This phase can run in parallel with structured logging if file scopes stay separated. Avoid
running it in parallel with Service Layer if both modify `DialogueController`.

## Phase 5: Database Migration And Backup Policy

### Goal

Move from ad hoc SQLite initialization toward explicit migration and backup discipline.

### Recommended Codex Call

```text
Worker Session: Database Migration Policy
```

### Inputs

- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `docs/session_contract.md`
- `docs/releases/v0.3.md`

### Suggested Files

- `docs/database_migration_policy.md`
- optional `app/db/migrations/`
- tests under `tests/`

### First-stage Scope

This does not need to replace every migration immediately. First define and document:

- migration version table ownership;
- startup migration order;
- transactional migration rule;
- rollback expectation;
- backup-before-upgrade rule;
- SQLite concurrency notes;
- path configuration for production.

### Acceptance

- Existing SQLite tests still pass.
- Migration policy explains how to add the next schema change.
- If migration files are introduced, one test verifies idempotency and rollback behavior.
- `pytest`
- `ruff check .`

### Deferred

- Full Alembic-style framework.
- Postgres migration.
- online migration.

## Phase 6: Worker Operations And Async Job Management

### Goal

Make the Memory worker operationally safer for deployment.

### Recommended Codex Call

```text
Worker Session: Worker Operations
```

### Inputs

- `app/memory/worker.py`
- `app/memory/store.py`
- `app/db/sqlite_store.py`
- Phase 1 logging if available

### Suggested Improvements

- structured worker logs;
- stale running job reclaim;
- max attempts;
- clearer failure reasons;
- worker heartbeat or lightweight status;
- debug command or script to list jobs;
- deployment notes.

### Acceptance

- Failed jobs are visible and do not loop silently forever.
- Stale jobs can be retried or reclaimed safely.
- Worker startup/shutdown logs are structured.
- Existing memory extraction behavior remains compatible.
- `pytest`
- `ruff check .`

### Deferred

- Celery/RQ/Dramatiq.
- distributed locks.
- external queue service.

## Phase 7: Configuration And Secret Management

### Goal

Prepare configuration for service deployment without leaking secrets.

### Recommended Codex Call

```text
Worker Session: Configuration And Secrets
```

### Inputs

- `app/config.py`
- `.env.example`
- `docs/error_handling.md`
- Phase 1 logging

### Suggested Files

- `docs/configuration.md`
- tests under `tests/`

### Scope

- add `AEMEATH_ENVIRONMENT`;
- define dev/test/prod behavior;
- validate required secrets at startup;
- document every environment variable;
- separate local `.env` from production environment variables;
- ensure config errors map through the unified error system.

### Acceptance

- Missing required model secret in real-model mode produces a stable config error.
- `.env.example` has safe placeholders only.
- Tests cover boolean/int/float config parsing and missing secret behavior.
- `pytest`
- `ruff check .`

## Phase 8: FastAPI Server API

### Goal

Add the first HTTP API layer after Service Layer, Auth, Error Mapping, Logging, and Config are ready.

### Recommended Codex Call

```text
Worker Session: FastAPI Server v0.1
```

### Inputs

- `app/services/`
- `app/auth/`
- `app/errors/`
- `app/logging/`
- `docs/interfaces.md`

### Suggested Files

- `app/api/server.py`
- `app/api/schemas.py`
- `app/api/dependencies.py`
- `app/api/routes/chat.py`
- `app/api/routes/sessions.py`
- `app/api/routes/memory.py`
- `app/api/routes/health.py`
- tests under `tests/`

### First API Set

```text
GET  /health
POST /sessions
GET  /sessions
POST /sessions/{session_id}/resume
POST /sessions/{session_id}/end
POST /sessions/{session_id}/archive
POST /chat
```

### Acceptance

- API uses service layer, not `DialogueController` directly.
- API responses have stable schemas.
- API errors use unified error payloads.
- User identity is resolved through the selected auth method.
- Tests cover happy path and permission denial.
- `pytest`
- `ruff check .`

### Deferred

- streaming.
- WebSocket.
- Audio.
- admin endpoints.

## Phase 9: Streaming Chat API

### Goal

Support real streaming later without breaking the current synchronous turn path.

### Recommended Codex Call

```text
Worker Session: Streaming Chat Design Spike
```

### Inputs

- `app/model/client.py`
- `app/model/deepseek.py`
- `app/services/agent_service.py`
- `app/api/`

### First-stage Output

Prefer a design document and small interface spike before full implementation.

Potential interface:

```text
StreamingChatModelClient.stream_chat(...)
AgentStreamService.stream_user_message(...)
```

### Important Rule

Keep current `handle_user_message(...)` stable. Add streaming beside it.

### Acceptance

- Design explains how to save final assistant message after stream completion.
- Design explains how to write prompt logs and trace events for failed/incomplete streams.
- Design explains cancellation and client disconnect behavior.

## Phase 10: Minimal Web Frontend

### Goal

Build a small Web UI on top of the API.

### Recommended Codex Call

```text
Worker Session: Web MVP
```

### Inputs

- Phase 8 API.
- `docs/interfaces.md`.

### MVP Scope

- login or dev identity selection;
- session list;
- create/resume/end session;
- chat window;
- warning/error display;
- optional memory debug panel.

### Acceptance

- Web uses API only.
- No direct SQLite or Controller access.
- Basic mobile and desktop layout works.
- API error messages are displayed safely.
- Manual smoke test documented.

### Deferred

- prompt log viewer.
- admin memory panel.
- audio playback.
- streaming UX.

## Phase 11: Test System And CI

### Goal

Make regression checks reliable before deployment.

### Recommended Codex Call

```text
Worker Session: Test And CI Hardening
```

### Inputs

- all existing tests;
- future API tests;
- future service tests.

### Scope

- organize unit/integration/API tests;
- add smoke test scripts;
- add GitHub Actions or local CI documentation;
- ensure tests can run without real API keys;
- ensure tests do not write to real `data/aemeath.db`.

### Acceptance

- `pytest`
- `ruff check .`
- `python -m compileall -q app`
- CI or documented equivalent runs all checks in clean environment.

## Phase 12: Deployment

### Goal

Make the text Agent deployable as a service.

### Recommended Codex Call

```text
Worker Session: Deployment v0.1
```

### Inputs

- Phase 8 API.
- Phase 6 worker operations.
- Phase 7 config.

### Suggested Files

- `Dockerfile`
- `docker-compose.yml`
- `docs/deployment.md`
- `scripts/backup_sqlite.ps1` or documented backup command

### Minimum Deployment Shape

```text
web api process
memory worker process
sqlite data volume
prompt/trace/app log volume
environment variables / secrets
```

### Acceptance

- Text-only deployment works without GPU.
- Memory worker can be enabled as a separate process.
- Health check works.
- Data and logs are mounted outside the image.
- Backup/restore instructions exist.
- Real secrets are not baked into images or docs.

## Phase 13: Memory Quality Improvements

### Goal

Improve Memory after service foundations are stable.

### Recommended Codex Call

```text
Worker Session: Memory Quality v0.4
```

### Candidate Scope

- scope classifier for `user_global` / `character` / `session`;
- memory consolidation and conflict handling;
- memory decay;
- better extraction prompts;
- better retrieval scoring;
- eval samples for recall precision.

### Dependency

Do this after logging, service layer, permissions, and worker operations. Memory quality work is
much easier to debug when trace and worker status are stable.

## Phase 14: Audio / TTS Integration Decision

### Goal

Decide whether and how to connect local GPT-SoVITS or a remote TTS service.

### Recommended Codex Call

```text
Worker Session: Audio Adapter Design
```

### Inputs

- `docs/audio_module.md`
- `docs/interfaces.md`
- future API/Web design

### Rule

Audio is optional and post-response:

```text
AgentTurnOutput.response
  -> optional TextToSpeechPort
  -> audio metadata / file / URL / stream
```

### Acceptance

- Text Agent remains deployable without GPU.
- TTS failure does not fail text chat.
- GPT-SoVITS details stay in adapter or sidecar service.
- Interface supports local, remote, and disabled providers.

## Suggested Parallelization

Safe-ish parallel tracks:

```text
Track A: Error Handling -> Service Layer -> FastAPI
Track B: Structured Logging -> Worker Operations
Track C: Context Compression
Track D: DB Migration Policy -> Deployment
Track E: Auth/Permission, after Service Layer design is stable
```

Avoid parallel edits to:

- `app/agent/dialogue_controller.py`
- `app/main.py`
- `app/db/sqlite_store.py`
- `app/schemas.py`
- `docs/interfaces.md`

These files are integration hotspots and should be manager-reviewed between worker sessions.

## Recommended Immediate Next Calls

Use this exact order unless priorities change:

1. Open Observability / Structured Logging worker.
2. Open Context Compression worker in parallel only if it avoids `DialogueController` changes until
   its design is approved.
3. Open Agent Service Layer worker.
4. Open Auth + Permission worker.
5. Open DB Migration Policy worker.
6. Open Worker Operations worker.
7. Open FastAPI Server worker.
8. Open Test/CI hardening worker.
9. Open Deployment worker.

## Generic Worker Prompt Template

```text
Worker Session: <Name>

You are working on Aemeath Agent. Read AGENTS.md first, then read:

- docs/architecture.md
- docs/interfaces.md
- docs/task_log.md
- docs/codex_development_sequence_plan.md
- <task-specific docs>

Do not start implementation immediately. First summarize:

1. your understanding of the current architecture;
2. the exact scope you propose;
3. files you expect to modify;
4. interfaces you need to add or change;
5. tests you plan to run;
6. risks or manager decisions needed.

After manager approval, implement within the approved scope. Do not modify unrelated modules or
revert other sessions' changes.
```

## Manager Review Checklist

After each worker delivery, the manager should check:

- Does it preserve `DialogueController` as an orchestration layer?
- Are new capabilities behind ports or services?
- Are errors mapped through the unified error system?
- Are logs structured and sanitized?
- Are user/session/memory ownership rules respected?
- Are tests focused and sufficient?
- Did the worker update docs for any interface or architecture changes?
- Did the worker avoid committing local runtime data, `.env`, DB files, prompt logs, source docs,
  and Audio workspace artifacts?
