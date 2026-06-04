# Character v0.2 交付说明

## 交付范围

本次 Character Session 已完成 Aemeath Character v0.2 多文件角色卡的基础接入，并保持外部调用接口不变。

Controller 仍然只通过 CharacterProviderPort / CharacterRegistry 调用 Character 模块：

```python
load_profile(character_id=...) -> CharacterProfile
build_runtime_context(
    character_id=...,
    user_input=...,
    metadata=...
) -> CharacterRuntimeContext
```

DialogueController 不直接读取 YAML、manifest 或 component files。

## 当前默认路径

默认 Character 来源已切换为：

```text
app/character/Aemeath.v0.2/
```

配置位置：

```text
app/config.py
```

v0.2 manifest 入口：

```text
app/character/Aemeath.v0.2/manifest.yaml
```

## Fallback 策略

如果 v0.2 加载失败，会 fallback 到：

```text
app/character/Aemeath.v0.1/character.yaml
```

旧根路径：

```text
app/character/aemeath.v0.1.yaml
```

仍然保留兼容，显式传入时可以加载，但默认路径不再依赖它，后续视为 deprecated。

Fallback 不静默。当前通过 `CharacterRuntimeContext.source_doc_ids` 暴露：

```text
character_source_fallback
primary_path=...
fallback_path=...
error_type=...
```

## 修改的主要代码

```text
app/character/loader.py
app/character/provider.py
app/config.py
app/agent/context_builder.py
tests/conftest.py
tests/test_character_registry.py
tests/test_character_manifest.py
```

## Character 内部实现

新增内部加载抽象：

```text
CharacterDocument
load_character_manifest()
load_character_document()
```

这些只属于 `app/character` 内部实现，不是 controller-facing interface。

## v0.2 映射关系

当前 v0.2 多文件内容会映射到 `CharacterRuntimeContext`：

```text
profile.yaml              -> CharacterProfile
identity.yaml             -> runtime.identity
relationship_to_user.yaml -> runtime.relation_to_user
persona_rules.yaml        -> runtime.persona_rules
stable_lore_summary.yaml  -> runtime.persona_rules，带 stable_lore_summary: 前缀
speech_rules.yaml         -> runtime.speech_rules
context_frame_policy.yaml -> runtime.speech_rules
boundaries.yaml           -> runtime.boundaries
forbidden_claims.yaml     -> runtime.forbidden_claims
avoid.yaml                -> runtime.avoid
style_examples.yaml       -> runtime.style_examples
```

`stable_lore_summary` 暂时并入 `persona_rules`，但用 `stable_lore_summary:` 前缀保持区分。若后续使用量增加，建议由 manager review 是否扩展 `CharacterRuntimeContext`。

## context_tags / 注入策略

`response_mode` 已从 Character 外部调用链路中弃用。Controller 不再传入全局 mode，也不参与 Character 规则选择。

CharacterProvider 当前可根据 `user_input` 和 `metadata.character_context_tags` 做内部轻量判断：

规则：

```yaml
inject:
  default: true
  context_tags:
    - comfort
    - lore_answer
```

当前策略：

- 普通规则缺少 `inject` 时，默认常驻。
- `inject.default: true` 会常驻。
- `context_tags` 命中时注入对应的非 example 规则。
- `style_examples` 当前采用最简单策略：全量注入 25 条默认样例，不依赖 mode。
- Controller 不参与选择。

## style_examples 状态

默认 `style_examples.yaml` 目前使用 skill 原文示例转换版，共 25 条。

另有短样例版本：

```text
app/character/Aemeath.v0.2/style_examples_short.yaml
```

两版保持共同最低结构：

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

默认版保留 `回答风格 A/B/C` 作为样例内部说明。正式输出不应包含这些标签，已在 `speech_rules.yaml` 中补充规则：

```text
不添加“爱弥斯：”“角色：”“旁白：”“回答风格 A：”等说话人标签或示例说明标签。
```

## PromptContext 变化

`ContextBuilder` 现在会把以下已有字段加入 prompt section：

```text
style_examples
forbidden_claims
```

这没有改变 controller-facing interface。

## 生成的检查样例

已生成当前默认模式的 runtime context 样例：

```text
app/character/example_context/aemeath_v0.2_default_runtime_context.md
```

使用：

```text
character_source: app/character/Aemeath.v0.2
external_mode: none
```

粗略 token 估算：

```text
约 5k+ tokens
```

其中 `Style Examples` 已改为全量注入，是当前最大头；已记录为后续优化项。

## source_doc_ids 说明

`source_doc_ids` 当前只作为兼容和粗粒度 trace 字段使用，不作为 v0.2 Character 设计核心。

不要围绕它设计强溯源。

真正需要剧情原文溯源的内容，后续应进入 RAG / Evidence，而不是 Character 常驻规则。

## 已验证测试

运行结果：

```text
pytest: 24 passed
ruff check .: All checks passed
```

覆盖内容包括：

- v0.2 directory 通过 CharacterRegistry 加载
- v0.2 映射 CharacterProfile
- v0.2 映射 CharacterRuntimeContext 关键 section
- context_tags 可选择非 example 规则
- style_examples 默认全量注入
- style_examples 支持 response / responses
- v0.2 加载失败 fallback 到 v0.1 folder
- legacy root yaml 显式传入仍可加载
- controller 不直接读 character 文件

## 后续建议

1. 继续人工微调 `style_examples.yaml`，尤其是 skill 原文示例中的旧口径、称呼和动作描写尺度。
2. 后续可对比 `style_examples.yaml` 与 `style_examples_short.yaml`，选择最终默认版本或融合版本。
3. 已记录优化项：当前 `style_examples` token 占比偏高，后续需要重点优化。可考虑由 Character 内部按 context_tags 限制注入数量，或从 25 条原文样例精简到 8-12 条短样例。
4. 若 stable lore 增多，建议扩展 `CharacterRuntimeContext` 或新增清晰 section，而不是长期混在 persona_rules 中。
5. CharacterProvider 不应加入 RAG 检索逻辑；剧情原文和证据仍应走 EvidenceProviderPort。
