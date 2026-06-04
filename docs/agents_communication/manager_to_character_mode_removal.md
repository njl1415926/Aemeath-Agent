# Manager To Character Worker: Global Mode Removed

Date: 2026-06-03

## Summary

Manager session has removed the global `ResponseModeRouter` from the active runtime.

`DialogueController` no longer passes a global `response_mode` into character loading.
Character context selection should be owned by the Character module itself.

## Current Character Contract

The active controller call is:

```python
character_context = character_provider.build_runtime_context(
    character_id=active_character_id,
    user_input=turn_input.message,
    metadata=turn_input.metadata,
)
```

`CharacterProviderPort.build_runtime_context(...)` should return a complete
`CharacterRuntimeContext` for prompt building.

The controller must not decide:

- which style examples to include
- whether the turn is comfort / lore / daily
- how much character context to inject

Those decisions belong inside the Character module.

## What Changed

- `response_mode` is removed from the formal `CharacterProviderPort`.
- `CharacterProvider` now accepts `user_input` and `metadata`.
- It currently infers internal legacy context tags from the query and metadata.
- Existing manifest fields named `inject.response_modes` remain temporarily supported as
  legacy tags, but they are no longer the architecture contract.

## Character Worker Requirements

Future Character design should move toward:

```text
CharacterProvider
  -> CharacterContextPolicy
  -> CharacterRuntimeContext
```

The policy can inspect:

- user input
- character_id
- metadata.character_context_tags
- prompt budget constraints added later
- character manifest version

The policy should not require:

- `ResponseModeRouter`
- external `response_mode`
- controller-side style example selection

## Manifest Guidance

Existing:

```yaml
inject:
  default: false
  response_modes:
    - comfort
```

Preferred future shape:

```yaml
inject:
  default: false
  context_tags:
    - comfort
```

This rename can be handled in a future Character task. For now, do not do a large manifest
rewrite unless explicitly assigned.

## Boundary Reminder

Character should provide resident identity, voice rules, boundaries, stable persona, and
selected examples.

Character should not:

- run RAG
- read plot text directly
- construct `EvidencePack`
- decide memory retrieval

Dynamic lore / plot / voice evidence remains under `EvidenceProviderPort`.

## Follow-Up Work

Recommended Character follow-up:

- Rename manifest `response_modes` to `context_tags`.
- Add a dedicated `CharacterContextPolicy` object.
- Add tests for context selection using `user_input` and `metadata`, not `response_mode`.
- Add prompt budget handling for style examples.
