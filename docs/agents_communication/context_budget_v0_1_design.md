# Context Budget And Compression v0.1 Design

Date: 2026-07-28

Status: manager decisions approved. This document is the implementation contract for v0.1.

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

The estimator must use the same system-content rendering semantics as `PromptCompiler`. Its result
is exactly the sum of the final system and user message content lengths; role labels and JSON/API
envelopes are excluded.

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
  over-budget context with `status=protected_floor_exceeded` and `over_budget_chars > 0`.
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

`relationship_state` is also protected. It is small, stable, and important to roleplay
continuity. Unknown section ids are protected by default so a future safety or identity section
cannot be silently trimmed before it is classified.

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

T-0135 already owns semantic query construction and memory retrieval ranking. The compressor must
not create a second relevance policy from `MemoryRecord.importance` or `confidence`.

Rules:

- Apply `AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT` before global trimming.
- Keep the first N memories in the exact order returned by `MemoryReader`.
- Drop from the end so T-0135 retrieval order remains authoritative.
- Dropped memory ids go into `dropped_source_ids.interaction_memories`.

### RAG Evidence

Rules for `canon_evidence` and `voice_evidence`:

- Apply `AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT` independently to each evidence section.
- Keep the first N results in the exact order returned by the evidence provider.
- Do not re-rank from `confidence` inside the compressor.
- `EVIDENCE_LIMIT=4` applies independently to `canon_evidence` and `voice_evidence`.
- Dropped retrieval ids go into `dropped_source_ids.canon_evidence` or
  `dropped_source_ids.voice_evidence`.

### Style Examples

Rules:

- Style examples are trimmable.
- They should be dropped from the end first, preserving the beginning of the curated example bank.
- v0.1 assigns deterministic `style_example:0001` synthetic budget source ids because the current
  `CharacterRuntimeContext` contains formatted strings but not the source YAML item ids.
- v0.1 does not add a separate style-example limit.
- This does not replace the older Character Style Examples Prompt Budget backlog item. It only
  gives the general context budget layer permission to trim examples when needed.

### Line-Level Trimming

v0.1 should trim whole `PromptSection.content_lines` items.

Do not:

- truncate strings by character count;
- split multiline style examples;
- rewrite evidence;
- summarize content.

If a trimmable section loses all content, remove the whole section. Do not add an omission
placeholder. The report records the dropped section id without consuming prompt budget or adding
new model-facing text.

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
- Keep each trimmable section's `content_lines` and `source_ids` positionally aligned.
- Use existing recent-message, memory, and evidence ids. Give style examples deterministic
  synthetic ids without storing their text in metadata.

- Call `self.context_compressor.compress(context=prompt_context)`.
- Return the compressed context.

Recommended compressor behavior:

- Return a new `PromptContext`.
- Return new `PromptSection` objects for any changed section.
- Do not mutate existing section lists in place.
- Apply per-section cardinality limits before the global character limit.
- Preserve upstream Memory/Evidence order; do not call embedding or re-rank.
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
- A future tokenizer estimator can replace `compiled_message_chars_v1` behind policy without changing
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
    "schema_version": "context-budget-report/v1",
    "policy_version": "context-budget-policy/v1",
    "budget_enabled": true,
    "budget_limit": 24000,
    "budget_unit": "chars",
    "estimator": "compiled_message_chars_v1",
    "status": "trimmed",
    "estimated_before": 31852,
    "estimated_after_limits": 27410,
    "estimated_after": 23876,
    "applied": true,
    "over_budget_chars": 0,
    "kept_section_ids": ["base_instructions", "canon_evidence"],
    "trimmed_section_ids": ["canon_evidence"],
    "dropped_section_ids": ["style_examples"],
    "trimmed_sections": [
      {
        "section_id": "style_examples",
        "before_lines": 25,
        "after_lines": 6,
        "dropped_line_count": 19
      }
    ],
    "dropped_line_count": 19,
    "kept_source_ids": {
      "canon_evidence": ["world_info:86"]
    },
    "dropped_source_ids": {
      "style_examples": ["style_example:6", "style_example:7"],
      "interaction_memories": ["memory_3"],
      "canon_evidence": ["world_info:200"]
    },
    "limits": {
      "recent_message_limit": 8,
      "memory_limit": 4,
      "evidence_limit_per_section": 4
    }
  }
}
```

Notes:

- Keep metadata namespaced under `context_budget`.
- Do not store dropped content.
- Keep current top-level `memory_ids` and `evidence_ids` as pre-compression retrieval facts.
- Record final prompt membership in `context_budget.kept_source_ids` and
  `context_budget.dropped_source_ids`.

Trace event recommendation:

```text
event_type: context_budget_applied
payload:
  budget_enabled
  budget_limit
  estimated_before
  estimated_after
  applied
  status
  over_budget_chars
  kept_section_ids
  trimmed_section_ids
  dropped_section_ids
  trimmed_sections
  dropped_line_count
  kept_source_ids
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

3. Memory trimming preserves the upstream T-0135 result order.
   - Build memories in an explicit relevance order.
   - Set memory limit to 2.
   - Assert the first two are kept and the compressor does not re-rank from record fields.

4. RAG evidence trimming preserves provider result order.
   - Build canon and voice results in an explicit upstream order.
   - Set evidence limit to 2.
   - Assert the first two in each section remain.

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
    - Assert protected sections remain, `status=protected_floor_exceeded`, and
      `over_budget_chars > 0`.

12. T-0135 is unchanged.
    - Compare enabled and disabled turns with a recording embedding provider.
    - Assert the semantic embedding requests, canonical query text, and call count are identical.
    - Assert the compressor never activates the `character_examples` semantic role.

## 9. Risks / Open Questions

- Character count is not token count. It is deterministic, but it cannot guarantee provider token
  fit exactly.
- The compressor intentionally trusts upstream Memory/Evidence order. Retrieval quality remains
  owned by T-0135 and the retrieval providers.
- If the configured budget is lower than protected content, the final prompt can still exceed the
  limit. This must be visible in metadata and trace.
- Trimming style examples may affect voice consistency. That is acceptable for v0.1 only if
  manager agrees that identity/rules are more important than examples.
- Style-example ids are synthetic positional ids in v0.1. Reordering the curated bank changes
  those ids, but does not change Character documents or introduce vector selection.
- Section metadata will become more detailed. It should contain ids, scores, and counts, not full
  dropped text.
- Public `used_memory_ids` and the existing top-level prompt metadata continue to describe
  retrieval. Prompt inclusion is separately observable under `context_budget`.

## 10. Manager Decisions

1. Approved: add `ContextCompressorPort` and inject a compressor into `ContextBuilder`.

2. Approved rollout default: `AEMEATH_ENABLE_CONTEXT_BUDGET=false` in v0.1.

3. Approved budget unit: deterministic compiled-message character count.

4. Approved limits: `MAX_CHARS=24000`, recent messages `8`, memories `4`, Canon evidence `4`,
   and Voice evidence `4`.

5. Approved protected sections: base instructions, character context, persona rules, speech rules,
   boundaries, forbidden claims, avoid, relationship state, and current user input.

6. Memory/Evidence ordering approved:
   - preserve upstream order and do not re-rank in the compressor.

7. Metadata semantics approved:
   - existing `memory_ids` and `evidence_ids` remain pre-budget retrieval ids;
   - final kept/dropped ids live under `context_budget`.

8. Approved trace event: add `context_budget_applied` with ids/counts/estimates only.

9. Style-example behavior approved:
   - no separate style config or vector selection;
   - remove the section entirely when every example is dropped.

10. Prompt-log inspector display polish is deferred; the JSONL record already contains the full
    safe budget report.
