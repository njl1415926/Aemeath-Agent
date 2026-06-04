# Memory Worker Session 3 Delivery: v0.2 Minimal Runtime

Date: 2026-06-04

Task: T-0117 Memory v0.2 Minimal Runtime

Status: implementation complete, ready for manager review

## Summary

Memory Worker Session 3 implemented the v0.2 minimal memory runtime:

- Recent conversation is the default memory baseline.
- Long-term memory read/write is disabled by default.
- Long-term memory can still be explicitly enabled by configuration.
- `DialogueController` continues to call stable memory ports and does not own memory policy.
- The current user input is no longer duplicated inside the `Recent conversation` prompt section.

## Runtime Behavior

Default runtime:

```text
Character v0.2
+ WorldInfo RAG
+ recent conversation from MessageStore
+ current user input
=> v0.2 minimal prompt
```

Long-term interaction memory is disabled by default:

```text
long_term_memories = []
used_memory_ids = []
```

Conversation history still persists to the `messages` table:

```text
user: user1
assistant: response1
user: user2
assistant: response2
```

Character context, RAG evidence, system prompt sections, and long-term memory sections are not
written into recent conversation. Only actual user / assistant messages are stored there.

## Key Changes

- Added `AEMEATH_ENABLE_LONG_TERM_MEMORY=false` config.
- Added disabled memory port implementations:
  - `DisabledMemoryReader`
  - `DisabledMemoryWriter`
  - `DisabledMemoryWriteClassifier`
- Updated `app/main.py` to inject disabled memory ports by default.
- Preserved v0.1 long-term memory path when `AEMEATH_ENABLE_LONG_TERM_MEMORY=true`.
- Kept `MemoryAdminService` connected to the real store so existing memory records remain manageable.
- Updated `DialogueController` to filter out the current `user_message_id` from `recent_messages`
  before passing them to `ContextBuilder`.

## Interface Compliance

The implementation preserves the agreed contracts:

- `DialogueController` still calls:
  - `MemoryReaderPort.retrieve_query(...)`
  - `MemoryWriteClassifierPort.decide(...)`
  - `MemoryWriterPort.persist_decision(...)`
  - `ContextBuilderPort.build_context(...)`
- Memory enable/disable policy is handled by dependency injection in `app/main.py`.
- `ContextBuilder` still receives `recent_messages` and `long_term_memories`.
- `long_term_memories` can be empty in v0.2 minimal runtime.
- `recent conversation` remains present.
- No `response_mode` was restored.
- No embedding memory was introduced.
- Character and RAG do not read memory data.
- Interaction memory is not placed into `EvidencePack.memory_results`.

## Prompt Duplication Fix

Before this task, the current user message was saved, then immediately read back by
`get_recent_messages(...)`, causing it to appear both:

- inside the system prompt `Recent conversation` section
- as the final `user` message produced by `PromptCompiler`

The fix keeps the database log intact but filters the current `user_message_id` before prompt
construction:

```text
save current user message
read recent messages
filter current user_message_id
build PromptContext
compile final current user input as separate user message
```

This is a minimal short-term memory hygiene fix. `PromptCompiler` and `ContextBuilder` were not
restructured.

## Files Changed

- `.env.example`
- `app/config.py`
- `app/main.py`
- `app/memory/disabled.py`
- `app/agent/dialogue_controller.py`
- `tests/conftest.py`
- `tests/test_memory_recall.py`
- `tests/test_memory_writer.py`
- `tests/test_memory_minimal_runtime.py`
- `docs/task_log.md`
- `docs/architecture.md`

## Validation

- `pytest`: 50 passed
- `ruff check .`: All checks passed

## Remaining Non-Scope Items

These were intentionally not implemented in this session:

- Semantic deduplication of recent conversation.
- Prompt section token budget and trimming.
- Conversation summarization.
- Long-term memory merge / update / conflict / decay policy.
- Embedding memory or memory RAG.
- Prompt preview / trace viewer beyond existing prompt context trace summaries.

## Notes For Manager

`docs/task_log.md` now lists T-0117 as `review`.

The main review point is whether defaulting `AEMEATH_ENABLE_LONG_TERM_MEMORY=false` should also be
mirrored into local `.env` if manager wants the developer runtime to match `.env.example`.
