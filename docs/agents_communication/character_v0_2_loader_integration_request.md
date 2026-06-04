# Character v0.2 Loader Integration Request

Owner suggestion: Manager Session / Character Worker follow-up

## Background

The Character worker has drafted the multi-file Aemeath v0.2 character manifest under:

```text
app/character/Aemeath.v0.2/
```

A legacy v0.1 folder also exists for path consistency:

```text
app/character/Aemeath.v0.1/
```

The old external single-file card `app/character/aemeath.v0.1.yaml` should be considered deprecated for the upcoming integration path. It may remain in the repository temporarily, but the new runtime should not depend on it as the default source.

Current v0.1 code path still loads a single YAML file through `load_character_card()` and `CharacterProvider`. The controller already depends on `CharacterProviderPort` / `CharacterRegistry`, so the controller should not be changed to read character files directly.

## Requested Change

Implement Character v0.2 loading inside the Character module while preserving the existing external call shape:

```python
CharacterRegistry.build_runtime_context(
    response_mode=response_mode,
    character_id=character_id,
) -> CharacterRuntimeContext
```

The controller should continue to call only the provider/registry port and receive a `CharacterRuntimeContext`. It should not know whether the underlying character source is v0.1 single YAML, v0.1 folder, v0.2 manifest folder, or any future storage format.

## Recommended Architecture

Keep document loading and prompt-ready mapping internal to `app/character`.

Suggested flow:

```text
DialogueController
  -> CharacterProviderPort / CharacterRegistry
      -> CharacterProvider
          -> Character loader/document builder
              -> load v0.2 manifest directory
              -> if v0.2 fails, fallback to v0.1 folder card
              -> map to CharacterRuntimeContext
  -> ContextBuilder
      -> PromptContext
      -> PromptCompiler
```

Do not make `DialogueController` read YAML, manifest files, or component files.

## Default Source And Fallback

Preferred default primary source:

```text
app/character/Aemeath.v0.2/
```

Fallback source if v0.2 loading fails:

```text
app/character/Aemeath.v0.1/character.yaml
```

The root-level legacy file:

```text
app/character/aemeath.v0.1.yaml
```

is temporarily deprecated. It can remain in the tree for compatibility during transition, but the new default should use the directory-based sources above.

## Interface Compatibility

Do not break:

```python
CharacterProfile
CharacterRuntimeContext
CharacterProviderPort
CharacterRegistry
```

No controller-facing interface change is requested.

If additional internal types are useful, add them inside `app/character`, for example:

```text
CharacterDocument
CharacterManifestLoader
CharacterRuntimeBuilder
```

These should remain internal implementation details unless manager review decides otherwise.

## v0.2 Mapping Expectations

The v0.2 manifest components currently include:

```text
profile.yaml
identity.yaml
relationship_to_user.yaml
context_frame_policy.yaml
persona_rules.yaml
speech_rules.yaml
boundaries.yaml
avoid.yaml
style_examples.yaml
stable_lore_summary.yaml
forbidden_claims.yaml
provenance.yaml
```

Recommended runtime mapping:

```text
profile.yaml              -> CharacterProfile fields
identity.yaml             -> CharacterRuntimeContext.identity
relationship_to_user.yaml -> CharacterRuntimeContext.relation_to_user
persona_rules.yaml        -> CharacterRuntimeContext.persona_rules
stable_lore_summary.yaml  -> selected runtime persona/lore rules or a dedicated mapped section if later added
speech_rules.yaml         -> CharacterRuntimeContext.speech_rules
context_frame_policy.yaml -> speech/boundary-style runtime guidance, not controller logic
boundaries.yaml           -> CharacterRuntimeContext.boundaries
forbidden_claims.yaml     -> CharacterRuntimeContext.forbidden_claims or boundaries if prompt builder does not yet render forbidden_claims
avoid.yaml                -> CharacterRuntimeContext.avoid
style_examples.yaml       -> CharacterRuntimeContext.style_examples
provenance.yaml           -> metadata/source trace only, not prompt text by default
```

Maintenance fields such as these should not be injected into prompt text by default:

```text
id
source_type
source_doc_ids
confidence
severity
notes
inject
```

Prompt-facing text should mainly use:

```text
content
user_context
sample_user_input
response_intent
response
responses
```

For `style_examples.yaml`, the schema intentionally allows both simple and richer examples:

```yaml
required:
  - id
  - user_context
  - response | responses

optional:
  - title
  - sample_user_input
  - response_intent
  - source_type
  - source_doc_ids
  - confidence
  - inject
  - notes
```

## Response Mode Selection

The current code already has `response_mode` flowing through the controller/provider/context builder path. v0.2 loading should use each rule's optional `inject` metadata internally:

```yaml
inject:
  default: true|false
  response_modes:
    - casual_chat
    - comfort
    - lore_answer
```

Suggested first implementation:

- Include rules with `inject.default: true`.
- Include rules with `response_mode` in `inject.response_modes`.
- If `inject` is missing, treat the item as default resident for non-example rule files.
- For `style_examples`, treat missing `inject` as not default resident unless manager decides otherwise.

## Fallback Behavior

If v0.2 loading fails because of missing files, invalid YAML, invalid schema, or mapping errors:

1. Log/trace a warning that v0.2 failed.
2. Load `app/character/Aemeath.v0.1/character.yaml` instead.
3. Continue returning a valid `CharacterRuntimeContext`.

The fallback should be visible through warning/trace/log metadata where practical, but should not require controller logic changes.

## Tests Requested

Add or update tests covering:

1. v0.2 manifest directory loads successfully through `CharacterRegistry` / `CharacterProvider`.
2. v0.2 maps to `CharacterProfile` correctly.
3. v0.2 maps key `CharacterRuntimeContext` sections correctly.
4. `style_examples.yaml` supports both `response` and `responses` forms.
5. Response mode selection includes matching style examples/rules and excludes nonmatching ones.
6. v0.2 failure falls back to `Aemeath.v0.1/character.yaml`.
7. Controller still calls only `CharacterProviderPort` and does not read character files directly.
8. Existing v0.1 single-card behavior remains supported if still used explicitly.

Acceptance commands:

```bash
pytest
ruff check .
```

## Notes For Report

When reporting this change later, please mention:

- The default character source is moving from the old root-level v0.1 YAML to `app/character/Aemeath.v0.2/`.
- The v0.1 folder path `app/character/Aemeath.v0.1/character.yaml` is the fallback.
- The root-level `app/character/aemeath.v0.1.yaml` is deprecated but not deleted yet.
- Controller-facing interfaces remain unchanged.
- Character document assembly is internal to the Character module.
