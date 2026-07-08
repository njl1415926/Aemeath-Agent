# Manager Review: SQLAlchemy Persistence Backend v0.1

Date: 2026-07-07

Status: Accepted.

## Summary

Worker Session 10 implemented an opt-in SQLAlchemy persistence backend while keeping the existing
raw SQLite backend as the default.

The design uses:

- `app/db/models.py` for SQLAlchemy models.
- `app/db/session.py` for sync SQLite engine/session setup.
- `app/db/repositories/*` for repository-level SQLAlchemy queries.
- `app/db/sqlalchemy_store.py` as a thin facade that preserves existing store ports.
- `app/db/store_factory.py` and `AEMEATH_DB_BACKEND=sqlite_raw|sqlalchemy` for backend selection.

The facade is accepted as a transitional adapter. Query logic should remain inside repositories.

## Manager Validation

Re-run by manager:

- `pytest -q tests/store_contract/test_sqlalchemy_memory_worker_smoke.py`: 2 passed.
- `pytest -q tests/store_contract/test_memory_contract.py`: 10 passed.
- `pytest -q tests/store_contract/test_session_message_contract.py`: 6 passed.
- `pytest -q tests/store_contract`: 24 passed.
- `pytest`: 149 passed.
- `ruff check .`: All checks passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: no whitespace errors, CRLF warnings only.

## Boundary Decisions

- Default backend remains `sqlite_raw`.
- SQLAlchemy backend remains opt-in and experimental.
- No old `data/aemeath.db` migration is included.
- No Alembic, PostgreSQL, async SQLAlchemy, or default backend switch is included.
- `DialogueController`, Character, RAG, Prompt, and Model strategies were not changed.

## Follow-Up Reviews

Memory review passed on 2026-07-07. Persistence worker added the requested SQLAlchemy enabled
memory worker single-job smoke, two-store claim smoke, and embedding shape mismatch parity test.

Session review passed on 2026-07-07 after fixing a high-priority `list_sessions(..., limit=N)`
ordering bug. SQLAlchemy now applies `ORDER BY updated_at DESC` before `LIMIT`, matching raw
SQLite. A cross-backend store contract test covers newest-session windowing.

### Memory Review

Please review SQLAlchemy parity for:

- memory scope visibility;
- exact active memory dedupe;
- owner-only archive/disable;
- memory extraction job enqueue/claim/complete/fail;
- embedding upsert/search behavior;
- enabled memory worker loop behavior beyond the current disabled/job-enqueue smoke checks.

The important files are:

- `app/db/repositories/memories.py`
- `app/db/repositories/memory_jobs.py`
- `app/db/repositories/memory_embeddings.py`
- `app/db/sqlalchemy_store.py`
- `tests/store_contract/test_memory_contract.py`

### Session Review

Please review SQLAlchemy parity for:

- session create/ensure/require/list/end/resume/archive;
- ownership errors;
- archived/ended behavior;
- `message_count`;
- `last_message_at`;
- ordering of session lists.

The important files are:

- `app/db/repositories/sessions.py`
- `app/db/repositories/messages.py`
- `app/db/sqlalchemy_store.py`
- `tests/store_contract/test_session_message_contract.py`
