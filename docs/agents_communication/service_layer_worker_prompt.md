# Worker Session 8: Service Layer

You are working on Aemeath Agent as a worker session. Read `AGENTS.md` first.

Your task id is `T-0125 Agent Service Layer v0.1`.

Do not start implementation immediately. First read the files below and reply with a short design
proposal for manager review:

- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `docs/session_contract.md`
- `docs/error_handling.md`
- `docs/observability.md`
- `docs/codex_development_sequence_plan_cn.md`
- `app/main.py`
- `app/cli.py`
- `app/schemas.py`
- `app/agent/dialogue_controller.py`
- `app/session/`
- `app/errors/`
- `app/observability/`
- related tests under `tests/`

## Current Project State

Aemeath Agent is currently CLI-first, with a modular runtime:

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

Important completed foundations:

- Session Contract v0.1 exists. `session_id`, `user_id`, and `character_id` are stable runtime
  identity fields.
- Memory v0.3 has long-term memory, scope/provenance, extraction jobs, and session-aware queries.
- Error Handling v0.1 exists under `app/errors/`; reuse `map_exception(...)`, `ErrorInfo`, and the
  sanitizer.
- Observability v0.1 exists. App Log, Trace Log, and Prompt Log are separate. `AppLoggerPort`
  should live at CLI/API/service/worker boundaries, not inside `DialogueController`.
- `DialogueController` should remain an orchestration layer.

## Why This Task Exists

Future FastAPI, Web, Auth, Streaming, Audio, and Worker Operations should not call
`DialogueController` directly. This task creates a stable service boundary:

```text
CLI / future API / Web
  -> AgentService / optional SessionService
       -> DialogueController
            -> Character / Memory / RAG / Prompt / Model / State / Session
```

The service layer is an application boundary, not a new business-policy module.

## Required Design Proposal

Before coding, propose:

1. Files you expect to add/change.
2. DTOs you propose, especially:
   - `UserContext`
   - `ChatRequest`
   - `ChatResponse`
   - any `ServiceError` / `ServiceResult` shape if you recommend one
3. Whether `AgentService.handle_chat(...)` should:
   - return a structured success/error result, or
   - return `ChatResponse` on success and raise exceptions for adapter layers to map.
4. Whether CLI normal chat should be migrated in this task or left for a follow-up.
5. Whether a minimal `SessionService` is useful in v0.1 or should wait.
6. How you will reuse `app/errors` and `app/observability` without duplicating their logic.
7. Tests you plan to add.

Wait for manager approval before implementation.

## Suggested Scope

Recommended files:

- `app/services/__init__.py`
- `app/services/schemas.py`
- `app/services/agent_service.py`
- optional: `app/services/session_service.py`
- tests under `tests/`
- update docs:
  - `docs/architecture.md`
  - `docs/interfaces.md`
  - `docs/task_log.md`

Suggested DTO direction:

```python
@dataclass(frozen=True)
class UserContext:
    user_id: str
    roles: list[str] = field(default_factory=list)
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatRequest:
    session_id: str
    character_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatResponse:
    turn_id: str
    session_id: str
    character_id: str
    response: str
    warnings: list[str]
    used_memory_ids: list[str]
    state_delta: dict[str, int]
```

You may propose adjustments if existing code suggests a better shape.

## Out Of Scope

Do not implement these in this task:

- account registration or login;
- password hashing, OAuth, token, cookie, or refresh-token flows;
- full owner-only permission model;
- FastAPI server;
- Web frontend;
- streaming API, SSE, or WebSocket;
- Audio/TTS output;
- context compression;
- changing prompt assembly order;
- changing Character, RAG, Memory, Model, or State policies.

## Acceptance Criteria

After manager approves your design and you implement:

- `AgentService` can call `DialogueController` through stable service DTOs.
- `UserContext` exists as the future Auth insertion point.
- Future API/Web adapters do not need to know `AgentTurnInput` internals.
- `DialogueController` remains orchestration-only.
- Existing CLI behavior is unchanged if CLI is migrated.
- Tests cover:
  - successful chat through service;
  - ended session failure;
  - archived or invalid session behavior where practical;
  - unknown character fallback behavior;
  - error mapping or result shape;
  - no sensitive values leaked in service-level logs/errors if app logging is used.
- Run:
  - `pytest`
  - `ruff check .`

Also report:

- files changed;
- interfaces added/changed;
- whether CLI was migrated;
- whether `SessionService` was added or deferred;
- risks and deferred work.
