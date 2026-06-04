# Character Manifest v0.2 Template Design

## 1. Current Character Layer Summary

当前 Character 层的稳定边界如下：

- `DialogueController` 只通过 `CharacterProviderPort` 获取角色资料。
- `CharacterRegistry` 已按 `character_id` 分发 provider，后续多角色应继续走 registry。
- `CharacterProfile` 承载角色元信息。
- `CharacterRuntimeContext` 承载 prompt-time 常驻角色上下文。
- v0.1 当前只加载单个 YAML 角色卡。
- RAG / lore / voice evidence 已预留 `EvidenceProviderPort` 和 `EvidencePack`，不应塞进 CharacterProvider。

v0.2 的目标是设计一个多文件 YAML 文档体系，让 Character 从“单卡片”升级为“manifest + components”的可追踪角色系统。

本阶段只设计模板，不填入具体角色内容。

## 2. Character vs RAG Boundary

Character 层负责常驻、稳定、压缩的角色约束：

- 角色是谁。
- 角色如何称呼和看待用户。
- 角色当前处于哪个 canon point。
- 稳定人格、关系、说话方式和边界。
- 角色知道什么、不知道什么。
- 哪些规则来自官方支持、剧情归纳或项目策略。
- 少量高价值风格锚点。

RAG 层负责按需注入的临时证据：

- 具体剧情原文。
- 具体台词。
- 某个版本任务文本。
- 某个事件、人物、地点、道具细节。
- 长篇 voice examples。
- 当前用户问题需要的 evidence。

边界原则：

- Character manifest 不保存大段剧情原文。
- Character manifest 只保存压缩后的稳定规则和少量短样例。
- `lore_and_voice/*` 可作为来源，但不应整篇进入 Character。
- `plot_text/*` 和原始 gamedata 只作为后续 RAG / importer 数据源。
- CharacterProvider 不做 query 检索，不构建 EvidencePack。

## 3. Multi-File Directory Template

推荐 v0.2 每个角色版本使用一个目录：

```text
app/character/<character_id>.v0.2/
  manifest.yaml
  profile.yaml
  identity.yaml
  relationship_to_user.yaml
  persona_rules.yaml
  speech_rules.yaml
  boundaries.yaml
  avoid.yaml
  style_examples.yaml
  stable_lore_summary.yaml
  forbidden_claims.yaml
  provenance.yaml
```

未来多角色可扩展为：

```text
app/character/manifests/
  <character_id>/
    v0.2/
      manifest.yaml
      profile.yaml
      ...
```

短期建议先用 `app/character/<character_id>.v0.2/`，避免一次移动太多现有路径。

## 4. Entry Manifest Template

`manifest.yaml` 只做入口索引和版本元数据，不放具体角色内容。

```yaml
schema_version: character_manifest.v0.2
character_id: <character_id>
version: <character_id>.v0.2
status: draft

components:
  profile: profile.yaml
  identity: identity.yaml
  relationship_to_user: relationship_to_user.yaml
  persona_rules: persona_rules.yaml
  speech_rules: speech_rules.yaml
  boundaries: boundaries.yaml
  avoid: avoid.yaml
  style_examples: style_examples.yaml
  stable_lore_summary: stable_lore_summary.yaml
  forbidden_claims: forbidden_claims.yaml
  provenance: provenance.yaml

loader:
  component_mode: explicit_files
  unknown_component_policy: ignore
  missing_required_component_policy: error
```

Required components for v0.2 runtime:

- `profile`
- `identity`
- `relationship_to_user`
- `persona_rules`
- `speech_rules`
- `boundaries`
- `avoid`
- `forbidden_claims`
- `provenance`

Optional components:

- `style_examples`
- `stable_lore_summary`

## 5. Component Templates

### `profile.yaml`

```yaml
profile:
  display_name: <display_name>
  english_name: <english_name>
  source: <source_title_or_world>
  canon_point: <canon_point_id>
  roleplay_pack_path: <relative_roleplay_pack_path_or_null>
  locale: zh-CN
  aliases:
    - <optional_alias>
```

### `identity.yaml`

```yaml
identity:
  description: <short_stable_identity_description>
  known_scope:
    - <stable_known_scope_item>
  unknown_scope:
    - <stable_unknown_scope_item>
  current_state:
    - <stable_current_state_item>
```

### `relationship_to_user.yaml`

```yaml
relationship_to_user:
  user_role: <user_role_in_character_world>
  address_user_as:
    - <direct_address_option>
  default_stance: <stable_relationship_stance>
  intimacy_boundary: <default_intimacy_policy>
  relationship_constraints:
    - <stable_relationship_constraint>
```

### `persona_rules.yaml`

```yaml
persona_rules:
  - id: persona_001
    content: <stable_persona_rule>
    source_type: <canon|derived|authorial>
    confidence: <0.0-1.0>
    inject:
      default: true
      context_tags: []
    notes: <maintainer_only_optional_note>
```

### `speech_rules.yaml`

```yaml
speech_rules:
  - id: speech_001
    content: <stable_speech_rule>
    source_type: <canon|derived|authorial>
    confidence: <0.0-1.0>
    inject:
      default: true
      context_tags: []
```

### `boundaries.yaml`

```yaml
boundaries:
  - id: boundary_001
    content: <stable_boundary_rule>
    source_type: <canon|derived|authorial>
    confidence: <0.0-1.0>
    severity: <low|medium|high|critical>
    inject:
      default: true
      context_tags: []
```

### `avoid.yaml`

```yaml
avoid:
  - <style_or_behavior_to_avoid>
```

### `style_examples.yaml`

```yaml
style_examples:
  - id: example_001
    content: <short_style_anchor_not_long_dialogue>
    source_type: <canon|derived|authorial>
    confidence: <0.0-1.0>
    inject:
      default: false
      context_tags:
        - <context_tag>
```

Rules for examples:

- Keep examples short.
- Do not copy long official lines.
- Do not store full scene dialogues here.
- Full example banks should go to Retrieval / voice evidence.

### `stable_lore_summary.yaml`

```yaml
stable_lore_summary:
  - id: lore_summary_001
    content: <compressed_stable_lore_fact_or_theme>
    source_type: <canon|derived|authorial>
    confidence: <0.0-1.0>
    inject:
      default: false
      context_tags:
        - lore_answer
        - roleplay_scene
```

Rules for stable lore summary:

- Store only high-level stable facts or themes.
- Avoid event chains that should be retrieved by RAG.
- Avoid raw plot text and long quotations.

### `forbidden_claims.yaml`

```yaml
forbidden_claims:
  - id: forbidden_001
    content: <claim_the_character_must_not_make>
    source_type: <canon|derived|authorial>
    confidence: <0.0-1.0>
    inject:
      default: true
      context_tags: []
```

### `provenance.yaml`

```yaml
provenance:
  source_docs:
    - id: <source_doc_id>
      path: <relative_path_or_external_ref>
      layer: <persona_core|speech_style|boundary|stable_lore|rag_source|private_source>
      source_type: <canon|derived|authorial>
      runtime_read: false
      description: <short_maintainer_description>
```

## 6. Source Type Design

Allowed values:

```text
canon      官方文本或官方数据直接支持的规则或事实。
derived    从剧情表现、人工摘要或语气样本中归纳出的稳定规则。
authorial  项目为了体验、安全、边界或工程稳定性定义的策略。
```

Use guidance:

- `canon` should be rare in Character unless the rule is short and directly supported.
- `derived` is appropriate for persona, speech and stable lore summaries.
- `authorial` is appropriate for safety, output format, anti-hallucination and UX policies.

## 7. Injection Policy

Every structured rule may include:

```yaml
inject:
  default: true
  context_tags:
    - <context_tag>
```

Semantics:

- `default: true` means the rule is resident every turn.
- `default: false` means it is not resident unless a Character-internal context tag matches.
- `context_tags` allows lightweight tag-specific injection without exposing a global response mode to the controller.
- Missing `inject` should default to resident for backward authoring convenience, but loader should warn in validation later.
- `style_examples` are currently injected in full as a simple v0.2 policy; this is intentionally conservative for behavior quality, and token cost should be optimized later.

Recommended resident content:

- profile metadata
- identity description
- relationship stance
- core persona rules
- core speech rules
- critical boundaries
- forbidden claims
- avoid list

Recommended mode-only content:

- style anchors
- stable lore summaries
- roleplay-scene-specific speech constraints
- comfort-mode emotional pacing notes

## 8. Runtime Mapping

The multi-file manifest still maps into current dataclasses.

`CharacterProfile` mapping:

```text
character_id         <- manifest.character_id
display_name         <- profile.display_name
english_name         <- profile.english_name
source               <- profile.source
version              <- manifest.version
canon_point          <- profile.canon_point
roleplay_pack_path   <- profile.roleplay_pack_path
```

`CharacterRuntimeContext` mapping:

```text
identity             <- identity.description + selected current_state/known_scope summary
relation_to_user     <- relationship_to_user.default_stance + address/intimacy summary
persona_rules        <- selected persona_rules + selected stable_lore_summary
speech_rules         <- selected speech_rules + selected style_examples
boundaries           <- selected boundaries + selected forbidden_claims
avoid                <- avoid list
style_examples       <- selected style_examples
forbidden_claims     <- selected forbidden_claims
source_doc_ids       <- optional legacy trace field; v0.2 template does not maintain per-rule source_doc_ids
```

No `CharacterProviderPort` change is required for v0.2.

If future review needs per-rule metadata in prompt trace, propose a separate manager-reviewed interface change instead of overloading this task.

## 9. Loader Design

Suggested loader flow for later implementation:

```text
CharacterProvider(path)
  -> load_character_document(path)
     -> if path is v0.1 YAML: load v0.1 card
     -> if path is v0.2 directory: read manifest.yaml
     -> if path is v0.2 manifest file: read it directly
     -> load declared components relative to manifest directory
     -> validate required components
     -> normalize into internal document model
  -> build CharacterProfile / CharacterRuntimeContext
```

Important constraints:

- Loader may read component YAML files.
- Loader must not read RAG source documents directly.
- Loader must not inspect current user query.
- Loader must not construct EvidencePack.
- DialogueController must remain unaware of file layout.

## 10. Multi-Character Extension

Registry remains the multi-character entry point:

```python
CharacterRegistry(
    {
        "<character_id>": Path("app/character/<character_id>.v0.2"),
    },
    default_character_id="<character_id>",
)
```

Multi-character requirements:

- `character_id` is the stable key.
- Each character directory is self-contained.
- Controller should continue to request by `AgentTurnInput.character_id` only.

## 11. Migration Plan

1. Keep the v0.1 character card available under `app/character/Aemeath.v0.1/character.yaml` until config and tests move to directory-aware loading.
2. Agree on the v0.2 multi-file template with manager/user.
3. Create an empty draft directory from the templates.
4. Fill `profile`, `identity`, `relationship_to_user` first.
5. Fill `boundaries` and `forbidden_claims` before adding richer persona.
6. Add persona and speech rules in small reviewable batches.
7. Add only compressed `stable_lore_summary`; leave detailed lore to Retrieval.
8. Implement loader after the template and first content batch are accepted.
9. Add tests for v0.1 compatibility and v0.2 mapping.
10. Only after tests pass, consider switching default config to v0.2.

## 12. Test Plan For Later Implementation

Minimum tests when code implementation starts:

- v0.1 YAML still loads.
- v0.2 directory manifest loads through `CharacterProvider`.
- Missing required component produces a clear error.
- `CharacterProfile` maps correctly from multi-file YAML.
- `CharacterRuntimeContext` maps selected resident rules correctly.
- response mode controls mode-only style examples and lore summaries.
- Runtime context does not contain long RAG source text.
- User chat does not switch into meta/system mode because of literal tags in the message body.

Acceptance commands:

```bash
pytest
ruff check .
```

## 13. Open Questions For Manager

- Should v0.2 live under `app/character/<character_id>.v0.2/` first, or jump directly to `app/character/manifests/<character_id>/v0.2/`?
- Should loader allow optional components to be absent, or require empty files for every component?
- Should validation enforce source_type enum in the first implementation, or only document it until content stabilizes?
- Should draft manifests have `status: draft|reviewed|active` and refuse active loading unless reviewed?
- Should the CLI/main process add a structured system-dialogue mode for project collaboration? If yes, it should be an outer input mode such as `input_mode=meta`, not a tag parsed from normal user chat.

## 14. Current Design Decisions

- Character dialogue stays in normal roleplay mode only. It supports in-world and real-world user topics, but not free-form meta/system discussion inside ordinary chat.
- Meta or system collaboration, if added later, belongs to the CLI/main process as a structured mode outside Character. Do not trigger it from literal text such as `/*META*/` in the user message body.

