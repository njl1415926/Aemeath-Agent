# Aemeath Agent 架构审查报告

审查时间：2026-05-20

审查范围：

- `AGENTS.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- 核心接口文件抽查：`app/schemas.py`、`app/agent/dialogue_controller.py`、`app/agent/ports.py`、`app/retrieval/*`、`app/memory/*`、`app/tools/*`

本报告只做架构审查与改进建议，不包含代码修改。

## 1. 总体评价

当前 Aemeath Agent 架构方向合理。项目已经形成了“Controller 编排 + port 注入 + 业务模块隔离 + SQLite 作为 v0.1 adapter”的基本骨架，适合多 session 协作继续推进。

主要优点是：最小对话闭环清晰，Memory、State、Character、Model、Retrieval、Tool/MCP 都有初步边界。主要风险是：Runtime / Trace / PromptContext 还没有形成一等对象，部分接口命名不统一，Memory 的读写、更新、冲突、溯源能力还偏薄。如果直接扩 Lore RAG、MCP、Web UI、Eval，会跑得动，但可观测性和接口稳定性会很快成为瓶颈。

结论：不建议推倒重构；建议在继续实现大功能前，优先补齐 `PromptContext`、`TraceEvent / ConversationRun`、`MemoryQuery / MemorySearchResult` 三类核心契约。

## 2. 当前架构的主要优点

1. `DialogueController` 被定位为编排层，AGENTS.md 和架构文档都明确禁止 controller 承载业务判断。
2. 关键依赖已经 port 化，包括 `MemoryReaderPort`、`MemoryWriterPort`、`MemoryWriteClassifierPort`、`StateUpdaterPort`、`ContextBuilderPort`、`ResponseGeneratorPort` 等，便于 mock 和多 session 并行开发。
3. SQLite 被明确为 v0.1 实现，而不是长期边界，后续替换向量库、Postgres 或混合存储时不应污染业务层。
4. Memory write path 已拆成 classifier 和 writer，`MemoryDecision` 也预留了 `operation`、`namespace`、`memory_type`、`importance`、`confidence`、`source`、`reason` 等关键字段。
5. Lore / Voice / MCP 方向已经通过 `RetrieverPort`、`EvidenceProviderPort`、`EvidencePack` 预留入口，没有把 MCP 误放成核心业务层。
6. Tool calling 预留了 registry、need detector、policy、executor 四类 port，权限判断没有和执行混在一起。
7. `docs/task_log.md` 的任务拆分清楚，已经适合 manager 分配 worker。

## 3. 关键问题与风险

### High Priority

#### 3.1 Trace / ConversationRun 缺位，完整闭环不可审计

- 问题描述：当前 turn 里会检索 memory、构造 prompt、调用模型、更新 state、写 memory，但没有统一的 `ConversationRun`、`TraceEvent` 或 `TraceLogger`。
- 影响范围：Runtime、Memory、Prompt、Eval、Debug、Web UI。
- 可能后果：无法回答“为什么写入这条记忆”“用了哪些 evidence”“prompt 由哪些部分组成”“fallback 原因是什么”。
- 建议改进：优先推进 T-0103，并把 trace 抽象为 port：`TraceLoggerPort.record(event: TraceEvent)`。同时增加 `ConversationRun`，包含 `turn_id`、`user_id`、`session_id`、`character_id`、`started_at`、`ended_at`、`status`、`warnings`。

#### 3.2 接口文档漏列 `EvidenceProviderPort`

- 问题描述：`docs/interfaces.md` 的 `Dialogue Controller Ports` 列表没有包含 `EvidenceProviderPort`，但 `app/agent/dialogue_controller.py` 实际已经注入并调用。
- 影响范围：Retrieval worker、MCP worker、架构审查。
- 可能后果：worker 误以为 evidence provider 不属于 controller 稳定依赖，后续容易绕过统一入口。
- 建议改进：在 `docs/interfaces.md` 的 controller ports 列表中补上 `EvidenceProviderPort`，并说明它是 RAG / Lore / MCP resource 的唯一入口。

#### 3.3 PromptContext 不是一等对象

- 问题描述：文档强调 Prompt context 是系统集成点，但接口层没有定义 `PromptContext`。当前 `ContextBuilderPort.build_messages(...) -> list[dict[str, str]]` 直接输出模型 messages。
- 影响范围：Prompt Compiler、Trace、Eval、Token budget、Streaming UI、Critic。
- 可能后果：后续很难追踪 prompt section、做 token 裁剪、解释每段上下文来源。
- 建议改进：新增 `PromptContext` 和 `PromptSection`。`ContextBuilder` 先产出结构化 context，再由 `PromptCompiler` 编译为 model messages。

#### 3.4 Memory update / merge 被接口名义支持，但 store / writer 能力不完整

- 问题描述：`MemoryDecision.operation` 支持 `update`、`merge`、`archive`，但 `MemoryStore` 当前没有明确 `update_memory` / `merge_memories` 方法，`MemoryWriter.persist_decision` 对 `update`、`merge` 实际仍走 create。
- 影响范围：Memory consistency、冲突解决、长期记忆质量。
- 可能后果：系统会不断 append，难以修正旧记忆，也难以处理用户偏好变化。
- 建议改进：短期先把 `operation` 实际支持范围写清楚；中期增加 `MemoryUpdatePlan`、`MemoryConflictResolverPort`、`update_memory`、`merge_memories`。

### Medium Priority

#### 3.5 `character_id` 与 `character_name` 混用

- 问题描述：`AgentTurnInput` 使用 `character_id`，`RelationshipStore` 使用 `character_name`，controller 以 `display_name` 查询关系状态。
- 影响范围：Multi-character support、角色改名、本地化显示名。
- 可能后果：同一角色 display name 改变后关系状态断裂；多角色时难以唯一定位。
- 建议改进：关系状态主键改为 `user_id + character_id`，`character_name / display_name` 只作为展示字段或 metadata。

#### 3.6 `EvidencePack.memory_results` 与 `MemoryReader` 的关系不清楚

- 问题描述：当前长期互动记忆通过 `MemoryReader` 单独进入 `ContextBuilder`，同时 `EvidencePack` 又有 `memory_results` 字段。
- 影响范围：Memory RAG、Lore RAG、ContextBuilder、Trace。
- 可能后果：未来可能出现两条 memory read path：一个从 memory reader，一个从 evidence provider，导致重复召回或语义冲突。
- 建议改进：明确规则：Interaction Memory 走 `MemoryReaderPort`；Canon / Lore / Voice 走 `EvidenceProviderPort`。如果保留 `EvidencePack.memory_results`，应说明它只用于外部 memory evidence 或统一检索聚合。

#### 3.7 Model interface 太薄

- 问题描述：`ChatModelClient.chat(...) -> str` 无法返回 provider、model、token、latency、finish_reason、raw_error、tool_calls。
- 影响范围：Model Router、Trace、Eval、Streaming、Tool calling。
- 可能后果：后续加入模型路由、成本统计、失败降级时需要改核心接口。
- 建议改进：中期新增 `ModelRequest` / `ModelResponse`，保留兼容 adapter。`ModelResponse` 至少包含 `text`、`model`、`provider`、`usage`、`latency_ms`、`finish_reason`、`warnings`。

#### 3.8 Critic / Rewriter 只有路线，没有 runtime 扩展点

- 问题描述：文档提到 Roleplay Quality Critic，但当前 controller 流程没有 critic / rewrite hook。
- 影响范围：人格一致性、Lore safety、回复质量控制。
- 可能后果：后续加 critic 时可能直接塞进 controller，破坏“只编排”的边界。
- 建议改进：预留 `ResponseCriticPort`、`ResponseRewriterPort`，但默认 Noop。先只定义 hook 位置，不急着实现复杂策略。

### Low Priority

#### 3.9 Session 概念没有一等接口

- 问题描述：有 `session_id` 和 message history，但没有 `Session` / `SessionState` / `SessionStore`。
- 影响范围：session resume、Web UI、多设备、多会话列表。
- 建议改进：Web / API 前新增 `SessionRecord`，记录 `session_id`、`user_id`、`character_id`、`title`、`created_at`、`updated_at`、`status`。

#### 3.10 ContextBuilder 暂未使用 voice evidence

- 问题描述：`EvidencePack` 有 `voice_results`，但当前 prompt 构建主要展示 `lore_results`。
- 影响范围：角色语气一致性。
- 建议改进：T-0102 中明确 `voice_results` 的 prompt section 和 token budget。

## 4. 模块职责与解耦性审查

职责较清晰的模块：

- Character Layer：负责角色资料加载和 runtime context 输出，边界合理。
- Model Layer：供应商细节被限制在 client 内，方向正确。
- Tool / MCP Layer：registry / detector / policy / executor 拆分良好。
- Persistence Layer：SQLite 作为 adapter，文档定位正确。

存在模糊或需要收紧的模块：

- `DialogueController`：当前仍是线性流程编排，可接受，但后续加 trace、critic、tool、streaming 时会膨胀。建议尽快引入 turn lifecycle / runtime step 概念。
- `ContextBuilder`：目前同时承担 context 汇合、prompt section 排版、model message 输出，未来会和 Prompt Compiler 重叠。
- Memory Layer：reader / writer / classifier 边界初步合理，但 update / merge / conflict / decay 还没有真实职责落点。
- Retrieval Layer：EvidenceProvider 很好，但需明确它与 Interaction Memory 的边界，避免 Lore 和用户记忆混读。

## 5. 接口设计审查

已定义较清楚的对象：

- `AgentTurnInput`
- `AgentTurnOutput`
- `RelationshipState`
- `MemoryDecision`
- `MemoryRecord`
- `RetrievalResult`
- `EvidencePack`
- `ToolSpec` / `ToolCall` / `ToolResult`

当前文档未说明或说明不足的对象：

- `Message`：代码里是 `MessageRecord`，文档问题列表里叫 Message，建议统一命名。
- `Session`：当前文档未定义。
- `AgentState`：当前文档未定义。只有 `RelationshipState`，缺少更高层 runtime state。
- `MemoryCandidate`：当前文档未定义。建议用于 classifier 输出前的候选抽取。
- `MemoryQuery` / `MemorySearchResult`：当前文档未定义。现在 reader 只收 `query: str`，不利于 namespace / type / filter / limit / reason 追踪。
- `PromptContext`：当前文档未定义，是高优先级缺口。
- `TraceEvent`：当前文档未定义，只在 backlog 里有 trace 任务。
- `ToolSpec`：已定义，但还缺少权限 scope、side effect level、rate limit、confirmation copy 等字段。

命名不统一点：

- `character_id` vs `character_name`
- `Message` vs `MessageRecord`
- `MemoryDecision.source_message_ids` vs `MemoryWriter.persist_decision(source_message_id=...)`
- `EvidencePack.memory_results` vs `MemoryReader.retrieve(...)`

## 6. Memory 系统专项审查

当前 Memory 设计已经有不错的起点：区分了 read path (`MemoryReader`) 和 write path (`MemoryWriteClassifier` + `MemoryWriter`)；`MemoryRecord` 有 `namespace`、`memory_type`、`importance`、`confidence`、`source`、`created_at`、`updated_at`、`decay_score`、`expiry` 等字段。

关键不足：

- Canon Lore Memory 与 Interaction Memory 在文档原则上隔离，但接口层没有明确 `namespace` 枚举或 policy。
- `MemoryDecision.reason` 有解释字段，但没有 `MemoryWriteDecisionLog`，解释不会稳定进入 trace。
- update / merge / conflict resolver 还只是字段预留，没有实际 port。
- decay / forgetting 有 `decay_score` 字段，但没有 `MemoryDecayPolicyPort` 或调度入口。
- 防止“过度记忆”目前依赖规则式 classifier 和精确去重，缺少 cooldown、sensitivity、用户确认、同类记忆合并。
- 防止“用户互动记忆误当原作剧情”需要强制 namespace / source_type 规则，例如 `interaction:user_profile`、`interaction:preference`、`canon:lore`、`canon:voice`。

建议补充：

- `MemoryNamespace` / `MemoryType` 常量或枚举。
- `MemoryQuery`：包含 `user_id`、`character_id`、`namespaces`、`memory_types`、`query_text`、`limit`、`min_confidence`。
- `MemorySearchResult`：包含 `record`、`score`、`match_reason`。
- `MemoryConflictResolverPort`
- `MemoryDecayPolicyPort`
- `MemoryWriteDecisionLog`

## 7. Agent Runtime 专项审查

当前系统是一个清晰的线性 turn pipeline，已经能支撑 v0.1 多轮对话闭环：

```text
User Input
  -> response mode
  -> character context
  -> save user message
  -> recent messages
  -> relationship state
  -> memory retrieve
  -> evidence pack
  -> prompt messages
  -> model response
  -> save assistant message
  -> state update
  -> memory decision/write
  -> output
```

但它还不是完整 Agent Runtime，主要缺少：

- 明确的一轮生命周期对象：`ConversationRun` / `TurnRun`
- step 级状态：started、completed、failed、skipped
- trace hook
- critic / rewriter hook
- tool calling hook
- partial failure 降级策略
- session resume 策略
- streaming response mode

建议演进顺序：

1. 先加 `ConversationRun` 和 `TraceLoggerPort`。
2. 再把 controller 内部步骤拆成可追踪 step，但不急着上 graph。
3. 然后加入 Noop critic / rewriter / tool hook。
4. 最后再考虑状态机或 graph runtime。

## 8. 可扩展性审查

- Lore RAG：已预留 `RetrieverPort` / `EvidenceProviderPort`，容易接入；需要补 PromptContext 和 trace。
- Evidence Pack：已有基础结构；需要补 `source_id`、`score`、`citation`、`token_cost`、`selected_reason`。
- Persona Critic：当前无 hook；需要新增 critic port。
- Lore Critic：当前无 hook；建议复用 critic port，但输入应包含 `EvidencePack`。
- Memory Conflict Resolver：当前只预留 operation；需要新增 resolver port 和 store update 能力。
- Memory Decay：字段有，策略无；需要新增 decay policy 和后台 / 启动时维护入口。
- Trace Viewer：当前 trace 缺位；先做结构化 trace，再做 viewer。
- Memory Admin Panel：`memory/admin.py` 已有 CLI 管理迹象，但 Web panel 需要 Memory API 和审计字段。
- MCP Server：tool / retrieval ports 方向正确；不要让 MCP 直接改 core policy。
- FastAPI Backend：需要 `Session`、`ConversationRun`、streaming-friendly response contract。
- Web Chat UI：当前可接 CLI 上层，但缺 session list、trace/debug API、streaming。
- AG-UI / Streaming UI：当前 `ChatModelClient` 返回 str，不适配；需要 streaming model response。
- Multi-character support：`character_id` / `character_name` 混用会卡，需要先修。
- Model Router：当前 model interface 太薄，需要 `ModelRequest` / `ModelResponse`。
- Evaluation Benchmark：需要 trace、prompt snapshot、expected behavior cases、memory fixture。

## 9. 建议的架构改进路线

### 短期改进

1. 更新 `docs/interfaces.md`：补 `EvidenceProviderPort` 到 controller ports。
2. 明确 `character_id` 是关系状态和记忆隔离的稳定 key。
3. 定义 `TraceEvent`、`ConversationRun`、`PromptContext` 的最小 schema。
4. 给 `EvidencePack.memory_results` 写清楚用途，避免和 `MemoryReader` 双路径冲突。
5. 在 `docs/task_log.md` 新增 memory namespace / type 规范任务。

### 中期改进

1. 拆 `ContextBuilder` 为 `ContextBuilder` + `PromptCompiler`。
2. 新增 `ModelResponse`，为 token、latency、provider、fallback、streaming 做准备。
3. 完成 `MemoryQuery` / `MemorySearchResult` / `MemoryConflictResolverPort`。
4. 为 critic / rewrite 增加 Noop port 和 trace 记录。
5. 推进 Lore / Voice Retriever，并将 evidence selection 写入 trace。

### 长期改进

1. 将 controller 演进为轻量 Agent Runtime 或 state graph。
2. 支持多角色 manifest、角色知识索引和角色版本管理。
3. 建立 eval benchmark：人格一致性、lore grounding、memory precision、过度记忆率。
4. 增加 Trace Viewer、Memory Admin Panel、FastAPI / Web UI。
5. 引入 Model Router、Tool Runtime、MCP server / client adapter。

## 10. 建议修改的文档或接口

建议优先更新：

- `docs/interfaces.md`：补 `EvidenceProviderPort`、`PromptContext`、`TraceEvent`、`Session`、`MemoryQuery`。
- `docs/architecture.md`：补完整 runtime lifecycle 和 trace 数据流。
- `docs/task_log.md`：新增短期任务，例如 `T-0108 Runtime Trace Contract`、`T-0109 Memory Namespace Policy`。
- 新增 `docs/memory_design.md`：专门定义 interaction memory、canon lore、namespace、conflict、decay。
- 新增 `docs/runtime_design.md`：定义 turn lifecycle、step、failure policy、critic / tool hooks。
- `AGENTS.md`：可补一句“跨模块新增能力必须说明 trace 落点”。

## 11. 最终结论

建议继续按当前架构推进，不需要推倒重构。这个 v0.1 骨架是健康的，尤其是 port 化和 adapter 边界做得比较早，值得保留。

但在开始大规模做 Lore RAG、MCP、Web UI、Eval 之前，应先补三个核心契约：`PromptContext`、`TraceEvent / ConversationRun`、`MemoryQuery / MemorySearchResult`。这三块补齐后，后续扩展会主要是新增模块，而不是反复拆 controller 和重写接口。
