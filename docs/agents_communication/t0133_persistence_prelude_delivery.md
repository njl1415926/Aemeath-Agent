# T-0133 Persistence Prelude Delivery

## Scope

Implemented the approved Persistence-only prelude for canonical Memory embeddings. No Memory reader,
extractor, worker, session behavior, dialogue flow, or RAG code changed.

## Contracts

- `create_or_get_exact_active_memory(...)` atomically resolves active identity
  `user_id + scope + scope_character_id + scope_session_id + content` and returns
  `ExactActiveMemoryResult(memory_id, created)`.
- A partial unique expression index is the final database invariant. Raw SQLite detects old duplicate
  active identities and raises content-safe `STORAGE_MIGRATION_REQUIRED` without mutating rows or writing
  the marker. Both backends validate `PRAGMA index_list`, `PRAGMA index_xinfo`, and exact normalized DDL
  before using an existing database. Normalization preserves single-quoted literals, including case,
  whitespace, and escaped quotes, so an expanded, narrowed, or literal-mutated predicate is rejected.
- Canonical artifact search now receives `MemoryArtifactEligibility` and returns
  `eligible_memory_count` from the same owner/scope/status/active/namespace/type/confidence snapshot as
  scoring and diagnostics.
- `list_memory_embedding_artifact_gaps(...)` provides bounded stable Worker input for an exact full
  signature. It distinguishes `missing`, `content_stale`, `vector_corrupt`, `signature_corrupt`, and
  `legacy_only` without treating artifacts from another signature as corrupt.

## Safety Boundaries

- No `INSERT OR IGNORE` is used for create-or-get; both backends serialize with `BEGIN IMMEDIATE`, select,
  then insert normally.
- Gap scanning is one bounded SQL window, not N+1. Its cursor advances only to the last inspected row.
- The cross-user default is internal-only and is not wired into Service, FastAPI, or MCP.
- Persistence reports facts and diagnostics only; it does not choose repair, retry, fallback, or rebuild.

## Validation

- `pytest -q tests/store_contract/test_memory_canonical_contract.py tests/store_contract/test_embedding_artifact_contract.py`
  passed: 31 tests.
- Full `pytest -q`, `ruff check .`, and `python -m compileall -q app tests` passed.
- `git diff --check` found no whitespace errors; Git reported only existing CRLF conversion notices.
- Follow-up index-gate tests cover expanded and narrowed predicates in both backends plus archive/restart
  recreation of the same identity, and quote-aware literal mutation rejection in both backends.

## Follow-up

Memory integration can now consume the new contracts for fact-first extraction, exact-signature recall,
and bounded repair. Provider-neutral query embedding source, repair scheduling, and runtime fallback stay
with the Memory session.
