# Character v0.2 Loader Integration Report

## Summary

Implemented v0.2 Character manifest loading behind the existing Character provider boundary.

External controller-facing calls remain unchanged:

```python
CharacterProviderPort.load_profile(character_id=...)
CharacterProviderPort.build_runtime_context(
    character_id=...,
    user_input=...,
    metadata=...,
)
```

The returned objects remain:

```python
CharacterProfile
CharacterRuntimeContext
```

`DialogueController` does not read YAML, manifest files, or component files directly.

## Default Source

Default character path now points to:

```text
app/character/Aemeath.v0.2/
```

This is configured in:

```text
app/config.py
```

The old root-level file remains compatible when explicitly passed:

```text
app/character/aemeath.v0.1.yaml
```

It is treated as deprecated for the default path but was not deleted.

## Fallback

If v0.2 loading fails, Character loading falls back to:

```text
app/character/Aemeath.v0.1/character.yaml
```

Fallback is visible through `CharacterRuntimeContext.source_doc_ids`, including:

```text
character_source_fallback
primary_path=...
fallback_path=...
error_type=...
```

This keeps the existing public interface unchanged while making fallback observable.

## Internal Implementation

Changed internal Character module files:

```text
app/character/loader.py
app/character/provider.py
```

Added internal concepts:

```text
CharacterDocument
load_character_manifest()
load_character_document()
```

These are implementation details inside `app/character`, not controller-facing interfaces.

## v0.2 Runtime Mapping

v0.2 component files are loaded from `manifest.yaml` and mapped into `CharacterRuntimeContext`:

```text
profile.yaml              -> CharacterProfile
identity.yaml             -> runtime.identity
relationship_to_user.yaml -> runtime.relation_to_user
persona_rules.yaml        -> runtime.persona_rules
stable_lore_summary.yaml  -> runtime.persona_rules with stable_lore_summary prefix
speech_rules.yaml         -> runtime.speech_rules
context_frame_policy.yaml -> runtime.speech_rules
boundaries.yaml           -> runtime.boundaries
forbidden_claims.yaml     -> runtime.forbidden_claims
avoid.yaml                -> runtime.avoid
style_examples.yaml       -> runtime.style_examples
```

`stable_lore_summary` is temporarily mapped into `persona_rules` with a `stable_lore_summary:` prefix, as requested for the first implementation.

## Inject / Context Tags

`response_mode` is deprecated for Character. `CharacterProvider.build_runtime_context(...)` now receives `user_input` and `metadata`, then applies lightweight Character-internal context tags:

```yaml
inject:
  default: true
  context_tags:
    - comfort
    - lore_answer
```

Rules:

- Rule files missing `inject` default to resident.
- Matching `context_tags` includes non-example items.
- `style_examples` are currently injected in full as the simplest v0.2 policy.
- Controller does not participate in selection.

## Style Examples

`style_examples.yaml` supports both forms:

```yaml
response: ...
```

and:

```yaml
responses:
  - ...
  - ...
```

The prompt-facing formatting supports optional fields:

```text
user_context
sample_user_input
response_intent
response/responses
```

## Prompt Context

`ContextBuilder` now includes existing `CharacterRuntimeContext` fields that were previously unused:

```text
style_examples
forbidden_claims
```

This does not change the controller-facing interface.

## Source IDs

`source_doc_ids` remains a compatibility/trace field, not a core v0.2 design pillar. It is used for coarse file references and fallback visibility only.

## Tests

Added tests in:

```text
tests/test_character_manifest.py
```

Updated existing test defaults to v0.2 where appropriate.

Coverage includes:

- v0.2 directory loads through `CharacterRegistry`
- v0.2 maps `CharacterProfile`
- v0.2 maps key `CharacterRuntimeContext` sections
- response mode inject selects style examples
- style examples support `response` and `responses`
- v0.2 failure falls back to `Aemeath.v0.1/character.yaml`
- legacy root YAML explicitly passed still loads
- controller does not directly read character files

## Verification

Commands run:

```bash
pytest
ruff check .
```

Results:

```text
pytest: 20 passed
ruff check .: All checks passed
```
