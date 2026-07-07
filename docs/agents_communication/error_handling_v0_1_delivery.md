# Worker Session 6 Delivery: Error Handling v0.1

Date: 2026-07-06

Task: Error Handling Worker Session initial implementation

Status: implementation complete, ready for Manager review

## Summary

Implemented the first-stage unified error handling foundation for Aemeath Agent.

The goal of this stage is not to rewrite every business exception. Instead, it introduces a
stable error contract that can be reused by CLI, future API handlers, Trace, and Prompt Log while
preserving existing runtime behavior.

Delivered:

- new `app/errors/` package;
- stable `ErrorCode` enum;
- `ErrorInfo` public/log error object;
- `AemeathError` base class for future typed errors;
- non-invasive exception mapper for current Session, Memory, Model, Config, Character, and Storage
  failures;
- shared sensitive-data sanitizer;
- HTTP status mapping helper without FastAPI dependency;
- Controller trace / prompt-run failure logging with stable `error_code`;
- CLI user-facing error messages with stable code suffixes;
- prompt log inspector support for error code and public message;
- DeepSeek transient-request retry with 3 retries;
- Chinese design document for the error system.

## Added Files

- `app/errors/__init__.py`
- `app/errors/base.py`
- `app/errors/codes.py`
- `app/errors/http.py`
- `app/errors/mapper.py`
- `app/errors/sanitizer.py`
- `docs/error_handling.md`
- `docs/agents_communication/error_handling_v0_1_delivery.md`
- `tests/test_errors.py`

## Modified Files

- `app/agent/dialogue_controller.py`
- `app/cli.py`
- `app/model/deepseek.py`
- `app/observability/prompt_log_inspector.py`
- `app/observability/schemas.py`
- `tests/test_deepseek_client.py`

No business module was forced to inherit from `AemeathError` in this stage.

## Public Interfaces

### ErrorCode

`app/errors/codes.py` defines stable codes:

```text
INTERNAL_ERROR
INVALID_INPUT
CONFIG_INVALID
CONFIG_MISSING_SECRET
SESSION_NOT_FOUND
SESSION_OWNERSHIP_DENIED
SESSION_ENDED
SESSION_ARCHIVED
SESSION_INVALID_TRANSITION
SESSION_MIGRATION_FAILED
CHARACTER_NOT_FOUND
CHARACTER_LOAD_FAILED
CHARACTER_INVALID_MANIFEST
MEMORY_INVALID_SOURCE
MEMORY_EXTRACTION_FAILED
MEMORY_INVALID_MODEL_OUTPUT
MODEL_BAD_REQUEST
MODEL_AUTH_FAILED
MODEL_RATE_LIMITED
MODEL_TIMEOUT
MODEL_UNAVAILABLE
MODEL_INVALID_RESPONSE
STORAGE_UNAVAILABLE
STORAGE_MIGRATION_FAILED
STORAGE_CONSTRAINT_VIOLATION
```

### ErrorInfo

`ErrorInfo` is the standard boundary object:

```python
ErrorInfo(
    code=ErrorCode.MODEL_TIMEOUT,
    public_message="The model request timed out. Please try again.",
    internal_message="timed out",
    error_type="URLError",
    retryable=True,
    metadata={},
)
```

Boundary helpers:

- `to_public_dict()` for future API responses;
- `to_log_dict()` for Trace and Prompt Log.

### map_exception

`map_exception(exc)` converts existing exceptions to `ErrorInfo` without requiring large business
refactors.

Currently covered:

- `AemeathError`
- urllib `HTTPError` / `URLError`
- `TimeoutError` / `socket.timeout`
- `json.JSONDecodeError`
- `SessionError` subclasses
- `MemoryExtractionError`
- `sqlite3.Error` subclasses
- `FileNotFoundError`
- `KeyError`
- `ValueError`
- fallback unknown exceptions

### http_status_for_error

`http_status_for_error(error_info)` provides future HTTP mapping without importing FastAPI.

Examples:

- `SESSION_NOT_FOUND -> 404`
- `SESSION_OWNERSHIP_DENIED -> 403`
- `SESSION_ENDED -> 409`
- `MODEL_RATE_LIMITED -> 429`
- `MODEL_TIMEOUT -> 504`
- `MODEL_UNAVAILABLE -> 503`

## Runtime Behavior

### DialogueController

`DialogueController` still only orchestrates the turn. It does not classify errors itself.

Changes:

- existing trace payload sanitization now uses `app.errors.redact_sensitive`;
- `session_validation_failed` trace records `ErrorInfo.to_log_dict()`;
- `turn_failed` trace records a structured `error` object;
- failed prompt-run records include:
  - `error_code`
  - `error_type`
  - `error_public_message`
  - sanitized `error`

The Controller still re-raises the original exception after logging. CLI or future API layers
remain responsible for final presentation.

### CLI

CLI behavior remains local and non-HTTP. It now maps caught exceptions to safe public messages and
prints the stable code:

```text
[session] command failed: Session has ended. Resume it before sending messages. [SESSION_ENDED]
[memory] command failed: Memory extraction returned an invalid model response. [MEMORY_INVALID_MODEL_OUTPUT]
Aemeath> 当前处理失败：The model request timed out. Please try again. [MODEL_TIMEOUT]
```

This keeps the original command flow but avoids exposing raw exception text.

### Prompt Log Inspector

Failed prompt-run inspection now shows:

- `Error code`
- `Error type`
- `User message`
- `Error`

The inspector remains backward-compatible with old prompt logs that do not contain the new fields.

## Model Retry Behavior

`DeepSeekClient` now retries transient failures.

Defaults:

- `max_retries=3`
- `retry_base_delay_seconds=0.5`
- total attempts on repeated failure: 1 initial request + 3 retries
- backoff: `0.5s -> 1s -> 2s`

Retryable:

- `TimeoutError`
- `URLError`
- HTTP `408`
- HTTP `429`
- HTTP `500`
- HTTP `502`
- HTTP `503`
- HTTP `504`

Not retried:

- HTTP `400`
- HTTP `401`
- HTTP `403`
- model response shape errors

Existing fallback behavior remains in `FallbackChatModelClient`.

## Sensitive Data Handling

`app/errors/sanitizer.py` redacts sensitive keys and common sensitive text patterns.

Sensitive key fragments:

```text
api_key
apikey
authorization
cookie
password
secret
token
```

The sanitizer is used by:

- `ErrorInfo.to_log_dict()`;
- `DialogueController._safe_trace_payload()`;
- prompt-run error recording through mapped errors.

## Documentation

Added Chinese design documentation:

- `docs/error_handling.md`

It explains:

- design goals;
- module structure;
- all error-code meanings;
- CLI / Controller / Prompt Log integration;
- HTTP mapping;
- special handling rules;
- model retry behavior;
- process for adding new errors;
- sensitive-data constraints.

## Tests

Added `tests/test_errors.py` covering:

- Session error code mapping;
- HTTP status mapping;
- model HTTP error retryability mapping;
- model timeout mapping;
- memory extraction source/output error mapping;
- nested sensitive-data redaction.

Extended `tests/test_deepseek_client.py` covering:

- retrying transient `URLError` three times;
- retrying HTTP `503`;
- not retrying HTTP `401`.

Validation run:

```text
pytest: 103 passed
ruff check app tests: All checks passed
```

`ruff check .` is currently blocked by unrelated third-party vendor code under
`Aemeath_roleplay_skill/audio/vendor/...`. That directory is not part of this Error Handling
delivery.

## Compatibility Notes

- Existing `SessionError` subclasses are preserved.
- Existing `MemoryExtractionError` is preserved.
- `AgentTurnInput` and `AgentTurnOutput` are unchanged.
- No FastAPI dependency was introduced.
- No database schema change was introduced.
- CLI command names and control flow remain unchanged.
- Prompt-run records gained optional fields, so old records remain readable.

## Known Risks And Follow-ups

- `FallbackChatModelClient` still catches broad model failures and may hide auth/config problems
  behind fallback output. A future Model Error Taxonomy task should make fallback decisions based
  on stable model error codes.
- DeepSeek retry count and `fallback_used` are not yet recorded as structured trace fields.
  This should be revisited after a `ModelRequest` / `ModelResponse` contract exists.
- Mapper-based classification relies on current exception types and message fragments for some
  legacy `ValueError` paths. Future module-specific exceptions should inherit `AemeathError` or
  provide explicit metadata.
- API response shape is documented and supported by `to_public_dict()`, but no API layer exists yet.
- Storage errors are mapped conservatively. More precise SQLite-to-domain classification can be
  added when storage adapters mature.
- Documentation in `docs/architecture.md`, `docs/interfaces.md`, and `docs/task_log.md` may need
  Manager synchronization if this delivery is accepted as an official project task.

## Manager Review Response: 2026-07-06

Applied the requested small follow-up patch without expanding scope.

Changes:

- Updated `app/errors/sanitizer.py` to redact hyphenated sensitive keys:
  - `api-key`
  - `x-api-key`
  - `x_api_key`
- Kept ordinary metadata keys unredacted to avoid over-sanitizing unrelated fields such as
  `ordinary-key`.
- Extended text sanitization to redact assignments such as `x-api-key=secret`.
- Added sanitizer regression coverage in `tests/test_errors.py`.
- Added Controller failed prompt-run regression coverage in `tests/test_prompt_run_logger.py`.
  The test fails after prompt compilation through a fake response generator and verifies failed
  JSONL records contain:
  - `error_code`
  - `error_type`
  - `error_public_message`
  - `error`
- Verified failed prompt log output does not contain sensitive values from either exception text
  or request metadata.
- Added CLI regression coverage in `tests/test_cli_errors.py`, confirming ordinary turn failures
  print a stable error code such as `[SESSION_ENDED]` and do not leak sensitive exception text.

Validation after review patch:

```text
pytest tests/test_errors.py tests/test_prompt_run_logger.py tests/test_cli_errors.py: 10 passed
ruff check app tests: All checks passed
```

Full validation requested by Manager is listed below in the final validation section.

Final validation after review patch:

```text
pytest: 105 passed
ruff check .: All checks passed
```

## Manager Acceptance: 2026-07-06

Accepted as Error Handling v0.1 after the follow-up patch.

Manager-side verification:

```text
pytest: 105 passed
ruff check .: All checks passed
python -m compileall -q app: passed
git diff --check: no whitespace errors, CRLF warnings only
```

Project documentation synchronized:

- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/error_handling.md`
- `docs/task_log.md`
- `docs/codex_development_sequence_plan.md`
- `docs/codex_development_sequence_plan_cn.md`
