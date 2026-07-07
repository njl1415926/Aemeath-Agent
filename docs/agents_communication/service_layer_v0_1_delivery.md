# Worker Session 8 Delivery: Agent Service Layer v0.1

Date: 2026-07-06

Task: `T-0125 Agent Service Layer v0.1`

Status: implementation complete, ready for Manager review

## Summary

Implemented the first-stage application service boundary for Aemeath Agent.

This task inserts `AgentService` between CLI / future API / future Web adapters and
`DialogueController`. The service layer is intentionally thin: it translates stable service DTOs
to the existing internal turn contract, returns a stable chat response DTO on success, and
re-raises original exceptions on failure so adapters can keep using the shared error mapper.

Delivered:

- new `app/services/` package;
- `UserContext` as the future Auth insertion point;
- `ChatRequest` and `ChatResponse` service DTOs;
- `AgentService.handle_chat(...)`;
- optional `AppLoggerPort` injection at the service boundary, defaulting to `NoopAppLogger`;
- `build_agent_service(...)` wiring in `app/main.py`;
- CLI normal chat path migrated to `AgentService`;
- focused service tests and CLI regression coverage;
- architecture, interface, and task-log documentation updates.

Out of scope items were not implemented:

- account registration or login;
- password, token, cookie, OAuth, or refresh-token flows;
- role permission or full owner-only permission model;
- `ServiceResult` / `ServiceError`;
- `SessionService`;
- FastAPI / Web / Streaming / Audio;
- changes to Character, Memory, RAG, Prompt, Model, State, or Session policies.

## Added Files

- `app/services/__init__.py`
- `app/services/schemas.py`
- `app/services/agent_service.py`
- `tests/test_agent_service.py`
- `docs/agents_communication/service_layer_v0_1_delivery.md`

## Modified Files

- `app/main.py`
- `app/cli.py`
- `tests/test_cli_errors.py`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`

Note: the working tree already contained unrelated uncommitted Error Handling, Observability, and
Audio documentation changes before this worker started. This delivery only covers the Service
Layer files and the minimal integration points listed above.

## Public Interfaces

### UserContext

Defined in `app/services/schemas.py`.

```python
@dataclass(frozen=True)
class UserContext:
    user_id: str
    roles: list[str] = field(default_factory=list)
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

Purpose:

- holds the caller identity supplied by CLI today and future Auth/API adapters later;
- keeps `roles`, `request_id`, and `metadata` available as future adapter context;
- does not implement authorization logic in v0.1.

### ChatRequest

Defined in `app/services/schemas.py`.

```python
@dataclass(frozen=True)
class ChatRequest:
    session_id: str
    character_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

Purpose:

- gives CLI / future API / future Web a stable request shape;
- hides `AgentTurnInput` from application adapters;
- preserves metadata for channel/client/request information.

### ChatResponse

Defined in `app/services/schemas.py`.

```python
@dataclass(frozen=True)
class UsedMemorySummary:
    id: str
    content: str
    score: float | None
    match_reason: str


@dataclass(frozen=True)
class ChatResponse:
    turn_id: str
    session_id: str
    character_id: str
    response: str
    warnings: list[str]
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    used_memories: list[UsedMemorySummary] = field(default_factory=list)
```

Notes:

- `ChatResponse.character_id` currently represents the requested target character.
- Unknown-character fallback remains visible through `warnings`.
- `AgentTurnOutput` was not changed for this task.

### AgentService

Defined in `app/services/agent_service.py`.

```python
class AgentService:
    def handle_chat(
        self,
        *,
        user_context: UserContext,
        request: ChatRequest,
    ) -> ChatResponse:
        ...
```

Behavior:

- success returns `ChatResponse`;
- failure re-raises the original exception;
- adapters should call `map_exception(exc)` to produce public error payloads;
- no `ServiceResult` or `ServiceError` was introduced.

## Runtime Flow

Before this task, normal chat went through:

```text
CLI
  -> AgentTurnInput
  -> DialogueController.handle_user_message(...)
  -> AgentTurnOutput
```

After this task, normal chat goes through:

```text
CLI / future API / future Web
  -> UserContext + ChatRequest
  -> AgentService.handle_chat(...)
  -> AgentTurnInput
  -> DialogueController.handle_user_message(...)
  -> AgentTurnOutput
  -> ChatResponse
```

`DialogueController` remains the orchestration layer. It still owns turn orchestration across:

- session validation;
- character loading and fallback;
- message persistence;
- memory recall and memory write decisions;
- RAG evidence;
- prompt context building;
- response generation;
- state update;
- trace log and prompt-run log.

`AgentService` does not duplicate or move those policies.

## App Logging

`AgentService` accepts an optional `AppLoggerPort`.

Wiring:

- `build_agent_service(...)` injects `JsonlAppLogger` when `AEMEATH_ENABLE_APP_LOG=true`;
- otherwise it injects `NoopAppLogger`.

Service app logs record summary-only fields:

- `request_id`;
- `user_id`;
- `session_id`;
- `character_id`;
- `status`;
- `turn_id`;
- `error_code`;
- `duration_ms`;
- `warning_count`.

Service app logs do not record:

- prompt text;
- model messages;
- user message text;
- provider raw responses;
- API keys;
- authorization headers;
- cookies;
- tokens.

Payloads are passed through the existing `app.errors.redact_sensitive(...)` sanitizer, and the
JSONL app logger also sanitizes payloads before writing.

## CLI Migration

Only the normal chat path was migrated.

Migrated:

```text
plain user input
  -> ChatRequest
  -> AgentService.handle_chat(...)
```

Not migrated:

- `/session`;
- `/memory`;
- `/memory debug`;
- CLI session command helpers.

This keeps CLI/session/memory command behavior out of this task and avoids turning Service Layer
v0.1 into a CLI/session refactor.

## Error Handling

This task deliberately does not create a second service-level error taxonomy.

The contract is:

```text
AgentService.handle_chat(...)
  -> returns ChatResponse on success
  -> re-raises original exception on failure

CLI / future API adapter
  -> catches exception
  -> map_exception(exc)
  -> user/API-facing response
```

On service failure, `AgentService` maps the exception only to obtain the stable `error_code` for
summary app logging, then re-raises the original exception.

## Tests

Added `tests/test_agent_service.py` covering:

- successful chat through `AgentService`;
- ended session failure through `AgentService`;
- archived session failure through `AgentService`;
- unknown character fallback warning through `ChatResponse.warnings`;
- service failure can be mapped by `map_exception(...)`;
- service app log does not leak token, api-key, authorization, or cookie values.

Updated `tests/test_cli_errors.py` so the CLI regression test now mocks `build_agent_service(...)`
instead of the old direct controller builder.

Validation:

```text
pytest: 115 passed
ruff check .: All checks passed
```

Targeted validation during implementation:

```text
pytest tests/test_agent_service.py tests/test_cli_errors.py: 6 passed
```

## Documentation Updates

Updated `docs/architecture.md`:

- added `AgentService` to the high-level runtime flow;
- added an Application Service Layer section;
- documented `build_agent_service()` and `AgentService.handle_chat()`;
- clarified service-level app logging summary boundaries.

Updated `docs/interfaces.md`:

- added Service Layer interfaces;
- documented `UserContext`, `ChatRequest`, `ChatResponse`, and `AgentService`;
- documented success/error behavior and app-log constraints.

Updated `docs/task_log.md`:

- moved `T-0125 Agent Service Layer v0.1` to `review`;
- recorded implementation summary;
- recorded CLI migration scope;
- recorded `SessionService` deferral;
- recorded validation coverage.

## Compatibility Notes

- Existing `build_controller(...)` remains available.
- Existing direct controller tests remain compatible.
- `AgentTurnInput` and `AgentTurnOutput` are unchanged.
- `DialogueController` continues to re-raise original exceptions after trace/prompt-log handling.
- CLI user-visible normal chat behavior is intended to remain unchanged.
- Session and memory commands remain on the previous path.
- No database schema change was introduced.
- No FastAPI dependency was introduced.

## Manager Review Checklist

Please review:

- whether `AgentService` remains thin enough and does not absorb controller/module policy;
- whether `ChatResponse.character_id` as requested target character is acceptable for v0.1;
- whether the app-log summary fields are sufficiently minimal;
- whether keeping `build_controller(...)` and adding `build_agent_service(...)` is the preferred
  transition shape;
- whether CLI normal chat migration is acceptable while session/memory commands remain unchanged;
- whether Service Layer docs should mention any additional FastAPI/Auth constraints before the
  next worker starts.

## Known Risks And Follow-ups

- `SessionService` is intentionally deferred. Future API session endpoints may need it once
  session command semantics move out of CLI.
- Auth and owner-only permissions are intentionally deferred. `UserContext.roles` is currently
  passive metadata.
- `ChatResponse.character_id` does not expose the active fallback character. Current fallback is
  available through warnings; a future contract can add `active_character_id` if API/Web needs it.
- Service app logs are summary-only. If future operations need richer diagnostics, add reviewed
  fields rather than logging raw messages or prompts.
- FastAPI should be built on `AgentService`, not on `DialogueController`.

## Manager Acceptance: 2026-07-06

Accepted as Agent Service Layer v0.1.

Manager review result:

- No blocking issues found.
- `AgentService` remains thin and does not absorb controller/module policy.
- `ChatResponse.character_id` as the requested target character is acceptable for v0.1.
- `build_agent_service(...)` alongside existing `build_controller(...)` is accepted as the
  transition shape.
- CLI normal chat migration is accepted; session and memory commands intentionally remain on the
  existing path.
- `SessionService`, Auth, permissions, FastAPI, Web, Streaming, and Audio remain deferred.

Manager-side verification:

```text
pytest: 115 passed
ruff check .: All checks passed
python -m compileall -q app tests: passed
git diff --check: no whitespace errors, CRLF warnings only
```
