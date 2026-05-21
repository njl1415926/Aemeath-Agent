# Aemeath Agent Interfaces

本文档定义 Aemeath Agent 当前稳定接口和后续扩展约束。Worker session 实现功能时应优先依赖这些接口，而不是直接耦合具体实现。

## Core Turn Contract

位置：

- `app/schemas.py`

### AgentTurnInput

```python
@dataclass
class AgentTurnInput:
    user_id: str
    session_id: str
    character_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

含义：

- `user_id`：用户标识。
- `session_id`：对话 session 标识。
- `character_id`：目标角色标识，当前默认 `aemeath`。
- `message`：用户本轮输入。
- `metadata`：预留给 UI、渠道、实验参数和 trace 信息。

### AgentTurnOutput

```python
@dataclass
class AgentTurnOutput:
    turn_id: str
    session_id: str
    response: str
    response_mode: str
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    warnings: list[str] = field(default_factory=list)
```

含义：

- `response`：模型或 fallback 生成的角色回复。
- `response_mode`：本轮对话模式。
- `used_memory_ids`：参与本轮上下文的长期记忆。
- `state_delta`：关系状态变化。
- `warnings`：非致命问题，例如记忆去重、模型 fallback。

## Dialogue Controller Ports

位置：

- `app/agent/ports.py`
- `app/agent/dialogue_controller.py`

`DialogueController` 是编排者，只依赖以下 ports：

- `ResponseModeRouterPort`
- `CharacterProviderPort`
- `EvidenceProviderPort`
- `MemoryReaderPort`
- `MemoryWriterPort`
- `MemoryWriteClassifierPort`
- `StateUpdaterPort`
- `ContextBuilderPort`
- `ResponseGeneratorPort`
- `TraceLoggerPort`
- `MessageStore`
- `RelationshipStore`

新增功能应优先实现这些 port，或在 manager session 评审后新增 port。

## Store Interfaces

### MessageStore

位置：

- `app/agent/message_store.py`

职责：

- 保存用户和 assistant 消息。
- 读取 session 的最近消息。
- 列出 session 历史消息。

当前实现：

- `app/db/sqlite_store.py`

### MemoryStore

位置：

- `app/memory/store.py`

职责：

- 创建长期记忆。
- 列出、搜索、归档、禁用记忆。
- 标记记忆被使用。
- 进行精确去重。

当前实现：

- `app/db/sqlite_store.py`

扩展约束：

- embedding 检索可以新增方法或新增 `MemoryRetrieverPort`，不要把向量库细节暴露给 `DialogueController`。
- 记忆合并、冲突解决、衰减策略应放在 memory layer，不应放进 controller。

### RelationshipStore

位置：

- `app/state/store.py`

职责：

- 获取或创建用户与角色关系状态。
- 按 delta 更新关系状态。

当前实现：

- `app/db/sqlite_store.py`

扩展约束：

- 新增关系维度时先扩展 `RelationshipState`，再更新 store schema 和 state updater。
- 关系状态的稳定主键是 `user_id + character_id`。
- `display_name` / `character_name` 只能作为展示字段或迁移兼容字段，不应作为长期业务主键。

## Prompt Context Interfaces

位置：

- `app/agent/prompt_context.py`
- `app/agent/context_builder.py`

### PromptSection

结构化 prompt 段落：

- `id`
- `title`
- `content_lines`
- `source_ids`
- `metadata`

### PromptContext

一次模型调用前的结构化上下文：

- `response_mode`
- `user_input`
- `sections`
- `metadata`

### PromptCompiler

将 `PromptContext` 编译为模型 messages。

当前 `ContextBuilder.build_messages()` 保持兼容旧调用，但内部流程已经变为：

```text
ContextBuilder.build_context()
  -> PromptContext
  -> PromptCompiler.compile_messages()
  -> list[dict[str, str]]
```

扩展约束：

- 未来 token budget、prompt snapshot、eval、critic、trace 都应优先读取 `PromptContext`。
- 新增 prompt section 时应有稳定 `section.id`，便于 trace 和 eval 引用。
- `ContextBuilder` 负责汇聚上下文，`PromptCompiler` 负责输出模型消息格式。
- `DialogueController` 应显式调用 `build_context()`，记录 prompt section 摘要后再编译 messages，避免 `PromptContext` 只停留在内部实现。

## Character Interfaces

位置：

- `app/character/schemas.py`
- `app/character/provider.py`

### CharacterProfile

用于角色元信息：

- `character_id`
- `display_name`
- `english_name`
- `source`
- `version`
- `canon_point`
- `roleplay_pack_path`

### CharacterRuntimeContext

用于 prompt-time 角色上下文：

- `identity`
- `relation_to_user`
- `persona_rules`
- `speech_rules`
- `boundaries`
- `avoid`
- `style_examples`
- `forbidden_claims`
- `source_doc_ids`

扩展约束：

- 新角色资料来源应被 `CharacterProvider` 吸收，不应让 `DialogueController` 直接读取角色文档。
- 剧情证据和语音证据优先通过 `EvidencePack` 进入 `ContextBuilder`。
- `source_doc_ids` 应用于 trace 和角色行为审查。

## Retrieval Interfaces

位置：

- `app/retrieval/ports.py`
- `app/retrieval/schemas.py`
- `app/retrieval/provider.py`

### RetrieverPort

统一检索器接口：

```python
class RetrieverPort(Protocol):
    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 8,
        metadata: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        ...
```

用途：

- lore retriever
- plot retriever
- voice example retriever
- external document retriever
- MCP resource adapter

约束：

- `RetrieverPort` 返回 `RetrievalResult`，不返回 provider 私有对象。
- 向量库、文件路径、MCP resource uri 等细节应放在 `metadata` 或 adapter 内部，不泄漏到 `DialogueController`。

### EvidenceProviderPort

统一证据聚合接口：

```python
class EvidenceProviderPort(Protocol):
    def build_evidence_pack(
        self,
        *,
        user_id: str,
        character_id: str,
        query: str,
        response_mode: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvidencePack:
        ...
```

用途：

- 聚合 lore、plot、voice、外部资源等证据。
- 作为 `DialogueController` 到 RAG 系统的唯一入口。
- 作为未来 MCP adapter 的接入点。

当前实现：

- `EmptyEvidenceProvider`
- `CompositeEvidenceProvider`

### RetrievalResult

统一表示一条检索结果：

- `id`
- `namespace`
- `content`
- `source_type`
- `confidence`
- `metadata`

### EvidencePack

统一传递外部证据：

- `memory_results`
- `lore_results`
- `voice_results`

当前 `ContextBuilder` 已接受 `evidence_pack` 参数，但 v0.1 尚未接入真实 lore retriever。

扩展约束：

- Lore RAG、voice examples、剧情原文检索都应输出 `RetrievalResult`。
- 不同 retriever 可以并行实现，但进入 prompt 前统一聚合为 `EvidencePack`。
- Memory / RAG 的核心策略留在内部服务；MCP 只作为可选 adapter 或受控访问出口。

## Tool / MCP Interfaces

位置：

- `app/tools/schemas.py`
- `app/tools/ports.py`

### Tool Schemas

当前预留的数据结构：

- `ToolSpec`：工具定义。
- `ToolCall`：一次工具调用请求。
- `ToolContext`：工具执行上下文。
- `ToolPolicyDecision`：权限判断结果。
- `ToolResult`：工具执行结果。
- `ToolNeedDecision`：是否需要工具调用的判断结果。

### Tool Ports

当前预留的接口：

- `ToolRegistryPort`
- `ToolNeedDetectorPort`
- `ToolPolicyPort`
- `ToolExecutorPort`

设计约束：

- `ToolNeedDetectorPort` 只判断是否需要工具，不直接执行工具。
- `ToolPolicyPort` 必须在执行前判断权限。
- `ToolExecutorPort` 可以由本地工具、远程工具或 MCP adapter 实现。
- MCP server/client 不应直接进入 `DialogueController`，必须通过 tool ports 或 retrieval ports 接入。
- 工具调用结果未来应进入 trace，且不能包含 API key 等敏感信息。

当前状态：

- v0.1 不启用工具调用。
- v0.1 不启动 MCP server。
- 这些接口仅用于避免后续接工具时破坏现有模块边界。

## Model Interface

位置：

- `app/model/client.py`
- `app/model/deepseek.py`
- `app/model/mock.py`

### ChatModelClient

```python
class ChatModelClient(Protocol):
    def chat(self, *, messages: list[dict[str, str]]) -> str:
        ...
```

当前实现：

- `MockChatModelClient`
- `DeepSeekClient`
- `FallbackChatModelClient`

扩展约束：

- 新模型供应商必须实现 `ChatModelClient`。
- 模型供应商专属参数应限制在 provider client 内部，不应污染 `ContextBuilder` 或 `DialogueController`。
- 结构化模型错误应在 model layer 内定义，再由 CLI 或 trace 层决定如何展示。

## Memory Decision Contract

位置：

- `app/schemas.py`
- `app/memory/classifier.py`
- `app/memory/writer.py`

### MemoryDecision

用于表达“是否写记忆、写什么、如何写”：

- `should_write`
- `operation`
- `namespace`
- `memory_type`
- `content`
- `importance`
- `confidence`
- `source`
- `source_message_ids`
- `related_memory_ids`
- `reason`
- `expiry`

扩展约束：

- classifier 只负责决策，不直接写数据库。
- writer 只负责持久化和去重，不负责判断用户意图。
- 未来 LLM classifier 应继续输出 `MemoryDecision`。

### MemoryQuery / MemorySearchResult

位置：

- `app/memory/schemas.py`
- `app/memory/reader.py`

`MemoryQuery` 用于表达一次长期互动记忆检索：

- `user_id`
- `query_text`
- `character_id`
- `namespaces`
- `memory_types`
- `limit`
- `min_confidence`
- `reason`

`MemorySearchResult` 用于表达检索结果和解释：

- `record`
- `score`
- `match_reason`

扩展约束：

- Interaction memory 走 `MemoryReaderPort`。
- Canon / lore / voice evidence 走 `EvidenceProviderPort`。
- `EvidencePack.memory_results` 仅保留给未来统一检索聚合或外部 memory evidence，不应与当前 interaction memory read path 混用。
- v0.1 仅支持 Aemeath 单角色记忆，`MemoryQuery.character_id` 先作为 trace / 未来过滤字段传递；真正按角色隔离 memory 由后续 namespace / schema 任务处理。

## Observability Interfaces

位置：

- `app/observability/schemas.py`
- `app/observability/ports.py`
- `app/observability/noop.py`

### ConversationRun

一次 turn 的生命周期对象：

- `turn_id`
- `user_id`
- `session_id`
- `character_id`
- `started_at`
- `ended_at`
- `status`
- `warnings`

### TraceEvent

一次可观察事件：

- `event_type`
- `turn_id`
- `session_id`
- `user_id`
- `character_id`
- `payload`
- `level`
- `event_id`
- `created_at`

### TraceLoggerPort

```python
class TraceLoggerPort(Protocol):
    def record(self, event: TraceEvent) -> None:
        ...
```

当前实现：

- `NoopTraceLogger`

扩展约束：

- Trace 不得记录 API key。
- 默认 trace payload 应记录 id、状态、长度、原因等结构化摘要，避免默认保存完整 system prompt。
- Controller 写入 trace 前应对常见敏感 metadata key 做基础脱敏，例如 token、secret、password、cookie、authorization。
- Controller 应在 turn 中途异常时记录 `turn_failed`，再继续抛出异常交给上层处理。
- 后续 SQLite / JSONL / OpenTelemetry trace logger 都应实现 `TraceLoggerPort`。

## State Update Contract

位置：

- `app/schemas.py`
- `app/state/classifier.py`
- `app/agent/state_updater.py`

### StateUpdate

用于表达一轮对话后的关系状态变化：

- `user_emotion`
- `aemeath_mood`
- `trust_delta`
- `affection_delta`
- `familiarity_delta`
- `last_topic`
- `reason`

扩展约束：

- classifier 输出 delta，不直接操作 store。
- store 负责 clamp 数值范围和落库。
- 未来可以新增 state policy，但 controller 的调用方式应保持稳定。

## Response Mode Contract

位置：

- `app/agent/response_mode.py`

当前模式：

- `casual_chat`
- `comfort`
- `memory_recall`
- `planning`
- `lore_answer`
- `technical_help`
- `roleplay_scene`

扩展约束：

- 新 response mode 必须更新 prompt 策略和测试样例。
- 不同 mode 可以影响检索策略、语气强度和是否启用 thinking。

## Interface Change Procedure

1. 在 `docs/task_log.md` 创建接口变更任务。
2. 说明新增或修改的 port、dataclass 或 schema。
3. 更新实现和最小测试。
4. 更新本文件。
5. 运行：

```bash
pytest
ruff check .
```
