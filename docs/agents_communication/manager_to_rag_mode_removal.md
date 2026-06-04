# Manager To RAG Worker: Global Mode Removed

Date: 2026-06-03

## Summary

Manager session has removed the global `ResponseModeRouter` from the active runtime.

`DialogueController` no longer decides `casual_chat`, `lore_answer`, `comfort`,
`roleplay_scene`, or any other global mode. RAG should not depend on an external
mode label to decide whether retrieval is needed.

## Current RAG Contract

The active controller call is:

```python
evidence_pack = evidence_provider.build_evidence_pack(
    user_id=turn_input.user_id,
    character_id=active_character_id,
    query=turn_input.message,
    metadata=turn_input.metadata,
)
```

`EvidenceProviderPort.build_evidence_pack(...)` should return:

- `EvidencePack()` when no lore / plot / voice retrieval is needed.
- `EvidencePack(lore_results=..., voice_results=...)` when retrieval is useful.

The controller must not perform `need_lore`, `need_plot`, or `need_voice` checks.
Those checks belong inside the RAG / evidence module.

## What Changed

- `response_mode` is removed from the formal `EvidenceProviderPort`.
- `WuwaRagEvidenceProvider` now owns internal retrieval planning.
- `metadata.retrieval_intent` may be used only as an explicit debug/test override.
- Legacy optional `response_mode` kwargs may remain temporarily for old tests/scripts,
  but worker code should not design new logic around it.

## RAG Worker Requirements

Please design future RAG code around internal evidence policy:

```text
EvidenceProvider
  -> EvidenceNeedPolicy / RetrievalPolicy
  -> RetrieverPort
  -> EvidencePack
```

The policy can inspect:

- user query text
- character_id
- metadata
- available namespaces
- retrieval index state

The policy should not require:

- `ResponseModeRouter`
- `response_mode`
- `ConversationScope`
- external controller-side flags

## Boundary Reminder

Interaction memory remains separate:

- User memory goes through `MemoryReaderPort`.
- Canon lore / plot / voice evidence goes through `EvidenceProviderPort`.
- Do not put normal user memory retrieval into `EvidencePack.memory_results` unless a
  future manager-approved unified retrieval experiment explicitly requests it.

## Follow-Up Work

Recommended RAG follow-up:

- Rename any remaining public docs/CLI language from "response mode" to "retrieval intent"
  or "evidence policy".
- Add tests showing ordinary daily input returns empty `EvidencePack()`.
- Add tests showing lore-like input triggers lore retrieval without external mode.
- Add trace-friendly metadata such as `retrieval_intent`, `policy_reason`, and selected
  namespace counts.
