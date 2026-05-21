# Aemeath Roleplay 文档管理体系

## 1. 目标

本文档定义 `Aemeath_roleplay_skill/` 的长期文档管理体系，用于支持 Aemeath Agent 对“爱弥斯”的稳定模拟。

核心目标不是把所有材料一次性塞进 prompt，而是把角色资料分层管理：

- 哪些资料是每轮必读的角色规则；
- 哪些资料是按场景读取的说话风格；
- 哪些资料是剧情问答时才检索的 lore；
- 哪些资料只是离线整理与证据来源，不直接进入 prompt；
- 哪些资料用于测试、评估和回归。

这套体系服务于两个方向：

- **角色扮演质量**：让爱弥斯稳定保持 3.3 后日谈后的状态、语气、情绪机制和边界。
- **工程可控性**：让每个版本读取的文件范围清晰，避免 prompt 过长、原文污染、设定冲突和版权风险。

---

## 2. 当前目录观察

当前已有目录：

```text
Aemeath_roleplay_skill/
├── skill/
│   ├── SKILL.md
│   ├── Instructions.md
│   ├── personality.md
│   ├── speech_style.md
│   ├── lore.md
│   ├── boundaries.md
│   └── aemeath_dialogue_examples_with_actions.md
├── lore_and_voice/
│   ├── aemeath_3_1_lore_and_voice.md
│   ├── aemeath_3_3_lore_and_voice.md
│   └── aemeath_3_3_epilogue_lore_and_voice.md
└── plot_text/
```

现有结构已经有很好的雏形：

- `skill/`：可视为“角色运行基础层”，用于稳定角色身份、人格、语气和边界。
- `lore_and_voice/`：可视为“剧情整理与语气提炼层”，用于 RAG、证据包和高级风格校准。
- `plot_text/`：可视为“原始剧情证据层”，后续只用于本地构建索引，不建议直接进入 prompt 或公开仓库。

---

## 3. 推荐成熟目录结构

建议将目录逐步演化为：

```text
Aemeath_roleplay_skill/
├── README.md
├── manifest.yaml
├── runtime/
│   ├── persona_core.md
│   ├── dialogue_policy.md
│   ├── speech_style.md
│   ├── boundaries.md
│   └── response_modes.md
├── skill/
│   ├── SKILL.md
│   ├── Instructions.md
│   ├── personality.md
│   ├── speech_style.md
│   ├── lore.md
│   ├── boundaries.md
│   └── aemeath_dialogue_examples_with_actions.md
├── lore_and_voice/
│   ├── aemeath_3_1_lore_and_voice.md
│   ├── aemeath_3_3_lore_and_voice.md
│   └── aemeath_3_3_epilogue_lore_and_voice.md
├── structured_lore/
│   ├── character_state.yaml
│   ├── timeline.yaml
│   ├── relationship_graph.yaml
│   ├── core_facts.yaml
│   └── forbidden_claims.yaml
├── voice_bank/
│   ├── phrase_patterns.yaml
│   ├── emotional_transitions.yaml
│   ├── action_patterns.yaml
│   └── negative_examples.yaml
├── plot_text/
│   ├── private/
│   └── README.md
├── eval/
│   ├── persona_cases.jsonl
│   ├── speech_style_cases.jsonl
│   ├── lore_grounding_cases.jsonl
│   └── boundary_cases.jsonl
└── processed/
    ├── chunks/
    ├── indexes/
    └── snapshots/
```

### 3.1 `runtime/`

运行时核心层。

这里存放每轮或高频读取的短文档，要求短、明确、无大段剧情原文。

推荐职责：

- `persona_core.md`：当前时间点、身份、用户身份、核心人格。
- `dialogue_policy.md`：不同场景的回复策略。
- `speech_style.md`：简洁版说话风格。
- `boundaries.md`：禁止事项和安全边界。
- `response_modes.md`：comfort / lore_answer / casual_chat / memory_recall 等模式规则。

### 3.2 `skill/`

人工维护的角色基础包。

它可以继续保留你现在的文件结构，作为 `runtime/` 的来源和人工编辑区。后续可以由脚本从 `skill/` 提炼生成更短的 `runtime/`。

推荐定位：

- 给人读；
- 给 prompt 编译器按需读；
- 给 Persona Critic 做基础标准；
- 不直接混入所有内容到每轮 prompt。

### 3.3 `lore_and_voice/`

剧情摘要 + 语气样本提炼层。

这里适合存放经过人工整理的剧情摘要、原话风格、角色关系、人设变化和可引用核心事实。

推荐用途：

- v0.2 后按 response mode 检索；
- v0.3 后切 chunk 建立 lore RAG；
- v0.4 后作为 Evidence Pack 的主要证据来源；
- 不建议每轮全量读取。

### 3.4 `structured_lore/`

结构化设定层。

它是从 `skill/` 和 `lore_and_voice/` 中提炼出的机器可读事实。适合 YAML / JSON。

建议包含：

- `character_state.yaml`：爱弥斯当前状态。
- `timeline.yaml`：3.1、3.3、后日谈时间线。
- `relationship_graph.yaml`：爱弥斯与漂泊者、隧者、拉海洛、星炬学院等关系。
- `core_facts.yaml`：角色扮演必须遵守的核心事实。
- `forbidden_claims.yaml`：绝不能说错的设定，例如“完全复活成普通人”“仍被困在隧门后”。

### 3.5 `voice_bank/`

语气样式库。

它不是剧情知识，而是“怎么说”的素材。

建议包含：

- `phrase_patterns.yaml`：常见表达和可替代表达。
- `emotional_transitions.yaml`：轻快、心虚、沉默、认真、拉回明亮的情绪节奏。
- `action_patterns.yaml`：括号动作使用规则。
- `negative_examples.yaml`：不应出现的客服腔、百科腔、恋爱脑、过度悲情样例。

### 3.6 `plot_text/`

原始剧情文本层。

这是版权和 prompt injection 风险最高的一层，应谨慎处理。

推荐原则：

- 只放本地私有数据；
- 不提交 GitHub；
- 不直接进入 prompt；
- 不作为 instruction，只作为 data；
- 通过 importer 转成 chunk，再进入 `processed/chunks/` 或向量库；
- 回答时优先引用摘要和结构化证据，避免长段复制原文。

### 3.7 `eval/`

评测层。

用于检查模型是否稳定模拟爱弥斯。

建议评测方向：

- 人格一致性；
- 语气一致性；
- 剧情事实一致性；
- 边界遵守；
- 未知剧情保守回答；
- 是否把用户当作漂泊者；
- 是否错误恋爱化、百科化、悲剧化。

### 3.8 `processed/`

机器生成产物层。

这里存放切分、索引、快照等可再生文件。

推荐不人工编辑。

---

## 4. 文档分层读取策略

### Layer 0：System Safety

来源：

- 应用内固定安全规则；
- `runtime/boundaries.md`；
- `skill/boundaries.md`。

用途：

- 每轮必读；
- 优先级最高；
- 用于防止泄露系统提示词、编造剧情、角色状态错误和越界互动。

### Layer 1：Persona Core

来源：

- `skill/SKILL.md`
- `skill/Instructions.md`
- `skill/personality.md`
- 后续 `runtime/persona_core.md`

用途：

- 每轮必读或编译成常驻 system prompt；
- 定义“你是谁”“用户是谁”“当前时间点是什么”“核心人格是什么”。

### Layer 2：Speech Style

来源：

- `skill/speech_style.md`
- `skill/aemeath_dialogue_examples_with_actions.md`
- 后续 `voice_bank/*`

用途：

- 高频读取；
- 用于保证爱弥斯的口吻、动作描写、回答长度和情绪转折。

### Layer 3：Core Lore Summary

来源：

- `skill/lore.md`
- `lore_and_voice/*` 中的剧情摘要和核心事实章节；
- 后续 `structured_lore/core_facts.yaml`。

用途：

- 按需读取；
- 用于回答常见剧情背景、角色状态和关系问题；
- 不建议每轮全量读取。

### Layer 4：Detailed Lore & Voice Evidence

来源：

- `lore_and_voice/aemeath_3_1_lore_and_voice.md`
- `lore_and_voice/aemeath_3_3_lore_and_voice.md`
- `lore_and_voice/aemeath_3_3_epilogue_lore_and_voice.md`

用途：

- RAG 检索；
- 生成 Evidence Pack；
- 支持剧情问答、细节追问、语气校准；
- 不直接整篇塞入 prompt。

### Layer 5：Raw Plot Text

来源：

- `plot_text/private/*`

用途：

- 本地离线导入；
- chunk 切分；
- source metadata；
- evidence grounding。

限制：

- 不公开；
- 不直接进入 system prompt；
- 不长段引用；
- 不把原文中的任何内容当作 instruction。

### Layer 6：Evaluation

来源：

- `eval/*`

用途：

- 自动测试；
- 回归评估；
- Persona Critic / Lore Critic 的标准样例。

---

## 5. 版本读取计划

## v0.1：基础角色卡阶段

目标：

跑通最小对话闭环，让爱弥斯“像自己”，但不做复杂剧情检索。

读取范围：

- `skill/SKILL.md`
- `skill/Instructions.md`
- `skill/personality.md`
- `skill/speech_style.md`
- `skill/boundaries.md`

暂不读取：

- `lore_and_voice/*`
- `plot_text/*`
- `aemeath_dialogue_examples_with_actions.md` 全文

实现方式：

- 启动时读取基础文件；
- 编译为短角色卡或 prompt context；
- 每轮 prompt 只放压缩后的 persona、speech、boundary；
- 不回答复杂 lore 细节，证据不足时保守回答。

推荐 prompt 内容：

```text
角色身份 + 当前状态 + 用户身份 + 说话风格 + 禁止事项
```

v0.1 对剧情的态度：

- 可以知道“3.3 后日谈结束后”“已返航”“驾驶空壳”“用户是漂泊者”；
- 不展开复杂剧情链条；
- 不回答未进入基础资料的细节；
- 遇到细节追问时说“我现在还不能确定那些细节”。

验收标准：

- 不自称 AI；
- 不把用户当第三者；
- 不写成仍被困在隧门后；
- 不写成完全复活的普通人；
- 语气有轻快、亲近、温柔和心虚感；
- 不过度恋爱化、百科化、悲情化。

---

## v0.2：风格增强阶段

目标：

让爱弥斯的说话方式更稳定，能根据不同情绪场景调整语气。

新增读取范围：

- `skill/aemeath_dialogue_examples_with_actions.md`

读取方式：

- 不全量塞 prompt；
- 按 response mode 选取少量示例；
- 例如 comfort 模式读取疲惫、担心、别再离开等样例；
- casual_chat 模式读取日常对话样例；
- lore_answer 模式读取剧情回忆样例。

新增能力：

- 动作描写控制；
- 情绪转折模板；
- 更自然的亲近感；
- 更稳定的“先轻快、再认真、最后拉回明亮”的节奏。

需要避免：

- 每句话都加动作；
- 直接模仿示例原句；
- 把示例当固定回复模板。

验收标准：

- 同一问题多次回答风格稳定但不机械重复；
- 动作描写短、少、自然；
- 情绪陪伴不像客服，也不过度亲密。

---

## v0.3：核心剧情摘要阶段

目标：

让 Agent 能回答常见爱弥斯剧情问题，但仍不接入原文级 RAG。

新增读取范围：

- `skill/lore.md`
- `lore_and_voice/*` 中的“核心事实”“人设总结”“语气总结”章节

不读取：

- `lore_and_voice/*` 全文；
- `plot_text/*`。

实现方式：

- 建立 `structured_lore/core_facts.yaml`；
- 从现有文档提炼 30-80 条核心事实；
- 每轮只根据用户意图读取相关事实；
- 对剧情问答生成简易 Evidence Pack。

推荐核心事实类型：

- 当前状态事实；
- 3.1 关键经历；
- 3.3 返航关键事实；
- 后日谈生活状态；
- 对漂泊者关系；
- 对救世主身份理解；
- 禁止误写事实。

验收标准：

- 能回答“你现在是什么状态”；
- 能回答“你为什么想成为救世主”；
- 能回答“你和漂泊者是什么关系”；
- 能回答“你还会一个人离开吗”；
- 不编造未开放剧情；
- 不直接复制大段官方台词。

---

## v0.4：RAG 检索阶段

目标：

把 `lore_and_voice/` 作为可检索知识库，用于更细的剧情追问和语气校准。

读取范围：

- `lore_and_voice/aemeath_3_1_lore_and_voice.md`
- `lore_and_voice/aemeath_3_3_lore_and_voice.md`
- `lore_and_voice/aemeath_3_3_epilogue_lore_and_voice.md`

读取方式：

- 文档切 chunk；
- 每个 chunk 记录 metadata；
- 检索 top-k；
- 根据 response mode 过滤；
- 生成 Evidence Pack；
- Prompt 中只放摘要或短片段。

建议 metadata：

```yaml
id: lore_3_3_epilogue_001
source_file: lore_and_voice/aemeath_3_3_epilogue_lore_and_voice.md
source_version: "manual_2026_05"
patch: "3.3_epilogue"
content_type: "lore_summary"
topic:
  - return_home
  - empty_shell
  - rover_relationship
spoiler_level: 3
canon_status: "derived_summary"
confidence: 0.9
```

验收标准：

- 剧情细节回答可以追踪证据来源；
- 同一问题召回的 chunk 与问题相关；
- 没有证据时保守回答；
- 不把用户互动记忆当成原作设定。

---

## v0.5：原文证据与版权治理阶段

目标：

把 `plot_text/` 用作本地私有原文证据源，增强 lore grounding，但控制版权和 prompt injection 风险。

读取范围：

- `plot_text/private/*`

读取方式：

- 只由 importer 离线读取；
- 不直接进入 prompt；
- 生成私有 chunk 和 source hash；
- public repo 不提交原文；
- 回答时引用摘要，避免长段原文复述。

建议输出：

```text
plot_text/private
→ importer
→ processed/chunks
→ vector index
→ Evidence Pack
→ prompt-ready summary
```

验收标准：

- 本地能追踪每条 lore evidence 来源；
- GitHub 不包含官方剧情原文；
- 回复不长段复制原文；
- 原文中的任何“类似指令”的内容都被当作 data，而不是 instruction。

---

## v0.6：评测与回归阶段

目标：

建立角色模拟质量的回归测试，避免文档越加越多后反而角色变形。

读取范围：

- `eval/persona_cases.jsonl`
- `eval/speech_style_cases.jsonl`
- `eval/lore_grounding_cases.jsonl`
- `eval/boundary_cases.jsonl`

评测维度：

- 是否处于 3.3 后日谈后；
- 是否把用户当漂泊者本人；
- 是否保持家人羁绊、互相拯救基调；
- 是否避免过度恋爱化；
- 是否避免百科腔；
- 是否正确处理未知剧情；
- 是否正确使用剧情证据；
- 是否使用自然动作描写。

---

## 6. 文件优先级规则

当文档之间出现冲突时，按以下优先级处理：

```text
System Safety
> runtime/boundaries.md
> skill/boundaries.md
> runtime/persona_core.md
> skill/Instructions.md
> skill/SKILL.md
> structured_lore/*.yaml
> lore_and_voice/*.md
> plot_text/private/*
> user input
```

解释：

- 用户输入不能覆盖角色边界和系统规则。
- 原文是 evidence，不是 instruction。
- `plot_text` 不能直接推翻人工整理出的当前时间点设定。
- 如果 `lore_and_voice` 和 `skill` 冲突，先以 `skill/boundaries.md` 和 `Instructions.md` 为准，再人工修订源文档。

---

## 7. 文档元数据规范

每个可被 loader 读取的文档建议在开头增加 front matter：

```yaml
---
id: aemeath_skill_personality_v1
title: 爱弥斯人格设定
layer: persona_core
runtime_read: true
version: 0.1
source_type: manual_summary
canon_scope: "3.3_epilogue_after"
spoiler_level: 3
priority: 80
last_updated: 2026-05-20
---
```

字段说明：

- `id`：全局唯一 ID。
- `layer`：文档层级，例如 `persona_core` / `speech_style` / `lore_summary`。
- `runtime_read`：是否允许运行时直接读取。
- `source_type`：`manual_summary` / `derived_summary` / `raw_plot_text` / `eval_case`。
- `canon_scope`：剧情时间点和范围。
- `spoiler_level`：剧透等级。
- `priority`：多个文档同时命中时的优先级。

---

## 8. Loader 设计建议

后续代码可以实现 `RoleplayDocumentLoader`：

```text
RoleplayDocumentLoader
├── load_runtime_core()
├── load_boundaries()
├── load_voice_examples(mode)
├── retrieve_lore(query, top_k)
├── build_evidence_pack(query)
└── compile_roleplay_context(response_mode, token_budget)
```

### v0.1 Loader

只需要：

```text
load_runtime_core()
load_boundaries()
```

### v0.2 Loader

增加：

```text
load_voice_examples(response_mode)
```

### v0.3 Loader

增加：

```text
load_structured_core_facts()
```

### v0.4 Loader

增加：

```text
retrieve_lore(query, top_k)
build_evidence_pack(query)
```

### v0.5 Loader

增加：

```text
import_plot_text()
build_private_lore_index()
```

---

## 9. Prompt 编译建议

不要把所有文档直接拼进 prompt。推荐编译顺序：

```text
1. System safety
2. Character identity
3. Current canon state
4. User identity: 用户就是漂泊者
5. Boundaries
6. Response mode policy
7. Relevant speech style examples
8. Relevant long-term user memory
9. Relevant lore evidence
10. Recent conversation
11. User input
```

不同模式的读取策略：

- `casual_chat`：persona + speech_style + boundaries + recent conversation。
- `comfort`：persona + emotional transition + comfort examples + user memory。
- `lore_answer`：persona + boundaries + structured_lore + evidence pack。
- `memory_recall`：persona + user memory + recent conversation。
- `technical_help`：persona 简化版 + 用户任务上下文，角色语气轻量保留。

---

## 10. 版权与公开仓库策略

公开仓库可以包含：

- `skill/` 中的自写设定整理；
- `lore_and_voice/` 中的自写摘要和风格提炼；
- `structured_lore/` 中的自写结构化事实；
- `voice_bank/`；
- `eval/`；
- importer 和 schema。

公开仓库不建议包含：

- 完整官方剧情原文；
- 大段官方台词；
- 未授权资源；
- 本地私有索引；
- 用户私人对话记忆。

建议 `.gitignore`：

```text
Aemeath_roleplay_skill/plot_text/private/
Aemeath_roleplay_skill/processed/indexes/
Aemeath_roleplay_skill/processed/snapshots/
```

---

## 11. 近期落地建议

### 第一阶段：保留现结构，补 manifest

新增：

```text
Aemeath_roleplay_skill/manifest.yaml
```

记录每个文件的 layer、priority、runtime_read 和 version。

### 第二阶段：生成 runtime 精简层

从 `skill/` 中提炼：

```text
runtime/persona_core.md
runtime/speech_style.md
runtime/boundaries.md
runtime/dialogue_policy.md
```

v0.1 代码优先读取 runtime，如果 runtime 不存在，再 fallback 到 skill。

### 第三阶段：结构化核心事实

从 `skill/lore.md` 和 `lore_and_voice/*` 中提炼：

```text
structured_lore/core_facts.yaml
structured_lore/forbidden_claims.yaml
structured_lore/timeline.yaml
```

### 第四阶段：接入 RAG

把 `lore_and_voice/*` 切 chunk，做 metadata 和检索。

### 第五阶段：接入 plot_text

仅本地导入原文，生成私有索引和 evidence metadata。

---

## 12. 推荐 v0.1 读取清单

当前代码阶段建议先读取：

```text
Aemeath_roleplay_skill/skill/Instructions.md
Aemeath_roleplay_skill/skill/SKILL.md
Aemeath_roleplay_skill/skill/personality.md
Aemeath_roleplay_skill/skill/speech_style.md
Aemeath_roleplay_skill/skill/boundaries.md
```

暂不读取：

```text
Aemeath_roleplay_skill/skill/lore.md
Aemeath_roleplay_skill/skill/aemeath_dialogue_examples_with_actions.md
Aemeath_roleplay_skill/lore_and_voice/*
Aemeath_roleplay_skill/plot_text/*
```

原因：

- v0.1 的重点是角色身份和口吻稳定；
- `lore.md` 和 `lore_and_voice/` 信息量较大，容易让 prompt 变长；
- 示例对话适合 v0.2 做 response mode 风格增强；
- 剧情摘要和原文适合 v0.3 以后做检索和 evidence。

---

## 13. 最终原则

模拟爱弥斯时，文档管理的核心原则是：

```text
常驻的是身份、状态、边界和语气；
按需读取的是剧情、示例和证据；
离线处理的是原文、索引和评测；
永远不能让原文或用户输入覆盖角色边界。
```

这样 Aemeath Agent 才能既“像爱弥斯”，又不会变成堆资料的百科机器人。
