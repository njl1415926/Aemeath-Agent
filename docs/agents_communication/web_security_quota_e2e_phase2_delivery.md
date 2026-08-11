# T-0146 Phase 2 Delivery - Web Security, Quota And E2E Gate v0.1

Status: Ready for Manager checkpoint

## Scope Completed

- Integrated `AgentStreamService` with Phase 1 `StreamQuotaTurnPersistencePort` behind the explicit runtime quota switch.
- Added quota runtime configuration for enablement, daily turn limit, required qualified token limit, retry-after seconds, and estimator version.
- Wired API startup orphan recovery to drain all `recover_orphaned_with_quota()` batches when quota is enabled.
- Added quota-enabled startup cleanup for legacy in-progress stream records that have no quota reservation.
- Preserved legacy stream persistence path when quota is disabled.
- Added reserve-time 429 path with `USAGE_QUOTA_EXCEEDED` and `Retry-After`.
- Persisted provider-started quota adjustment after `turn_engine.prepare()` and before `model_client.open_stream()`.
- Routed complete/fail/cancel/disconnect terminalization through quota UoW when enabled.
- Added production Web profile backend guard for exact synchronous `POST /v1/chat`.
- Added safe quota lifecycle logs for rejected, reserved, provider-started, settled, released, and recovered-orphaned states.
- Added D4-safe quota environment template entries in `deploy/prod.env.example`; quota remains disabled and token limit remains blank until estimator qualification.

## Contract Notes

- Quota is off by default and requires `AEMEATH_USAGE_QUOTA_ENABLED=true` plus a positive `AEMEATH_USAGE_QUOTA_DAILY_TOKEN_LIMIT`.
- `quota_day` remains UTC and is assigned at reserve time.
- Reserve-time quota exhaustion does not create stream idempotency rows, user messages, or quota reservations.
- Prompt adjustment quota rejection happens before opening the provider stream and releases the reservation.
- Provider-started terminal failures settle; pre-provider terminal failures release, using the Phase 1 UoW phase rules.
- Sync `/v1/chat` remains compatible for dev, CLI, and API-only profiles; prod + `web_session` rejects in FastAPI once that profile is allowed.
- Current D4 production validation still requires `dev_bearer`, so prod + `web_session` is not runnable yet. Switching production to Web Session is a Phase 3/D8 gate.

## Validation

- `pytest -q tests/test_agent_stream_service.py tests/test_api_streaming.py tests/store_contract/test_usage_quota_contract.py tests/test_usage_quota.py tests/test_usage_quota_migrations.py tests/test_streaming_chat_migrations.py tests/test_runtime_config.py -p no:cacheprovider --basetemp .pytest_tmp_phase2_c`
  - 111 passed
- `ruff check app/services/agent_stream_service.py app/main.py app/config.py app/api/routes/chat.py app/db/stream_quota.py tests/test_agent_stream_service.py tests/test_api_streaming.py tests/test_runtime_config.py`
  - passed
- `python -m compileall -q app tests`
  - passed

Additional checkpoint repair validation:

- `pytest -q tests/test_agent_stream_service.py tests/test_api_streaming.py tests/store_contract/test_usage_quota_contract.py tests/test_usage_quota.py tests/test_usage_quota_migrations.py tests/test_streaming_chat_migrations.py tests/test_runtime_config.py tests/deployment/test_compose_contract.py::test_production_env_template_is_tracked_release_asset -p no:cacheprovider --basetemp .pytest_tmp_phase2_fix_d`
  - 120 passed
- `pytest -q -p no:cacheprovider --basetemp .pytest_tmp_phase2_fix_full`
  - passed
- `ruff check .`
  - passed

## Not In Phase 2

- No frontend changes.
- No Caddy, Dockerfile, Compose, or Playwright changes.
- No `AgentStreamService` quota behavior for synchronous `/v1/chat`.
- No production Caddy exact-path sync rejection yet.
- No production Web Profile switch yet; config still enforces the accepted D4 `dev_bearer` baseline.
- Safe admin usage query is deferred pending an approved admin HTTP/API surface and permission model. The underlying `UsageQuotaQueryPort.get_daily_usage()` remains available for that future surface.
- No task status change to done.

## Manager Checkpoint Focus

- Confirm provider-started ordering relative to `model_client.open_stream()`.
- Confirm 429 behavior and `Retry-After` contract for reserve-time exhaustion.
- Confirm safe logging field set excludes prompt, response, credentials, raw IP, and provider body.
- Confirm production Web sync API backend guard is sufficient for Phase 2 before Caddy work begins.
