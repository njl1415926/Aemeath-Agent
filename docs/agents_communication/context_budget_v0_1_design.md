# Context Budget And Compression v0.1 Design

Date: 2026-07-09

Scope: design only. No runtime code is implemented in this session.

## 1. Summary

Context Budget And Compression v0.1 should add a deterministic prompt budget layer between
`ContextBuilder` and `PromptCompiler`.

Recommended shape:

- Add a small `ContextBudgetPolicy` data object and a deterministic `ContextCompressor`.
- Expose a `ContextCompressorPort.compress(context: PromptContext) -> PromptContext`.
- Inject the compressor into `ContextBuilder`, so CLI and FastAPI share the same behavior through
  the existing `build_controller()` / `build_agent_service()` wiring.
- Keep `PromptCompiler` as a pure formatter.
- Keep `DialogueController` flow unchanged except for one optional trace event after
  `build_context()`.
- Use a character-count estimator in v0.1, not a tokenizer.
- Never trim character identity, role rules, safety boundaries, or the current user message.
- Record all budget decisions in `PromptContext.metadata`, and let prompt-run logs capture that
  metadata naturally.

This design intentionally does not do LLM summarization, does not write summaries back to memory,
does not change memory extraction, and does not change RAG retrieval.

## 2. Proposed Architecture

Current flow:

```text
DialogueController
  -> ContextBuilder.build_context(...)
  -> PromptContext
  -> PromptCompiler.compile_messages(...)
  -> model
```

Recommended v0.1 flow:

```text
DialogueController
  -> ContextBuilder.build_context(...)
       -> build full PromptContext
       -> ContextCompressorPort.compress(...)
       -> return compressed PromptContext
  -> optional trace: context_budget_applied
  -> existing trace: prompt_context_built
  -> PromptCompiler.compile_messages(...)
  -> model
```

Why this location:

- `ContextBuilder` already has the raw sources: `CharacterRuntimeContext`, recent messages,
  memories, relationship state, and `EvidencePack`.
- `PromptContext` already has stable section ids and is already logged in prompt-run records.
- CLI and FastAPI both go through the same runtime construction, so one compressor wiring covers
  both.
- `PromptCompiler` stays simple and deterministic.

New internal module recommendation:

```text
app/agent/context_budget.py
  ContextBudgetPolicy
  ContextBudgetReport
  NoopContextCompressor
  DeterministicContextCompressor
  estimate_prompt_context_chars(...)
```

New or updated interface recommendation:

```python
class ContextCompressorPort(Protocol):
    def compress(self, *, context: PromptContext) -> PromptContext:
        ...
```

`ContextBuilder` should accept a compressor constructor argument with a noop default. That keeps
existing direct tests and older builder construction working.

## 3. Budget Policy

v0.1 should use deterministic character counting as the primary budget unit.

Recommended estimator:

```text
estimated_chars =
  len(user_input)
  + for each section:
      len(section.title)
      + sum(len(line) for line in section.content_lines)
      + newline/header overhead
```

The exact estimator should be implemented once and tested. It does not need to match
`PromptCompiler` byte-for-byte, but it should be stable and close to the compiled prompt shape.

Why not tokenizer in v0.1:

- No provider-specific dependency.
- No model/tokenizer drift.
- Easy unit tests.
- Chinese text is naturally counted by Python `len()` as characters, which is stable enough for a
  deterministic guardrail.

Budget semantics:

- `max_chars` is a hard target only for trimmable content.
- Protected sections are never trimmed.
- If protected sections plus current user input exceed the budget, the compressor must return an
  over-budget context and record `over_budget_after_protected=true`.
- The compressor must never truncate strings mid-line in v0.1.

Recommended default rollout:

- `AEMEATH_ENABLE_CONTEXT_BUDGET=false` for v0.1 compatibility.
- Tests should verify disabled budget preserves current compiled prompt content.
- Manager may later flip the default after reviewing prompt quality.

Recommended default limits when enabled:

```text
max_chars: 24000
recent_message_limit: 8
memory_limit: 4
evidence_limit: 4 per evidence section
```

## 4. Section Priority And Trim Rules

### Protected Sections

Never trim:

- `base_instructions`
- `character_context`
- `persona_rules`
- `speech_rules`
- `boundaries`
- `forbidden_claims`
- `avoid`
- current user message, stored as `PromptContext.user_input`

Rationale: these carry core identity, role behavior, safety boundaries, hidden-prompt resistance,
forbidden claims, and user intent for the current turn.

### High Priority Sections

Keep in full in v0.1:

- `relationship_state`

Rationale: it is small, stable, and important to roleplay continuity. If this section grows in a
future version, it can receive its own policy.

### Trimmable Sections

Trimmable from lowest retention priority to highest retention priority:

1. `style_examples`
2. `voice_evidence`
3. `canon_evidence`
4. `interaction_memories`
5. `recent_conversation`

This means style examples are dropped first, and recent conversation is dropped last.

### Recent Conversation

Rules:

- Apply `AEMEATH_CONTEXT_BUDGET_RECENT_MESSAGE_LIMIT` before global trimming.
- Keep the last N message lines, not the first N.
- If global budget still needs more space, drop the oldest retained recent message first.
- Keep the last 2 recent message lines when possible.
- The current user message is not part of `recent_conversation` and is never trimmed.

Source tracking:

- Dropped message ids go into `dropped_source_ids.recent_conversation`.

### Long-Term Memories

Current limitation: `ContextBuilder` receives `list[MemoryRecord]`, not
`list[MemorySearchResult]`. That means retrieval score is not available at this boundary today.

Recommended v0.1 ranking without a port change:

```text
importance desc
confidence desc
original order asc
```

If manager wants true retrieval-score trimming, approve a small interface change so
`DialogueController` can pass `MemorySearchResult` or annotate memory section items with
`retrieval_score`.

Rules:

- Apply `AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT` before global trimming.
- Drop lower-ranked memories first.
- Preserve kept memory display in original order unless manager explicitly wants rank-sorted
  display.
- Dropped memory ids go into `dropped_source_ids.interaction_memories`.

### RAG Evidence

Rules for `canon_evidence` and `voice_evidence`:

- Apply `AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT` independently to each evidence section.
- Rank by `confidence desc`, then source order.
- Source order should use existing retrieval order by default, with metadata order fields used
  only when present.
- Drop lower-confidence evidence first.
- Dropped retrieval ids go into `dropped_source_ids.canon_evidence` or
  `dropped_source_ids.voice_evidence`.

### Style Examples

Rules:

- Style examples are trimmable.
- They should be dropped from the end first, preserving the beginning of the curated example bank.
- v0.1 should not require a separate style-example config unless manager wants one.
- This does not replace the older Character Style Examples Prompt Budget backlog item. It only
  gives the general context budget layer permission to trim examples when needed.

### Line-Level Trimming

v0.1 should trim whole `PromptSection.content_lines` items.

Do not:

- truncate strings by character count;
- split multiline style examples;
- rewrite evidence;
- summarize content.

If a trimmable section loses all content, use a short placeholder such as:

```text
- (omitted by context budget)
```

The placeholder should not include dropped content.

## 5. Config Proposal

Add fields to `Settings`:

```text
enable_context_budget: bool = False
context_budget_max_chars: int = 24000
context_budget_recent_message_limit: int = 8
context_budget_memory_limit: int = 4
context_budget_evidence_limit: int = 4
```

Add environment variables:

```env
AEMEATH_ENABLE_CONTEXT_BUDGET=false
AEMEATH_CONTEXT_BUDGET_MAX_CHARS=24000
AEMEATH_CONTEXT_BUDGET_RECENT_MESSAGE_LIMIT=8
AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT=4
AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT=4
```

Validation recommendations:

- `MAX_CHARS` must be at least 1.
- section limits should allow 0 for tests and emergency operation.
- invalid integers should fail fast through existing settings construction behavior.

FastAPI should not need a separate API budget flag in v0.1. It should inherit the same runtime
policy from `Settings`, unlike prompt-run logging and model fallback where CLI/API defaults are
intentionally different.

## 6. Interface / Code Change Plan

Recommended implementation files:

- Add `app/agent/context_budget.py`.
- Add `ContextCompressorPort` to `app/agent/ports.py`.
- Update `app/agent/context_builder.py`.
- Update `app/config.py`.
- Update `app/main.py`.
- Add a small trace event in `app/agent/dialogue_controller.py`.
- Optionally update `app/observability/prompt_log_inspector.py` to display budget metadata.
- Add tests under existing prompt, trace, API/service, and prompt-log test files.

Recommended `ContextBuilder` changes:

- Constructor accepts `context_compressor: ContextCompressorPort | None = None`.
- Build the complete pre-budget `PromptContext`.
- Add deterministic per-line metadata for trimmable sections, for example:

```text
section.metadata["budget_items"] = [
  {
    "line_index": 0,
    "source_id": "memory_1",
    "importance": 9,
    "confidence": 0.9,
    "original_index": 0
  }
]
```

- Call `self.context_compressor.compress(context=prompt_context)`.
- Return the compressed context.

Recommended compressor behavior:

- Return a new `PromptContext`.
- Return new `PromptSection` objects for any changed section.
- Do not mutate existing section lists in place.
- Keep all dropped content out of metadata. Store ids, counts, estimates, and reasons only.

Recommended controller change:

- After `prompt_context = self.context_builder.build_context(...)`, inspect
  `prompt_context.metadata.get("context_budget")`.
- If budget is enabled, record `context_budget_applied` with ids/counts/estimates only.
- Then keep the existing `prompt_context_built` trace event.

Future compatibility:

- A future LLM summarizer can implement the same `ContextCompressorPort`, but it must remain
  disabled for v0.1.
- A future LangGraph runtime can make compression its own node without changing the prompt data
  contract.
- A future tokenizer estimator can replace `char_count_v0_1` behind policy without changing
  section trim rules.

## 7. Metadata And Observability

Recommended `PromptContext.metadata` shape:

```json
{
  "character_id": "aemeath",
  "evidence_ids": {
    "lore": ["world_info:86"],
    "voice": []
  },
  "memory_ids": ["memory_1"],
  "context_budget": {
    "budget_enabled": true,
    "budget_limit": 24000,
    "budget_unit": "chars",
    "estimator": "char_count_v0_1",
    "estimated_before": 31852,
    "estimated_after": 23876,
    "applied": true,
    "over_budget_after_protected": false,
    "trimmed_sections": [
      {
        "section_id": "style_examples",
        "before_lines": 25,
        "after_lines": 6,
        "dropped_line_count": 19
      }
    ],
    "dropped_line_count": 19,
    "dropped_source_ids": {
      "style_examples": ["style_example:6", "style_example:7"],
      "interaction_memories": ["memory_3"],
      "canon_evidence": ["world_info:200"]
    },
    "limits": {
      "recent_message_limit": 8,
      "memory_limit": 4,
      "evidence_limit": 4
    }
  }
}
```

Notes:

- Keep metadata namespaced under `context_budget`.
- Do not store dropped content.
- Keep current `memory_ids` and `evidence_ids` as the ids remaining in the final prompt, or add
  explicit `kept_*` fields if manager wants both pre-budget and post-budget id lists.
- If manager wants backward compatibility for `memory_ids` as "retrieved ids", add separate
  `context_budget.kept_source_ids` and leave old fields unchanged. This needs a decision.

Trace event recommendation:

```text
event_type: context_budget_applied
payload:
  budget_enabled
  budget_limit
  estimated_before
  estimated_after
  applied
  over_budget_after_protected
  trimmed_sections
  dropped_line_count
  dropped_source_ids
```

Trace should not store prompt text or dropped content.

Prompt-run log behavior:

- No new prompt-run logger interface is needed.
- Prompt-run records already store `prompt_context_metadata` and final `sections`.
- Therefore budget metadata and final trimmed sections are captured naturally.

Prompt log inspector:

- Optional v0.1 enhancement: print one compact line such as
  `context_budget: enabled=True before=31852 after=23876 dropped=19`.

## 8. Test Plan

Recommended focused tests:

1. Protected sections are never trimmed.
   - Use a very small budget with huge optional sections.
   - Assert `base_instructions`, `character_context`, `persona_rules`, `speech_rules`,
     `boundaries`, `forbidden_claims`, `avoid`, and user input remain intact.

2. Recent conversation keeps the latest messages.
   - Build 10 recent messages.
   - Set recent limit to 3.
   - Assert the last 3 remain and the first 7 ids are in dropped metadata.

3. Memory trimming prefers higher importance.
   - Build memories with different importance and confidence.
   - Set memory limit to 2.
   - Assert low-ranked memory ids are dropped.
   - Add a tie case proving original order is deterministic.

4. RAG evidence trimming prefers higher confidence.
   - Build canon and voice `RetrievalResult` items with mixed confidence.
   - Set evidence limit to 2.
   - Assert lower-confidence ids are dropped, with source-order tie behavior.

5. Style examples are trimmable.
   - Build many style examples under a small budget.
   - Assert examples drop from the end and metadata records the count.

6. Metadata records the budget result.
   - Assert `budget_enabled`, `budget_limit`, `estimated_before`, `estimated_after`,
     `trimmed_sections`, `dropped_line_count`, and `dropped_source_ids`.
   - Assert dropped content is not present in metadata.

7. Disabled budget preserves current compiled prompt content.
   - Compare `PromptCompiler.compile_messages(...)` output with budget disabled against current
     behavior.

8. CLI and FastAPI share the same strategy.
   - Verify `build_controller(settings=budget_enabled)` and
     `create_app(settings=budget_enabled)` both use the same compressor policy through shared
     settings/runtime wiring.
   - Do not implement separate API budget logic.

9. Prompt log contains budget metadata.
   - Enable prompt-run log and context budget.
   - Run a turn that forces trimming.
   - Assert `record["prompt_context_metadata"]["context_budget"]` exists.
   - Assert final sections are trimmed.

10. Trace contains `context_budget_applied`.
    - Enable trace log and context budget.
    - Run a turn.
    - Assert a compact trace event exists without full prompt text.

11. Protected floor over budget is observable.
    - Set an unrealistically tiny max budget.
    - Assert protected sections remain and `over_budget_after_protected=true`.

## 9. Risks / Open Questions

- Character count is not token count. It is deterministic, but it cannot guarantee provider token
  fit exactly.
- Current `ContextBuilder` does not receive memory retrieval score. v0.1 can rank by
  `MemoryRecord.importance` and `confidence`; true score ranking needs a small interface change.
- If the configured budget is lower than protected content, the final prompt can still exceed the
  limit. This must be visible in metadata and trace.
- Trimming style examples may affect voice consistency. That is acceptable for v0.1 only if
  manager agrees that identity/rules are more important than examples.
- RAG evidence confidence is not always meaningful across providers. Source order remains the
  deterministic tie-breaker.
- Section metadata will become more detailed. It should contain ids, scores, and counts, not full
  dropped text.
- If existing `memory_ids` / `evidence_ids` metadata semantics mean "all selected before
  compression", changing them to post-compression ids could surprise prompt-log consumers.

## 10. Required Manager Decisions

1. Approve adding `ContextCompressorPort` and injecting a compressor into `ContextBuilder`.

2. Confirm rollout default:
   - recommendation: `AEMEATH_ENABLE_CONTEXT_BUDGET=false` in v0.1;
   - alternative: enable by default after focused prompt-quality review.

3. Confirm v0.1 budget unit:
   - recommendation: deterministic char count with `budget_unit="chars"`.

4. Confirm default limits:
   - recommendation: `MAX_CHARS=24000`, recent messages `8`, memories `4`, evidence `4`.

5. Confirm protected sections:
   - recommendation: never trim base instructions, character context, persona rules, speech
     rules, boundaries, forbidden claims, avoid, relationship state, or current user input.

6. Confirm memory ranking:
   - recommendation: use importance, confidence, original order in v0.1;
   - manager decision needed if retrieval score must be included now.

7. Confirm metadata semantics:
   - should existing `memory_ids` and `evidence_ids` mean final kept ids, or all pre-budget
     selected ids?

8. Confirm trace event:
   - recommendation: add `context_budget_applied` with ids/counts/estimates only.

9. Confirm whether to add a separate style-example limit env var:
   - recommendation: no separate style config in v0.1; use total budget and trim order.

10. Confirm whether prompt-log inspector should display budget summary in v0.1 or wait for a
    later observability polish pass.
