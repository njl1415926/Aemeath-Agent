# T-0136 Context Budget And Compression v0.1 Delivery

Date: 2026-07-28

Status: `done`

## Delivery

- Added a default-off deterministic `ContextCompressorPort` implementation after complete
  `PromptContext` assembly and before `PromptCompiler`.
- Added 24000-character, Recent 8, Memory 4, Canon 4, and Voice 4 defaults.
- Protected Character identity and rules, safety boundaries, relationship state, unknown sections,
  and current input.
- Preserved T-0135 query construction and all upstream Memory/Evidence ordering.
- Added safe kept/dropped section and source-id metadata plus `context_budget_applied` trace.
- Removed empty optional sections and reported `protected_floor_exceeded` without truncating protected
  content.

## Boundaries

- No embedding, tokenizer, LLM summary, semantic reranking, Persistence, RAG, Memory, or Character
  contract change.
- Existing public memory/evidence ids continue to describe retrieval; the budget report describes
  final Prompt membership.
- Removed body text is absent from budget metadata, trace, and prompt-run sections.

## Manager Review

Manager reviewed the algorithm, composition-root wiring, T-0135 ordering, logs, and configuration.
No blocking or non-blocking implementation finding remains. One additional test pins the complete
Style -> Voice -> Canon -> Memory -> Recent trim order.

## Validation

- Focused Context/Semantic/Prompt/Observability suite: 53 passed before the additional priority test.
- Final full repository suite: 438 passed.
- Ruff, compileall, and diff checks: passed.
- Existing FastAPI, SQLite datetime, and legacy Embedding setting deprecation warnings remain.
