# Aemeath Agent Observability

This document defines the v0.1 logging boundary for Aemeath Agent.

## Log Types

```text
App Log      process/runtime/application boundary events
Trace Log    one agent turn's internal structured events
Prompt Log   prompt/messages/sections/response snapshot for prompt debugging
```

### App Log

App logs describe process and boundary events, such as CLI startup, future API request handling,
worker lifecycle, configuration status, and operational failures.

Current v0.1 contract:

- `AppLogRecord`
- `AppLoggerPort.record(record)`
- `JsonlAppLogger`
- `NoopAppLogger`

App logs should contain IDs, status values, error codes, durations, counts, and sanitized payload
metadata. They should not contain full prompts, full model messages, raw provider responses, API
keys, authorization headers, cookies, or tokens.

### Trace Log

Trace logs describe one agent turn's internal structured events. `DialogueController` emits
`TraceEvent` records such as:

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

Current v0.1 persistence:

- `JsonlTraceLogger` implements `TraceLoggerPort`.
- Records are written as UTF-8 JSONL with `ensure_ascii=False`.
- Default root: `.aemeath/traces`.
- Trace logging is disabled by default.

Trace logs should stay compact and structured. They may include prompt section ids, source ids,
memory ids, counts, statuses, warning lists, and stable error information. They should not store
full prompt messages or full provider responses by default.

### Prompt Log

Prompt logs are local debugging and evaluation artifacts. They intentionally capture prompt
messages, structured prompt sections, context metadata, response text, warnings, and failed-run
error summaries.

Current v0.1 persistence:

- `PromptRunRecord`
- `PromptRunLoggerPort.record(record)`
- `JsonlPromptRunLogger`
- `NoopPromptRunLogger`
- Default root: `.aemeath/sessions`.

Prompt logs are separate from trace logs. Do not merge prompt-run records into trace logs.

## Configuration

```env
AEMEATH_ENABLE_TRACE_LOG=false
AEMEATH_TRACE_LOG_ROOT=.aemeath/traces
AEMEATH_ENABLE_APP_LOG=false
AEMEATH_APP_LOG_ROOT=.aemeath/app_logs
AEMEATH_ENABLE_PROMPT_RUN_LOG=true
AEMEATH_PROMPT_RUN_LOG_ROOT=.aemeath/sessions
```

Trace and app logs are disabled by default to keep local CLI runs quiet. Prompt-run logging keeps
its existing behavior.

## Redaction

Trace and app JSONL loggers call `app.errors.redact_sensitive(...)` before writing payloads.
`DialogueController` also sanitizes trace payloads before emitting events.

Sensitive keys and text fragments covered by the shared sanitizer include:

- `api_key`
- `apikey`
- `api-key`
- `x-api-key`
- `x_api_key`
- `authorization`
- `cookie`
- `password`
- `secret`
- `token`

Future service/API/worker code should reuse the same sanitizer instead of adding a second
observability-specific sensitive-field list.

## Runtime Wiring

`app/main.py` wires `JsonlTraceLogger` when `AEMEATH_ENABLE_TRACE_LOG=true`; otherwise it uses
`NoopTraceLogger`.

The app logger contract exists for process and boundary logs, but v0.1 does not inject app logging
into `DialogueController`. Future CLI, service, API, and worker operation work can use
`AppLoggerPort` at those boundaries.
