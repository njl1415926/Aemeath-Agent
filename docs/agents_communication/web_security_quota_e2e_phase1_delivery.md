# Web Security, Quota, And E2E Gate v0.1 Phase 1 Delivery

Status: Submitted for Manager checkpoint.

Task: T-0146

Date: 2026-08-10

Owner: Web Security / Quota / E2E Worker

## Scope Delivered

- Added quota DTOs, `UsageQuotaPolicyPort`, `UsageQuotaQueryPort`, and
  `StreamQuotaTurnPersistencePort`.
- Added `prompt_utf8_v0_1` deterministic estimator and qualification tests.
- Added `USAGE_QUOTA_EXCEEDED` with HTTP `429` mapping.
- Added `usage_quota_v0_1` migration with strict schema validation for tables, columns, defaults,
  checks, indexes, foreign keys, and marker presence.
- Added a stream+quota SQLite UoW for atomic reserve, provider-started adjustment, complete, fail,
  and orphan recovery.
- Exposed the same Phase 1 UoW/query methods through `SQLiteStore` and `SqlAlchemyStore`.
- Added raw SQLite and SQLAlchemy-on-SQLite parity tests for reserve/replay/conflict, quota
  rejection, provider-started adjustment, settle/release, cross-midnight quota day, policy freeze,
  concurrency, and crash recovery.

## Boundaries Preserved

- Did not integrate quota into `AgentStreamService`.
- Did not modify frontend, Caddy, Compose, Docker, or Playwright.
- Did not read or modify real `.env`, secrets, or `data/aemeath.db`.
- Did not modify `docs/task_log.md` status.
- Did not modify DialogueController, Character, Memory, RAG, prompt strategy, or model strategy.

## Interface Notes

- Quota writes are only exposed through `StreamQuotaTurnPersistencePort` methods:
  `resolve_or_reserve_with_quota`, `mark_provider_started_with_quota`, `complete_with_quota`,
  `fail_with_quota`, and `recover_orphaned_with_quota`.
- `UsageQuotaPolicyPort` is calculation-only.
- `UsageQuotaQueryPort` is read-only daily summary access.
- Production Web sync-chat blocking and stream-service integration remain outside Phase 1.

## Validation

- `python -m pytest tests\test_usage_quota.py tests\store_contract\test_usage_quota_contract.py tests\test_usage_quota_migrations.py -q --basetemp=.pytest_tmp_quota`
  - Result: `28 passed`
- `python -m pytest tests\store_contract\test_streaming_chat_contract.py tests\test_streaming_chat_migrations.py -q --basetemp=.pytest_tmp_stream_quota`
  - Result: `58 passed`
- `python -m pytest tests\store_contract -q --basetemp=.pytest_tmp_store_contract`
  - Result: `160 passed`
  - Existing warnings: Python 3.12 sqlite datetime adapter deprecation warnings.
- `python -m pytest tests\db\test_sqlite_readiness.py -q --basetemp=.pytest_tmp_readiness`
  - Result: `2 passed`
- `python -m ruff check app\quota app\db\stream_quota.py app\db\usage_quota_migrations.py app\db\sqlite_store.py app\db\sqlalchemy_store.py app\db\session.py tests\test_usage_quota.py tests\store_contract\test_usage_quota_contract.py tests\test_usage_quota_migrations.py tests\test_streaming_chat_migrations.py`
  - Result: `All checks passed!`
- `python -m compileall -q app tests\test_usage_quota.py tests\store_contract\test_usage_quota_contract.py tests\test_usage_quota_migrations.py`
  - Result: passed
- `git diff --check`
  - Result: passed; Windows line-ending warnings only.

## Notes For Manager Checkpoint

- The SQLAlchemy store currently exposes Phase 1 parity through the same SQLite UoW implementation
  against the SQLAlchemy-managed SQLite database file. This keeps behavior identical for the v0.1
  single-node SQLite target while preserving store-level parity tests.
- Existing streaming migration tests now remove quota tables/marker before simulating a historical
  streaming v1 database. This reflects that quota did not exist in the streaming v1 era and avoids
  manufacturing impossible foreign-key drift.
- Phase 2 should wire the UoW into `AgentStreamService` only after Manager checkpoint approval.

## Checkpoint Repair 2026-08-11

Manager requested three blocking repairs before Phase 2. Applied fixes:

- Quota reservation `idempotency_key` is now a deterministic session-scoped value derived from
  `(session_id, client_message_id)`, so identical client IDs in different Sessions no longer collide
  under the frozen `(user_id, quota_day, idempotency_key)` uniqueness constraint.
- `complete_with_quota()` now requires a live reservation in `charging_phase='provider_started'`
  before it may write an assistant message, update relationship state, or settle quota.
- `mark_provider_started_with_quota()`, `complete_with_quota()`, and `fail_with_quota()` now
  fail closed on live `owner_runtime_id` mismatch instead of returning unchanged in-progress state.

Additional validation:

- `python -m pytest tests\store_contract\test_usage_quota_contract.py -q --basetemp=.pytest_tmp_quota_fix`
  - Result: `24 passed`
- `python -m pytest tests\test_usage_quota.py tests\store_contract\test_usage_quota_contract.py tests\test_usage_quota_migrations.py tests\store_contract\test_streaming_chat_contract.py tests\test_streaming_chat_migrations.py -q --basetemp=.pytest_tmp_quota_fix_all`
  - Result: `92 passed`
- `python -m ruff check app\db\stream_quota.py tests\store_contract\test_usage_quota_contract.py`
  - Result: `All checks passed!`
- `python -m compileall -q app\db\stream_quota.py tests\store_contract\test_usage_quota_contract.py`
  - Result: passed
- `python -m pytest tests\store_contract -q --basetemp=.pytest_tmp_store_contract_fix`
  - Result: `166 passed`
  - Existing warnings: Python 3.12 sqlite datetime adapter deprecation warnings.
- `git diff --check`
  - Result: passed; Windows line-ending warnings only.
