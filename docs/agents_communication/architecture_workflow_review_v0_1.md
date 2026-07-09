# Architecture And Workflow Review v0.1

Date: 2026-07-08

Scope: Review only. No production code changes were made in this session.

Validation:

- `pytest`: 161 passed, 19 warnings.
- `ruff check .`: All checks passed.
- `python -m compileall -q app tests`: passed.

## 1. Executive Summary

Overall result: Passed with follow-up fixes recommended.

The core architecture is still healthy. `DialogueController` remains an orchestration layer, FastAPI chat enters through `AgentService`, auth stays at adapter/service boundaries, persistence is still behind store ports, and Character / RAG / Memory / State / Model are injected as ports or runtime dependencies rather than being hard-coded into HTTP/API paths.

The largest current risk is not a boundary collapse. It is operational privacy and auth behavior now that the same runtime is used by FastAPI:

- Prompt-run logging is enabled by default and records full prompt messages, sections, and response snapshots. That was acceptable as a local CLI debug default, but it becomes risky for an HTTP service.
- Dev-token hashes are persistent. If a token is configured once and later removed from env, the old database hash is not cleared, so the old token can remain valid.

Recommendation before Streaming API: fix the High findings first. Streaming increases the number of externally visible runtime paths, so auth revocation, prompt-log defaults, and error/log behavior should be tightened before adding it.

Recommendation before Context Compression: context compression can proceed if scoped to `ContextBuilder` / prompt policy, but it should also account for prompt-log privacy and prompt budget observability. It should not require major controller or API restructuring.

## 2. Findings

### Critical

None.

### High

#### H-1 Prompt-run logging is enabled by default for the shared CLI/FastAPI runtime

Issue:

`AEMEATH_ENABLE_PROMPT_RUN_LOG` defaults to true and `app/main.py` wires `JsonlPromptRunLogger` whenever it is enabled. `DialogueController` records compiled prompt messages, prompt sections, prompt metadata, and the model response. `JsonlPromptRunLogger` writes the record as-is. This is intentional for local debugging, but FastAPI now uses the same runtime, so API request content can be persisted by default under `.aemeath/sessions`.

Impact:

This can store user messages, prompt context, RAG evidence, memory snippets, and assistant responses for API traffic without an explicit API logging opt-in. Authorization headers and common token-shaped fragments are redacted before controller prompt records, but arbitrary user-provided secrets or private text inside normal prompt content still remain in the prompt log by design.

Evidence:

- `app/config.py:28` sets `enable_prompt_run_log: bool = True`.
- `app/config.py:78` to `app/config.py:80` reads `AEMEATH_ENABLE_PROMPT_RUN_LOG` with default `"true"`.
- `app/main.py:87` to `app/main.py:90` wires `JsonlPromptRunLogger` when enabled.
- `app/agent/dialogue_controller.py:125` to `app/agent/dialogue_controller.py:128` records full `messages`, `sections`, metadata, and `response`.
- `app/observability/prompt_run_logger.py:23` to `app/observability/prompt_run_logger.py:27` writes `asdict(record)` directly.
- `docs/observability.md:58` to `docs/observability.md:60` documents that prompt logs intentionally capture prompt messages, sections, metadata, response text, warnings, and failed-run summaries.
- `tests/conftest.py:25` disables prompt-run logging in the common test fixture, so API tests do not exercise the default production-like value.

Suggested fix:

Split CLI debug behavior from service behavior. Good options:

- Make prompt-run logging default false globally, and ask local CLI users to opt in.
- Or add an API/service-specific default such as `AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false`.
- Add a startup/runtime warning when FastAPI starts with prompt-run logging enabled.
- Add a FastAPI test that verifies the intended default and a test that confirms prompt logs are only created when explicitly enabled for API.
- Document prompt logs as sensitive local artifacts, not operational logs.

#### H-2 Removing `AEMEATH_DEV_AUTH_TOKEN` does not revoke a previously stored dev token

Issue:

Startup only writes a dev-token hash when `settings.dev_auth_token` is present. If the token was configured in a previous run, its hash remains in the `accounts` table. A later run with `AEMEATH_DEV_AUTH_TOKEN` unset does not clear the old hash, and `AuthService` still authenticates any bearer token whose hash matches a stored account hash.

Impact:

Operators may believe bearer auth is unavailable or rotated after removing the env var, while an old token continues to work against the same database. This is especially risky because `/v1/*` requires bearer auth by default and stale local tokens become hidden credentials.

Evidence:

- `app/config.py:118` reads `AEMEATH_DEV_AUTH_TOKEN` as `None` when unset.
- `app/main.py:129` to `app/main.py:134` only calls `set_dev_token_hash(...)` when `settings.dev_auth_token` is truthy.
- `app/auth/service.py:38` to `app/auth/service.py:44` authenticates by hashing the presented token and looking up any stored `dev_token_hash`.
- `app/db/sqlite_store.py:488` to `app/db/sqlite_store.py:503` looks up accounts by stored token hash.
- `app/db/repositories/accounts.py:45` to `app/db/repositories/accounts.py:57` does the same for SQLAlchemy.
- `app/db/sqlite_store.py:557` to `app/db/sqlite_store.py:570` and `app/db/repositories/accounts.py:96` to `app/db/repositories/accounts.py:109` already support setting the hash to `None`, but startup does not use that path when the env var is empty.

Suggested fix:

Treat the env var as the source of truth for dev-token auth in v0.1:

- On startup, clear `settings.dev_auth_user_id`'s `dev_token_hash` when `AEMEATH_DEV_AUTH_TOKEN` is unset.
- Add contract tests for both `sqlite_raw` and `sqlalchemy` proving that removing the env token revokes the old token.
- Consider enforcing unique non-null dev-token hashes or explicitly rejecting duplicate token assignment in `AccountService`, so token resolution cannot become ambiguous.
- Document the revocation behavior in `docs/auth_and_permissions.md` and README.

### Medium

#### M-1 API `request_id` does not flow into controller trace or prompt-run metadata

Issue:

FastAPI creates or accepts an `X-Request-ID`, and `AuthService` carries it in `UserContext`. `AgentService` uses it for app logs, but it does not merge `user_context.request_id` into `AgentTurnInput.metadata`. `TraceEvent` has no `request_id` field, and controller trace/prompt metadata only sees `turn_input.metadata`.

Impact:

App logs can correlate API request id to service-level chat events, but trace logs and prompt-run logs cannot reliably be joined back to the originating HTTP request unless the client also puts a request id inside the chat request metadata. This weakens debugging for API failures and later streaming workflows.

Evidence:

- `app/api/dependencies.py:64` to `app/api/dependencies.py:83` builds `UserContext` with `request_id`.
- `app/services/agent_service.py:36` to `app/services/agent_service.py:42` constructs `AgentTurnInput` with only `request.metadata`.
- `app/services/agent_service.py:111` records `user_context.request_id` in service app logs, so the value exists but stays at that layer.
- `app/observability/schemas.py:26` to `app/observability/schemas.py:32` defines `TraceEvent` without request id.
- `app/agent/dialogue_controller.py:184` to `app/agent/dialogue_controller.py:188` records turn metadata, but that metadata does not include the API request id by default.

Suggested fix:

Either add `request_id` to `AgentTurnInput` / `TraceEvent`, or have `AgentService` merge a sanitized `request_id` and selected `UserContext.metadata` into `AgentTurnInput.metadata`. Add a FastAPI test that enables trace logging and verifies `X-Request-ID` is present in trace or prompt metadata according to the chosen contract.

#### M-2 Memory worker failure path stores and prints raw exception text

Issue:

The memory worker catches broad exceptions, writes `str(exc)` into the memory job failure record, and prints the raw exception. This bypasses the shared error mapper/sanitizer used by CLI/API/app/trace paths.

Impact:

Provider failures, malformed model outputs, local filesystem errors, or unexpected exceptions could persist sensitive fragments in `memory_extraction_jobs.error` or emit them to stdout. This is inconsistent with the error/observability contract.

Evidence:

- `app/memory/worker.py:109` to `app/memory/worker.py:111` calls `fail_memory_extraction_job(job_id=job.id, error=str(exc))` and prints `exc`.
- `app/db/repositories/memory_jobs.py:183` to `app/db/repositories/memory_jobs.py:192` persists the supplied error string.
- `docs/observability.md:86` to `docs/observability.md:105` says future service/API/worker code should reuse the shared sanitizer instead of adding another sensitive-field list.
- `app/errors/mapper.py:24` to `app/errors/mapper.py:84` and `app/errors/base.py:26` to `app/errors/base.py:34` already provide mapped, sanitized log payloads.

Suggested fix:

Use `map_exception(exc).to_log_dict()` or at least `sanitize_text(str(exc))` before persisting or printing worker failures. Prefer structured app logs through `AppLoggerPort` for worker lifecycle/failure events. Add a worker regression test with a fake exception containing `token=...`, `api-key=...`, `Authorization: Bearer ...`, and `cookie=...`.

#### M-3 Model fallback masks provider errors for API/service traffic

Issue:

When not using the mock model, `build_controller()` always wraps `DeepSeekClient` with `FallbackChatModelClient`. The fallback catches `HTTPError`, `URLError`, `KeyError`, and `JSONDecodeError` and returns a mock fallback response instead of raising. That means API callers may receive HTTP 200 and a fallback roleplay response even when the provider has auth, rate-limit, timeout, or invalid-response problems.

Impact:

This is friendly for local CLI continuity, but it hides operational incidents from API clients and prevents the existing `MODEL_AUTH_FAILED`, `MODEL_RATE_LIMITED`, `MODEL_TIMEOUT`, `MODEL_UNAVAILABLE`, and `MODEL_INVALID_RESPONSE` mappings from reaching HTTP responses. Streaming will make this more visible because stream clients need a clear distinction between generated output and degraded fallback.

Evidence:

- `app/main.py:43` to `app/main.py:56` wraps real DeepSeek calls in `FallbackChatModelClient`.
- `app/model/deepseek.py:110` to `app/model/deepseek.py:117` catches provider/runtime errors and returns fallback text.
- `docs/error_handling.md:198` notes that `MODEL_AUTH_FAILED` should not be fully masked by fallback in future work.
- `tests/test_errors.py:53` to `tests/test_errors.py:78` covers model error mapping, but the default runtime fallback path prevents those errors from surfacing through API chat.

Suggested fix:

Make fallback policy adapter/config aware:

- CLI can keep graceful fallback by default.
- API should either disable fallback by default or expose a structured degraded response with status/warning semantics agreed in the service contract.
- Add API/service tests for provider auth/rate-limit/unavailable paths with fallback disabled or explicitly marked.

### Low

#### L-1 `PermissionService` is defined but not wired into application code

Issue:

`PermissionService` exists and is tested, but app code currently relies on session store ownership checks and memory owner-scoped store methods rather than using the generic permission helper.

Impact:

This is not a current cross-user access bug. The risk is future duplication: new non-session resources may invent another owner check instead of using `PermissionService`, or reviewers may assume it is already active in API/session paths.

Evidence:

- `app/permissions/service.py:7` to `app/permissions/service.py:15` defines `PermissionService.require_owner(...)`.
- Search usage shows it is referenced in docs/tests, but not in `app/api`, `app/services`, `app/memory`, or `app/session`.
- Existing protections are elsewhere: `app/services/session_service.py:65` to `app/services/session_service.py:79` validates session ownership/status before message reads, and memory archive/disable methods are owner-scoped.

Suggested fix:

Document it as an explicit future helper, or wire it into `SessionService` / future non-session resources when the first real generic permission boundary appears. Avoid adding parallel permission helpers.

#### L-2 Documentation has several stale or internally contradictory statements

Issue:

The high-level docs mostly describe the current architecture, but there are enough stale statements to confuse a new session.

Impact:

Workers may follow older "future API", "EmptyEvidenceProvider default", "Character v0.1 only", or old test-count guidance and make wrong assumptions about current runtime state.

Evidence:

- `docs/auth_and_permissions.md:8` to `docs/auth_and_permissions.md:9` says FastAPI is not implemented, and `docs/auth_and_permissions.md:95` to `docs/auth_and_permissions.md:108` lists FastAPI as deferred, while `docs/fastapi_service.md:1` to `docs/fastapi_service.md:10` documents the implemented API adapter.
- `docs/architecture.md:255` says `EmptyEvidenceProvider` is the default, while `docs/architecture.md:261` to `docs/architecture.md:264` and `app/main.py:108` say `WorldInfoEvidenceProvider` is active.
- `docs/interfaces.md:534` to `docs/interfaces.md:538` lists only `EmptyEvidenceProvider` and `CompositeEvidenceProvider`, while `docs/interfaces.md:1051` to `docs/interfaces.md:1068` says `WorldInfoEvidenceProvider` is active.
- `docs/architecture.md:640` says `CharacterProvider` still uses v0.1 YAML as its only source, while `app/config.py:70` defaults to `app/character/Aemeath.v0.2` and `app/character/provider.py:308` to `app/character/provider.py:331` supports v0.2 manifests.
- `README.md:207` to `README.md:212` still says current validation is 95 passed, while this review run passed 161 tests.
- `docs/task_log.md:322` to `docs/task_log.md:346` still lists Character Manifest as todo even though later tasks and code show v0.2 manifest is active.

Suggested fix:

Do a focused docs refresh after the High fixes. Keep historical task entries if desired, but mark superseded backlog items clearly and update current-state docs first: README, architecture, interfaces, auth_and_permissions, and task_log current status.

## 3. Architecture Health

Controller:

Healthy. `DialogueController` still orchestrates a turn and depends on ports for Character, Memory, Evidence, Context, Response, State, Session, Message, Trace, and Prompt Log. It does not import FastAPI, AuthService, AccountService, concrete DB backend, or API schemas. It does perform trace/prompt-log recording, which is consistent with current docs.

Service:

Healthy. `AgentService` is thin: it converts `UserContext` and `ChatRequest` into `AgentTurnInput`, delegates to the controller, maps output DTOs, and writes summary app logs. It does not absorb Character, Memory, Prompt, RAG, Model, or State strategy.

`SessionService` is also thin and adapter-facing. It wraps session creation/listing/message reads and validates ownership/status before message listing.

API:

Mostly healthy. FastAPI chat uses `AgentService.handle_chat(...)` and does not import `DialogueController`. `/v1/*` auth is centralized in dependencies. Error responses use `map_exception(...)`, `ErrorInfo.to_public_dict()`, and `http_status_for_error(...)`. App logs are summary-only. Main gaps are prompt-log default behavior and request-id correlation into trace/prompt layers.

CLI:

Healthy for normal chat. CLI normal turns use `AgentService`. Session and memory commands still operate on store/admin helpers directly, which matches the accepted service-layer transition notes.

Auth / Account / Permission:

Boundary is healthy, but dev-token lifecycle needs tightening. `UserContext` is the identity handoff to service/controller. Auth does not enter controller. PermissionService is currently a future helper rather than an active application dependency.

Session:

Healthy. Session ownership, lifecycle, and message read model remain behind `SessionStorePort`. Controller calls `ensure_session` before writing messages. API message listing validates ownership before reading messages.

Memory:

Healthy boundary. Dialogue uses `MemoryReaderPort`, `MemoryWriterPort`, and `MemoryWriteClassifierPort`. Scope/provenance are aligned around user, character, and session. Memory worker uses the same store factory, but worker error sanitization needs a small fix.

Persistence:

Healthy for v0.1. `sqlite_raw` remains default. SQLAlchemy is opt-in and implemented as a facade over repositories. Store contract tests cover both backends across sessions, messages, accounts, relationships, memories, jobs, and embeddings.

Observability:

Boundary concepts are good: App Log, Trace Log, and Prompt Log are separate. App/trace logs sanitize payloads. Prompt logs are intentionally full-fidelity debug snapshots, but that default should be revisited for FastAPI.

Prompt / Character / RAG:

Healthy. `ContextBuilder` remains the prompt integration point. Character does not call RAG. RAG enters through `EvidenceProviderPort`. Context compression has a clear entry point around `PromptContext` / `PromptCompiler`, but token budget and compression policy are still backlog.

## 4. Test Coverage Review

Strong existing coverage:

- API smoke/auth/session/error/redaction tests in `tests/test_api.py`.
- Agent service DTO/error/app-log tests in `tests/test_agent_service.py`.
- Error mapping and sanitizer tests in `tests/test_errors.py`.
- App log and trace log sanitizer tests in `tests/test_observability_logging.py`.
- Store contract tests parameterized for `sqlite_raw` and `sqlalchemy`.
- Session lifecycle/side-effect tests, including ended/archived rejection before message or memory side effects.
- Memory scope/provenance, extraction jobs, embeddings, owner-scoped archive/disable, and SQLAlchemy worker smoke tests.
- Character manifest, registry, RAG provider, and World Info integration tests.

Recommended added tests:

- API default prompt-log behavior: prove prompt logs are disabled by default for API, or prove they require explicit opt-in.
- Dev-token revocation: configure token once, restart with token unset against the same DB, verify the old token is rejected for both backends.
- Request-id correlation: enable trace logging through FastAPI and verify `X-Request-ID` reaches the chosen trace/prompt/app-log correlation field.
- Memory worker failure redaction: assert job error and stdout/app log do not contain token/API-key/cookie fragments.
- API model-provider error behavior: test provider auth/rate-limit/unavailable paths under the intended fallback policy.
- API session cross-character message listing: verify wrong `character_id` query receives `SESSION_OWNERSHIP_DENIED`.
- SQLAlchemy operational exception mapping: if API uses SQLAlchemy backend, ensure DB operational failures map to storage errors rather than generic internal errors.

## 5. Docs Drift Review

Primary drift:

- Auth docs still mark FastAPI as deferred even though FastAPI v0.1 is implemented.
- Architecture/interfaces docs contain old `EmptyEvidenceProvider` default statements alongside newer `WorldInfoEvidenceProvider` active statements.
- Architecture technical debt still says CharacterProvider is v0.1 YAML-only, but v0.2 manifest is active.
- README validation count is stale.
- Task log backlog has old Character Manifest todo entries that are superseded by later completed v0.2 work.
- Some older planning docs still say there is no FastAPI server. Those can remain as historical docs if clearly labeled, but current-state docs should not send workers down old paths.

Suggested docs refresh order:

1. README current validation count and API/logging caveats.
2. `docs/auth_and_permissions.md` FastAPI status and dev-token revocation behavior.
3. `docs/architecture.md` current provider/default statements and technical debt cleanup.
4. `docs/interfaces.md` current evidence provider list and World Info status.
5. `docs/task_log.md` mark superseded backlog items or add a current-state note.

## 6. Recommended Next Actions

Immediate:

1. Change or split prompt-run logging defaults for FastAPI/API usage.
2. Make dev-token revocation explicit when `AEMEATH_DEV_AUTH_TOKEN` is unset, with cross-backend tests.
3. Sanitize memory worker failure persistence/stdout.
4. Propagate API request id into trace or turn metadata.
5. Refresh current-state docs after the behavior decisions above.

Backlog:

1. Decide CLI vs API model fallback policy and add tests.
2. Clarify whether `PermissionService` should stay future-only or become the standard owner helper.
3. Add token budget/context compression around `PromptContext` and `PromptCompiler`.
4. Continue SQLAlchemy backend hardening before any default-backend change.
5. Keep Alembic/PostgreSQL as separate reviewed persistence work.

Streaming / Context Compression recommendation:

- Streaming API should wait until the High findings are fixed.
- Context Compression can start in parallel if it stays behind `ContextBuilder` / `PromptContext`, but it should include prompt-log privacy and request-id observability in its acceptance criteria.

## 7. Manager Follow-up

Date: 2026-07-09

Status: High findings and small medium follow-ups addressed.

Implemented:

- Added `AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false`; FastAPI now controls prompt-run logging with the
  API-specific flag while CLI keeps the historical local-debug default.
- Startup clears the configured dev user's `dev_token_hash` when `AEMEATH_DEV_AUTH_TOKEN` is unset,
  revoking the previous dev token for that database/account.
- `AgentService` copies `UserContext.request_id` into `AgentTurnInput.metadata` so trace and prompt
  metadata can correlate with API request ids.
- Memory worker failure handling persists and prints sanitized mapped errors instead of raw
  exception text.
- FastAPI runtime disables model fallback by default through
  `AEMEATH_API_ENABLE_MODEL_FALLBACK=false`, while CLI fallback remains enabled by default.
- Added regression tests for API prompt-log defaults/opt-in, dev-token revocation across both
  store backends, API request-id trace correlation, memory worker failure redaction, API provider
  error no-fallback behavior, and `.env` isolation for the store-factory default test.

Deferred:

- API/model fallback policy is resolved for v0.1: CLI fallback remains enabled by default, while
  FastAPI fallback defaults to disabled through `AEMEATH_API_ENABLE_MODEL_FALLBACK=false`.
- `PermissionService` remains future-only until a generic non-session permission boundary needs it.
- Full docs drift cleanup remains a follow-up docs task.

Validation:

- `pytest -q tests/test_api.py`: 16 passed.
- `pytest -q tests/test_api.py tests/test_auth.py tests/test_memory_worker.py`: 26 passed.
- `pytest`: 168 passed.
- `ruff check .`: All checks passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: no whitespace errors, CRLF warnings only.
