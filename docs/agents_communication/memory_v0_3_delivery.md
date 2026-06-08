# Memory Worker Session 3 Delivery: v0.3 Long-term Memory

Date: 2026-06-05

Task: T-0120 Memory v0.3 Explicit Long-term Memory

Status: implementation complete, including Session scope/provenance hardening

## Summary

Implemented an LLM-extracted, vector-indexed long-term memory path:

- Explicit remember triggers enqueue durable extraction jobs.
- 12-message session windows enqueue durable extraction jobs.
- `/memory extract [limit]` synchronously extracts recent session messages for testing.
- Long-term memories are stored in `long_term_memories` and indexed in
  `long_term_memory_embeddings`.
- `MemoryReader` actively recalls vector-indexed memories every turn when long-term memory is
  enabled.
- CLI `/memory debug on|off` prints recalled memory content from `AgentTurnOutput.used_memories`.

## Boundary Notes

- `DialogueController` still depends only on memory ports.
- Interaction memory does not enter `EvidencePack`.
- `response_mode` was not restored.
- Character and RAG layers do not read memory data.
- `AEMEATH_ENABLE_LONG_TERM_MEMORY=false` still disables long-term memory write, job enqueue, and
  recall.

## Runtime Additions

- Job queue table: `memory_extraction_jobs`
- Embedding table: `long_term_memory_embeddings`
- Standalone worker: `python -m app.memory.worker`
- Extraction service: Chinese JSON-only prompt over the existing `ChatModelClient`
- Embedding provider selection:
  - default/runtime-safe provider: deterministic `hash`
  - advanced local semantic provider: `bge-m3`, enabled explicitly by config
  - tests: deterministic hash provider

## CLI

- `/memory extract [limit]`
- `/memory debug on`
- `/memory debug off`

Debug output shows:

```text
[memory recall]
memory_xxx score=0.731 reason=embedding_similarity
- 用户喜欢吃火锅，尤其是辣锅。
```

## Interface Changes

- `AgentTurnOutput` keeps `used_memory_ids` and adds `used_memories`.
- `MemoryWriterPort.persist_decision(...)` requires verified `session_id` and `character_id`.
- `MemoryWriterPort` adds `enqueue_window_if_due(...)`.
- `SessionStorePort` adds read-only `require_session(...)`.
- `MemoryQuery` adds optional `session_id`; Controller always supplies it.
- Memory records separate visibility fields (`scope_*`) from provenance fields (`source_*`).
- Memory delete/disable operations require `memory_id + user_id`.

## Validation

- `pytest`: 95 passed
- `ruff check .`: All checks passed
- `pytest tests/test_chinese_encoding_and_behavior.py`: 4 passed

## Scope And Migration Follow-up

- Added `user_global | character | session` visibility semantics shared by keyword recall, vector
  recall, and fallback.
- Exact dedupe matches the target scope tuple only.
- Added a transactional, versioned `memory_scope_provenance_v1` migration.
- Existing memories migrate to `user_global`.
- Known message IDs move from `related_memory_ids` to `source_message_ids`; completed jobs provide
  `source_job_id` and fallback `source_session_id`.
- Unknown legacy references remain in `related_memory_ids` with an ID-only migration warning.
- Automatic extraction continues to create `user_global` memories.
- `/memory extract` accepts owned active/ended Sessions and rejects archived Sessions.

## Known Follow-ups

- `AEMEATH_MEMORY_VECTOR_DB_PATH` is currently exposed for config compatibility, but embeddings are
  stored in the main SQLite DB table in this implementation.
- Scope-aware storage and recall are available, but automatic scope classification remains
  deferred; extraction currently writes `user_global`.
- BGE-M3 is explicit advanced configuration because it requires local model dependencies.
- BGE-M3 threshold needs real conversation tuning; hash embedding tests validate plumbing, not
  semantic quality.
