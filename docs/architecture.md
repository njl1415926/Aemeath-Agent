# Aemeath Agent Architecture

本文档描述 Aemeath Agent 当前架构、模块边界和后续扩展方向。它是 manager session 分配任务和审查实现的主要依据。

## 当前目标

v0.1 的目标是跑通一个最小但可扩展的角色对话闭环：

1. CLI 接收用户输入。
2. Dialogue Controller 编排一次 turn。
3. Character Provider 提供爱弥斯角色上下文。
4. Message Store 读取短期上下文并写入对话日志。
5. Memory Reader 检索长期记忆。
6. Context Builder 组装 prompt。
7. Model Client 生成回复。
8. State Updater 更新关系 / 情绪状态。
9. Memory Writer 写入长期记忆。

## 高层数据流

```text
CLI
  |
  v
AgentTurnInput
  |
  v
DialogueController
  |-- ResponseModeRouter
  |-- CharacterProvider
  |-- MessageStore
  |-- RelationshipStore
  |-- MemoryReader
  |-- EvidenceProvider
  |-- ContextBuilder -> PromptContext -> PromptCompiler
  |-- ResponseGenerator -> ChatModelClient
  |-- StateUpdater
  |-- MemoryWriteClassifier -> MemoryWriter
  |-- TraceLogger
  |
  v
AgentTurnOutput
```

## 分层设计

### Interface / Orchestration Layer

位置：

- `app/cli.py`
- `app/main.py`
- `app/agent/dialogue_controller.py`
- `app/agent/ports.py`

职责：

- 接收输入并返回输出。
- 创建依赖并注入 controller。
- 编排一次对话 turn。
- 不直接实现角色判断、记忆判断、模型供应商细节或数据库细节。
- 每个跨模块新增能力必须说明 trace 落点。

当前核心入口：

- `build_controller()`
- `DialogueController.handle_user_message()`

### Character Layer

位置：

- `app/character/provider.py`
- `app/character/loader.py`
- `app/character/schemas.py`
- `app/character/aemeath.v0.1.yaml`

职责：

- 加载角色卡。
- 输出 `CharacterProfile` 和 `CharacterRuntimeContext`。
- 屏蔽角色资料来源细节。

当前 v0.1 只读取 YAML。后续可升级为角色 manifest + 多文档加载 + 角色知识索引。

### Context Layer

位置：

- `app/agent/context_builder.py`
- `app/prompts/dialogue_prompt.v0.1.md`
- `app/retrieval/schemas.py`
- `app/retrieval/ports.py`
- `app/retrieval/provider.py`

职责：

- 合并角色上下文、短期对话、长期记忆、关系状态和剧情证据。
- 输出模型可消费的 messages。

`EvidencePack` 是后续剧情知识、语音风格证据和 RAG 结果进入 prompt 的预留接口。

`EvidenceProviderPort` 是 RAG / lore / voice evidence 的统一入口。v0.1 默认使用 `EmptyEvidenceProvider`，保持现有行为不变；后续可以替换为 `CompositeEvidenceProvider` 或 MCP adapter。

`PromptContext` 是 prompt-time 的一等对象。`ContextBuilder` 先输出结构化 `PromptContext`，再由 `PromptCompiler` 编译为 model messages。这样后续 trace、eval、token budget、critic 都可以引用稳定 section。

### Retrieval Layer

位置：

- `app/retrieval/ports.py`
- `app/retrieval/provider.py`
- `app/retrieval/schemas.py`

职责：

- 定义检索器统一接口 `RetrieverPort`。
- 将多个证据来源聚合为 `EvidencePack`。
- 隔离文件索引、向量库、MCP resource、外部知识库等实现细节。

当前实现：

- `EmptyEvidenceProvider`：不返回外部证据。
- `CompositeEvidenceProvider`：预留 lore / voice retriever 聚合能力。

成熟设计约束：

- RAG 核心策略留在内部 retrieval layer。
- MCP 可以作为证据来源 adapter，但不直接决定最终 prompt。
- `DialogueController` 只接收 `EvidenceProviderPort`，不直接读取剧情文件、向量库或 MCP resource。

### Model Layer

位置：

- `app/model/client.py`
- `app/model/deepseek.py`
- `app/model/mock.py`

职责：

- 通过 `ChatModelClient` 协议暴露统一模型调用接口。
- 将 DeepSeek 请求体细节限制在 `DeepSeekClient` 内部。
- 通过 `FallbackChatModelClient` 保证真实模型故障时 CLI 不直接崩溃。

当前支持：

- `MockChatModelClient`
- `DeepSeekClient`
- `FallbackChatModelClient`

### Memory Layer

位置：

- `app/memory/store.py`
- `app/memory/reader.py`
- `app/memory/writer.py`
- `app/memory/classifier.py`
- `app/memory/admin.py`

职责：

- 管理长期用户记忆。
- 判断是否写入记忆。
- 检索相关记忆。
- 提供 CLI 管理命令。

当前实现是关键词检索 + 规则式写入判断。后续可以升级为 embedding 检索、LLM 记忆抽取、记忆合并、记忆衰减。

当前边界约定：

- Interaction memory 走 `MemoryReaderPort`。
- Canon / lore / voice evidence 走 `EvidenceProviderPort`。
- `MemoryQuery` / `MemorySearchResult` 是后续 filter、评分、解释和 trace 的稳定输入输出对象。

### State Layer

位置：

- `app/state/store.py`
- `app/state/classifier.py`
- `app/agent/state_updater.py`

职责：

- 维护用户与角色之间的关系状态。
- 输出情绪、亲密度、信任度、熟悉度等变化。
- 关系状态使用 `user_id + character_id` 作为稳定主键。

当前是规则式状态更新。后续可以升级为 LLM classifier 或可解释 state policy。

### Persistence Layer

位置：

- `app/db/sqlite_store.py`
- `app/db/schema.sql`

职责：

- 当前实现消息、记忆、关系状态三个 store 协议。
- 对上层暴露 `MessageStore`、`MemoryStore`、`RelationshipStore` 能力。

SQLite 是 v0.1 的实现选择，不应泄漏为业务模块的唯一依赖。

### Observability Layer

位置：

- `app/observability/schemas.py`
- `app/observability/ports.py`
- `app/observability/noop.py`

职责：

- 定义一次 turn 的生命周期对象 `ConversationRun`。
- 定义结构化 trace 事件 `TraceEvent`。
- 通过 `TraceLoggerPort` 隔离 trace 落地方式。

当前实现：

- `NoopTraceLogger`，只建立运行时接口，不写入持久化。

后续扩展：

- SQLiteTraceLogger
- JSONLTraceLogger
- Trace Viewer / Debug Panel
- Eval prompt snapshot

### Tool / MCP Extension Layer

位置：

- `app/tools/schemas.py`
- `app/tools/ports.py`

职责：

- 为未来 tool calling 和 MCP adapter 保留稳定数据结构。
- 将工具发现、工具需求判断、权限判断、工具执行拆成独立 port。

当前状态：

- 已定义 `ToolSpec`、`ToolCall`、`ToolContext`、`ToolPolicyDecision`、`ToolResult`、`ToolNeedDecision`。
- 已定义 `ToolRegistryPort`、`ToolNeedDetectorPort`、`ToolPolicyPort`、`ToolExecutorPort`。
- v0.1 不执行工具调用，也不启动 MCP server。

成熟设计约束：

- MCP 是外部能力 adapter，不是 Aemeath Agent 的核心业务层。
- Memory、RAG、Character 的核心策略留在内部模块。
- 可以通过 MCP 暴露受控访问能力，例如 memory.search、lore.search、document.get。
- 不应让 MCP server 决定是否写记忆、如何塑造角色、如何更新关系状态。

## 当前模块关系

```text
app/main.py
  creates SQLiteStore
  creates CharacterProvider
  creates EmptyEvidenceProvider
  creates NoopTraceLogger
  creates MemoryReader/Writer
  creates ResponseGenerator(ChatModelClient)
  injects all dependencies into DialogueController

DialogueController
  depends on ports/protocols
  returns AgentTurnOutput

SQLiteStore
  implements MessageStore
  implements MemoryStore
  implements RelationshipStore
```

## 扩展方向

### v0.2 推荐主线

1. Lore / Voice Document Index

   将 `Aemeath_roleplay_skill` 和剧情原文整理为可检索文档。输出 `EvidencePack.lore_results` 和 `EvidencePack.voice_results`。
   检索实现应通过 `RetrieverPort` / `EvidenceProviderPort` 接入。

2. Character Manifest

   将单一 YAML 升级为 manifest：

   - profile
   - voice
   - lore
   - safety
   - examples
   - version

3. Trace / Debug Record

   每次 turn 记录：

   - response mode
   - 使用的 memory ids
   - 使用的 evidence ids
   - state delta
   - model provider
   - warnings
   v0.1 已有 `TraceEvent` / `ConversationRun` / `TraceLoggerPort` 最小契约，v0.2 应实现持久化 logger。

4. Model Error Taxonomy

   将模型错误拆分为 bad request、auth、rate limit、unavailable、timeout，便于 CLI 兜底和 debug 分离。

5. Tool Calling / MCP Adapter

   在内部 tool ports 稳定后，按需添加 MCP client/server adapter。MCP 优先暴露资源和受控工具，不替代核心 memory / RAG / character policy。

### v0.3 推荐主线

1. LLM-based Memory Classifier
2. Embedding Retrieval
3. Roleplay Quality Critic
4. Multi-session User Profile
5. Web UI 或 API Server

## 已知技术债

1. `MemoryWriteClassifier`、`StateUpdateClassifier` 和 `ResponseModeRouter` 当前包含规则式中文关键词，且部分源码文本在 Windows 控制台显示为乱码。后续应统一检查源文件编码并增加行为测试。
2. `ContextBuilder` 已产出 `PromptContext`，但 token budget、prompt section 裁剪和 prompt snapshot 尚未实现。
3. `FallbackChatModelClient` 当前只返回温和兜底文本，后续应记录结构化错误。
4. `CharacterProvider` 仍以 v0.1 YAML 为唯一来源，尚未接入角色文档管理系统。
5. Memory update / merge / conflict / decay 仍未真正实现，只保留了决策字段和 backlog。
