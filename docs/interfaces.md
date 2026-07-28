# Aemeath Agent Interfaces

## 2026-06-08 Manager Update: Session Contract v0.1

The planned public session type is `SessionRecord`:

```python
@dataclass(frozen=True)
class SessionRecord:
    id: str
    user_id: str
    character_id: str
    status: Literal["active", "ended", "archived"]
    title: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    ended_at: str | None
    archived_at: str | None
    last_message_at: str | None
    message_count: int
```

The stable persistence boundary is `SessionStorePort`, exposing:

- `ensure_session(...)`
- `create_session(...)`
- `get_session(...)`
- `list_sessions(...)`
- `end_session(...)`
- `resume_session(...)`
- `archive_session(...)`

`session_id` is the globally unique primary key. `user_id + character_id` is an ownership and
filtering dimension, not a composite session ID. Existing `AgentTurnInput` remains compatible.
Changing the CLI's currently selected session does not implicitly end the old session.
`message_count` and `last_message_at` are read-model fields; the v0.1 SQLite adapter derives them
from messages rather than requiring Controller-managed counters.
Full semantics, migration rules, CLI commands, and the Memory boundary are defined in
`docs/session_contract.md`.

## 2026-06-04 Manager Update: Prompt Run Logging

Current runtime now exposes prompt-run logging as an observability port.

New contract:
- `PromptRunLoggerPort.record(record: PromptRunRecord) -> None`
- `PromptRunRecord` captures `run_id`, `turn_id`, `session_id`, `user_id`,
  `character_id`, `created_at`, `model`, compiled `messages`, structured prompt
  `sections`, `prompt_context_metadata`, `response`, `warnings`, `status`, and optional
  failure details.
- Runtime wiring chooses `JsonlPromptRunLogger` when
  `AEMEATH_ENABLE_PROMPT_RUN_LOG=true`; otherwise it uses `NoopPromptRunLogger`.
- Default log root is `.aemeath/sessions`.

Prompt section order is now:
`base_instructions -> character sections -> canon_evidence -> voice_evidence ->
interaction_memories -> relationship_state -> recent_conversation -> current user message`.

Boundary:
- Prompt run logs are local debugging/eval artifacts, not memory.
- They must not record API keys or authorization tokens.
- Future trace, eval, LangGraph checkpointing, or UI viewers should consume this port or an
  adapter around it instead of reaching into `DialogueController` internals.

## 2026-06-03 Manager Update: No Global Response Mode

当前运行时已经移除全局 `ResponseModeRouter`。`DialogueController` 不再把一轮对话分类成
`casual_chat`、`lore_answer`、`comfort` 等 mode，也不再把 mode 传给下游模块。

新的接口原则：

- `DialogueController` 每轮仍固定调用稳定 port。
- 模块自己判断是否需要工作；不需要时返回空结果。
- `MemoryReaderPort.retrieve_query(...)` 内部决定是否读记忆，必要时返回 `[]`。
- `EvidenceProviderPort.build_evidence_pack(...)` 内部决定是否查 lore / plot / voice，必要时返回空 `EvidencePack()`。
- `CharacterProviderPort.build_runtime_context(...)` 内部决定角色上下文和样例选择。
- Tool / MCP 后续也应通过自己的 policy / detector 判断，不依赖全局 mode。

兼容说明：Character v0.2 manifest 中仍存在 `inject.response_modes`，部分 provider 仍接受可选
`response_mode` keyword 给旧测试或调试脚本使用。这些都视为 legacy adapter 字段，不是当前架构契约。

本文档定义 Aemeath Agent 当前稳定接口和后续扩展约束。Worker session 实现功能时应优先依赖这些接口，而不是直接耦合具体实现。

## Service Layer Interfaces

位置：

- `app/services/schemas.py`
- `app/services/agent_service.py`

`AgentService` is the stable application boundary for CLI, FastAPI, future Web, streaming,
and optional post-response adapters. It wraps the internal turn contract without moving
Character, Memory, RAG, Prompt, Model, State, or Session policy out of their modules.

### UserContext

```python
@dataclass(frozen=True)
class UserContext:
    user_id: str
    roles: list[str] = field(default_factory=list)
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

`UserContext` is the Auth insertion point for FastAPI and future adapters. v0.1 only requires
`user_id`; roles and metadata are carried for adapters and must not be treated as a full
permission model.

### ChatRequest

```python
@dataclass(frozen=True)
class ChatRequest:
    session_id: str
    character_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

### ChatResponse

```python
@dataclass(frozen=True)
class UsedMemorySummary:
    id: str
    content: str
    score: float | None
    match_reason: str


@dataclass(frozen=True)
class ChatResponse:
    turn_id: str
    session_id: str
    character_id: str
    response: str
    warnings: list[str]
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    used_memories: list[UsedMemorySummary] = field(default_factory=list)
```

`ChatResponse.character_id` is the requested target character in v0.1. If the Character Registry
falls back to the default character, the actual fallback remains visible through `warnings`;
`AgentTurnOutput` is not changed for this service task.

### AgentService

```python
class AgentService:
    def handle_chat(
        self,
        *,
        user_context: UserContext,
        request: ChatRequest,
    ) -> ChatResponse:
        ...
```

Success returns `ChatResponse`. Failure re-raises the original exception. CLI and FastAPI
adapters should use `map_exception(exc)` and `ErrorInfo.to_public_dict()` instead of relying on
raw exception text or introducing `ServiceResult` / `ServiceError`.

`AgentService` may receive `AppLoggerPort`; the default is `NoopAppLogger`. Service app logs must
stay summary-only: request id, user id, session id, character id, status, turn id, error code,
duration, and warning count. They must not include full prompts, model messages, user message
text, provider raw responses, API keys, authorization headers, cookies, or tokens.

## Auth / Account / Permission Interfaces

位置：

- `app/accounts/`
- `app/auth/`
- `app/permissions/`
- `docs/auth_and_permissions.md`

### AccountRecord

```python
@dataclass(frozen=True)
class AccountRecord:
    id: str
    username: str
    display_name: str | None
    status: Literal["active", "disabled"]
    roles: list[str]
    dev_token_hash: str | None
    created_at: str
    updated_at: str
    last_login_at: str | None
```

`id` is the stable internal `user_id`. The default account is `user_default` with role `user`.
The SQLite adapter backfills distinct historical `user_id` values from runtime tables when
available.

### AuthService

```python
class AuthService:
    def resolve_bearer_token(
        self,
        token: str | None,
        *,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UserContext:
        ...
```

`resolve_bearer_token(...)` accepts a raw dev token or `Bearer ...` string, hashes it, looks up an
active account by `dev_token_hash`, updates `last_login_at`, and returns `UserContext`.
It never persists or logs the raw token.

`LocalDefaultAccountResolver` resolves the local CLI/default account to `UserContext` without
introducing a login flow.

### PermissionService

```python
class PermissionService:
    def require_owner(
        self,
        *,
        resource_user_id: str,
        user_context: UserContext,
    ) -> None:
        ...
```

Permission v0.1 is owner-only. Session owner failures continue to use
`SESSION_OWNERSHIP_DENIED`; generic non-session owner-only failures use `PERMISSION_DENIED`.
Roles are carried in `UserContext` for future use but are not a full RBAC model in v0.1.

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
class UsedMemory:
    id: str
    content: str
    score: float | None
    match_reason: str


@dataclass
class AgentTurnOutput:
    turn_id: str
    session_id: str
    response: str
    used_memory_ids: list[str]
    state_delta: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    used_memories: list[UsedMemory] = field(default_factory=list)
```

含义：

- `response`：模型或 fallback 生成的角色回复。
- `used_memory_ids`：参与本轮上下文的长期记忆。
- `used_memories`：CLI/debug 友好的长期记忆摘要，包含 id、内容、分数和匹配原因。
- `state_delta`：关系状态变化。
- `warnings`：非致命问题，例如记忆去重、模型 fallback。

## Dialogue Controller Ports

位置：

- `app/agent/ports.py`
- `app/agent/dialogue_controller.py`

`DialogueController` 是编排者，只依赖以下 ports：

- `CharacterProviderPort`
- `SemanticQueryCoordinatorPort`
- `EvidenceProviderPort`
- `MemoryReaderPort`
- `MemoryWriterPort`
- `MemoryWriteClassifierPort`
- `StateUpdaterPort`
- `ContextBuilderPort`
- `ResponseGeneratorPort`
- `TraceLoggerPort`
- `PromptRunLoggerPort`
- `MessageStore`
- `RelationshipStore`

新增功能应优先实现这些 port，或在 manager session 评审后新增 port。

v0.3 Memory 扩展说明：

- `MemoryWriterPort.persist_decision(...)` 可接收可选 `session_id` 和 `character_id`，用于
  memory layer 内部生成提炼 job 的幂等键。
- `MemoryWriterPort.enqueue_window_if_due(...)` 由 controller 每轮固定调用，但窗口大小、
  是否启用和是否创建 job 均由 memory writer 内部决定。

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
- `app/agent/context_budget.py`

### PromptSection

结构化 prompt 段落：

- `id`
- `title`
- `content_lines`
- `source_ids`
- `metadata`

### PromptContext

一次模型调用前的结构化上下文：

- `user_input`
- `sections`
- `metadata`

### PromptCompiler

将 `PromptContext` 编译为模型 messages。

当前 `ContextBuilder.build_messages()` 保持兼容旧调用，但内部流程已经变为：

```text
ContextBuilder.build_context()
  -> PromptContext
  -> ContextCompressorPort.compress()
  -> PromptCompiler.compile_messages()
  -> list[dict[str, str]]
```

扩展约束：

- prompt snapshot、eval、critic、trace 都应优先读取 `PromptContext`。
- 新增 prompt section 时应有稳定 `section.id`，便于 trace 和 eval 引用。
- `ContextBuilder` 负责汇聚上下文，`PromptCompiler` 负责输出模型消息格式。
- `DialogueController` 应显式调用 `build_context()`，记录 prompt section 摘要后再编译 messages，避免 `PromptContext` 只停留在内部实现。

### ContextCompressorPort

```python
class ContextCompressorPort(Protocol):
    def compress(self, *, context: PromptContext) -> PromptContext:
        ...
```

当前实现包括 `NoopContextCompressor` 和 `DeterministicContextCompressor`。后者遵守以下契约：

- 输入是完整的 pre-budget `PromptContext`，因此不得重新检索、调用 embedding 或改变上游排序。
- 受保护 section、未知 section 和当前 `user_input` 不可裁剪。
- 可裁 section 按 Style、Voice、Canon、Memory、Recent 的顺序删除完整 item。
- 映射不完整或 section id 重复时 fail safe，相关 section 不参与裁剪。
- 删除内容不得进入 metadata、trace 或 prompt-run log；只记录 ids、计数、估算和 reason。
- 字符预算不是模型 token 上限；protected floor 可以明确超出目标值。
- 顶层 `memory_ids` / `evidence_ids` 和公开 `used_memory*` 继续表示检索事实，最终 Prompt
  成员以 `context_budget.kept_source_ids` / `dropped_source_ids` 为准。

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
- `source_doc_ids` 是兼容字段，可为空或粗粒度填写；v0.2 Character 质量控制不围绕它设计。角色规则治理优先使用 rule id、`source_type`、`review_status` 和人工审核。
- `CharacterRegistry` 是多角色入口；当前默认注册 `app/character/Aemeath.v0.2/`，fallback 为 `app/character/Aemeath.v0.1/character.yaml`。root-level `app/character/aemeath.v0.1.yaml` 已废弃但仍可显式加载兼容。
- `DialogueController` 应按 `AgentTurnInput.character_id` 请求角色资料。
- 未注册角色应降级到默认角色并写入 warning / trace，不应静默使用错误角色。

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
        metadata: dict[str, Any] | None = None,
        semantic_context: TurnSemanticContext | None = None,
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
- `WorldInfoEvidenceProvider`，当前主运行时 active provider

`EmptyEvidenceProvider` 仅用于测试或显式关闭外部证据的运行方式，不再是 `app/main.py` 的默认 provider。

`semantic_context` 是可选的 turn-local 优化输入。现有关键词 Provider 可以忽略它；未来
canonical vector Provider 应消费其中已经解析好的 exact-signature query embedding，而不是再次
请求 Embedding Provider。

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

## Future Audio / TTS Interface

当前状态：

- `Aemeath_roleplay_skill/audio/` 是本地 GPT-SoVITS 实验工作区。
- 该能力尚未接入 Agent 主流程。
- 当前没有 `AudioOutputPort`、`TextToSpeechPort` 或服务端 TTS API。

未来建议的最小接口方向：

```python
class TextToSpeechPort(Protocol):
    def synthesize(
        self,
        *,
        text: str,
        user_id: str,
        session_id: str,
        character_id: str,
        voice_id: str | None = None,
        emotion: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AudioSynthesisResult:
        ...
```

建议结果结构：

- `audio_id`
- `file_path` 或 `url`
- `format`
- `duration_seconds`
- `provider`
- `latency_ms`
- `warnings`

边界约束：

- Audio/TTS 发生在 `AgentTurnOutput.response` 之后，不参与 prompt 编译。
- `DialogueController` 不应直接依赖 GPT-SoVITS。
- CLI / Web / API adapter 可选择是否调用 TTS。
- TTS 失败应降级为无音频输出，并通过 trace / app log 记录。
- GPT-SoVITS 本地权重、训练素材、运行包和输出音频不进入源码仓库。
- 服务器部署时，优先把 TTS 视为独立服务或可关闭的 adapter，因为它需要 GPU 资源。

## Error Handling Interfaces

Position:

- `app/errors/base.py`
- `app/errors/codes.py`
- `app/errors/mapper.py`
- `app/errors/http.py`
- `app/errors/sanitizer.py`

### ErrorCode

`ErrorCode` is the stable machine-readable error category used by CLI, FastAPI adapters,
trace records, and prompt-run failure logs. Codes currently cover:

- generic runtime and invalid input;
- account auth and owner-only permission errors;
- config and missing secret errors;
- session lifecycle and ownership errors;
- character load/manifest errors;
- memory source and extraction errors;
- model request, auth, rate limit, timeout, unavailable, and invalid-response errors;
- storage availability, migration, and constraint errors.

### ErrorInfo

`ErrorInfo` is the normalized error payload:

- `code`
- `message`
- `retryable`
- `error_type`
- `details`
- `internal_message`

Use `ErrorInfo.to_public_dict()` for user/API-facing output and `ErrorInfo.to_log_dict()` for
sanitized logs and traces.

### Mapping And HTTP Boundary

- `AemeathError` lets business modules raise a stable code plus public message.
- `map_exception(exc)` converts known exceptions into `ErrorInfo`.
- `http_status_for_error(error_info)` maps `ErrorInfo` into HTTP status codes for FastAPI and
  future HTTP adapters.
- Current CLI code uses the same mapping but does not depend on FastAPI.

Service/API layers should reuse these interfaces instead of creating a second error taxonomy.

### Sanitizer

`redact_sensitive(...)` and related helpers must be used before writing error details to logs,
prompt-run records, traces, or public responses.

Current sensitive key coverage includes:

- `api_key`
- `apikey`
- `api-key`
- `x-api-key`
- `x_api_key`
- `authorization`
- `cookie`
- `password`
- `secret`
- `token`

Text redaction also covers common `Bearer ...` and `key=value` style secret fragments. It should
not over-redact ordinary field names such as `ordinary-key`.

### Runtime Integration

- `DialogueController` records structured failed-turn error fields and re-raises.
- Prompt-run logs include `error_code`, `error_type`, `error_public_message`, and sanitized
  `error` for failed model/controller runs.
- CLI prints stable public messages such as `[SESSION_ENDED]`.

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
- `session_id`（兼容旧调用可为空；Controller 必须传入已验证 Session ID）
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
- 对话召回按 `MemoryQuery.user_id + character_id + session_id` 计算 scope visibility。

### Memory v0.3 Extraction And Vector Recall

位置：

- `app/memory/extractor.py`
- `app/memory/writer.py`
- `app/memory/reader.py`
- `app/memory/worker.py`
- `app/db/sqlite_store.py`

新增持久化结构：

- `memory_extraction_jobs`：长期记忆提炼 job queue，含 `idempotency_key`、
  `status`、`trigger_type`、`source_message_ids_json`、`input_json`、`result_json`、
  `error`、`attempt_count`、`locked_by`、`locked_at` 等字段。
- `long_term_memory_embedding_artifacts`：每条长期记忆可以按 exact canonical signature
  共存多个 `document` artifact；`long_term_memory_embeddings` 仅保留一个兼容版本。
- `long_term_memories` 增加 `scope`、`scope_character_id`、`scope_session_id`；
  来源单独记录在 `source_session_id`、`source_job_id`、`source_message_ids_json`。
- `schema_migrations` 记录一次性正式迁移；旧记忆迁为 `user_global`，保持跨 Session
  召回行为。

新增运行时能力：

- `QueuedMemoryWriter.persist_decision(...)` 将显式记忆触发写为 `hard` job。
- `QueuedMemoryWriter.enqueue_window_if_due(...)` 在当前 session 有至少
  `AEMEATH_MEMORY_WINDOW_SIZE` 条未处理消息时写入 `window` job。
- `MemoryExtractionService` 使用 `ChatModelClient` 输出 JSON memories，先通过
  `create_or_get_exact_active_memory(...)` 提交事实，再批量写 canonical document artifacts。
- `MemoryReader` 优先使用共享 turn 的 `TurnSemanticContext` 查询 exact-signature artifact，
  不会在同一对话 turn 再调用一次 Provider。未传 context 的独立调用保留 provider-neutral
  `QueryEmbeddingSourcePort` 一个兼容版本，返回
  `MemorySearchResult(match_reason="embedding_similarity")`。
- coverage 不完整时 vector 结果优先、keyword 只补位，不比较两种分数；query embedding
  失败或无有效向量命中时执行 keyword fallback，显式中文回忆保留 active-memory fallback。
- 关键词召回、向量召回和 fallback 共享 scope visibility：
  `user_global` 对同一用户可见，`character` 还需角色匹配，`session` 还需 Session 匹配。
- 精确去重严格匹配待写入 memory 的
  `scope + scope_character_id + scope_session_id`，不复用召回可见性。
- `python -m app.memory.worker` 独立处理 pending jobs。

配置：

- `AEMEATH_ENABLE_LONG_TERM_MEMORY`
- `AEMEATH_EMBEDDING_PROFILE`
- `AEMEATH_EMBEDDING_ALLOW_HASH`
- `AEMEATH_MEMORY_RECALL_TOP_K`
- `AEMEATH_MEMORY_RECALL_THRESHOLD`
- `AEMEATH_MEMORY_WORKER_POLL_INTERVAL_SECONDS`
- `AEMEATH_MEMORY_REPAIR_BATCH_SIZE`
- `AEMEATH_MEMORY_REPAIR_INTERVAL_SECONDS`
- `AEMEATH_MEMORY_REPAIR_MAX_BACKOFF_SECONDS`
- `AEMEATH_MEMORY_WINDOW_SIZE`
- `AEMEATH_ENABLE_MEMORY_WINDOW_EXTRACTION`

Provider 必须来自 approved profile 并经过统一 Factory；配置错误 fail-fast，不隐式切换
Hash。`AEMEATH_MEMORY_EMBEDDING_PROVIDER` 和模型路径旧名称只保留一个兼容版本。

CLI 调试命令：

- `/memory extract [limit]`：同步提炼当前 session 最近消息。
- `/memory debug on|off`：显示或关闭本轮召回 memory 摘要。

管理与权限：

- `/memory extract` 通过 `SessionStorePort.require_session(...)` 校验 Session 所有权；
  active、ended 可读取，archived 默认拒绝。
- `/memory delete` 和 `/memory disable` 使用 `memory_id + user_id` 原子更新。
- `/memory list` 是所有者管理视图；对话召回使用 `MemoryQuery` scope visibility，两者不共用
  列表语义。

边界：

- `DialogueController` 仍只调用 memory ports。
- Interaction memory 不进入 `EvidencePack.memory_results`。

### Embedding Foundation And Canonical Artifact Interfaces

`app/embeddings/ports.py` defines the shared batch-first `EmbeddingProviderPort`:

```python
embed(request: EmbeddingRequest) -> EmbeddingBatchResult
```

`EmbeddingRequest.purpose` is explicitly `query` or `document`. Every result carries an exact
`EmbeddingModelSignature`; equal dimensions do not make two signatures compatible. Provider
runtime options such as timeout, retry count, batch size, local device, and API credentials are not
part of artifact identity.

Profiles are selected through `AEMEATH_EMBEDDING_PROFILE`. The factory accepts only repository-owned
canonical profiles whose release status is `approved`. The old
`AEMEATH_MEMORY_EMBEDDING_PROVIDER` and `AEMEATH_MEMORY_EMBEDDING_MODEL_PATH` settings remain a
one-release Memory compatibility path and must not define new canonical vector spaces.

Current approved Profiles are `siliconflow_pro_bge_m3_v1` for API-backed 1024-dimensional
Memory/Semantic retrieval and `deterministic_hash_v1` for tests or explicit development. Hash still
requires `AEMEATH_EMBEDDING_ALLOW_HASH=true`; SiliconFlow must use the fixed
`SILICONFLOW_API_KEY` secret boundary and must not fall back to Hash. `local_bge_m3_v1` remains
`review`.

`MemoryStore` currently exposes both legacy and canonical methods:

- compatibility: `upsert_memory_embedding(...)`, `search_memory_embeddings(...)`;
- canonical: `upsert_memory_embedding_artifact(...)`,
  `search_memory_embedding_artifacts(...)`,
  `create_or_get_exact_active_memory(...)`, and
  `list_memory_embedding_artifact_gaps(...)`.

Canonical Memory search accepts `MemoryArtifactEligibility` and returns
`MemoryEmbeddingSearchOutcome(hits, diagnostics, eligible_memory_count)`. Its coverage count and
artifact candidates share the same owner, scope, namespace, type, confidence, active, and status
predicates in one read snapshot. Persistence filters by owner/scope, signature fingerprint, and
`document` purpose in SQL, then validates the complete signature and artifact bytes before scoring.
Different signatures coexist and are never cross-scored.

`create_or_get_exact_active_memory(...)` atomically resolves an active identity of
`user_id + scope + scope_character_id + scope_session_id + content` and returns
`ExactActiveMemoryResult(memory_id, created)`. A reuse preserves the original Memory fields;
archived or disabled rows do not prevent a new active row. The persistence invariant is a partial
unique expression index over that identity.

`list_memory_embedding_artifact_gaps(...)` takes a full `EmbeddingModelSignature` and returns
bounded, stable keyset pages of active Memories lacking a valid exact-signature `document`
artifact. Its reason is one of `missing`, `content_stale`, `vector_corrupt`,
`signature_corrupt`, or `legacy_only`. It is an internal Worker API: its default scan may cross
users for repair work, but it must not be exposed through Service, FastAPI, or MCP. It does not
schedule repair, retry, fallback, or rebuild work.

`SqliteVectorStore` provides the equivalent RAG artifact operations while retaining
`upsert_chunks(...)` and `search(...)` as one-release compatibility entry points. Namespace filters
must apply to both returned hits and aggregate diagnostics.

`QueryEmbeddingSourcePort.resolve(...)` returns provider-neutral
`ResolvedQueryEmbedding(signature, vector)`. `DirectQueryEmbeddingSource` remains the compatibility
implementation for calls outside the dialogue turn. The normal enabled Memory dialogue path now
receives `ResolvedQueryEmbedding` through `TurnSemanticContext` and does not make a second query
request.

`MemoryExtractionResult` and persisted job results distinguish
`created_memory_ids`, `existing_memory_ids`, `artifact_written_memory_ids`, and
`embedding_pending_memory_ids`. `MemoryEmbeddingRepairService` consumes bounded exact-signature
gap pages; pending is derived and does not add another persistence table.

These additions must not expose vectors or provider details to `DialogueController`.
- 当前自动提炼仍创建 `user_global` memory。`character` / `session` scope 已具备存储和召回
  契约，未来 scope classifier 另行设计。

## Semantic Query Interfaces

位置：

- `app/semantic/schemas.py`
- `app/semantic/ports.py`
- `app/semantic/query_builder.py`
- `app/semantic/coordinator.py`

`SemanticQueryCoordinatorPort` 是本轮语义查询的唯一入口：

```python
class SemanticQueryCoordinatorPort(Protocol):
    def prepare_turn_context(
        self,
        *,
        turn_input: SemanticTurnInput,
    ) -> TurnSemanticContext:
        ...
```

`SemanticTurnInput` 携带当前输入以及最近的 `user` / `assistant` 消息。Controller 创建该 DTO
前按 message id 排除本轮已保存的当前消息；system、tool 和未知 role 不进入 query builder。

`TurnSemanticContext` 为以下具名 role 各保存一个 resolution：

- `memory_recall`
- `canonical_lore`
- `canonical_voice`
- `character_examples`

每个 resolution 的状态是 `not_requested`、`skipped`、`ready` 或 `failed`。ready resolution
持有 provider-neutral `ResolvedQueryEmbedding`；context 不提供 query/vector 序列化，也不跨 turn
持久化。`safe_summary()` 只包含状态、reason code、计数、耗时、provider/profile 标识和
signature fingerprint。

v0.1 Coordinator 使用版本化确定性查询预算，对相同 role query 去重，并在一轮内提交一次
batch-first `purpose="query"` 请求。永久配置、qualification、signature 或结果契约错误会将
Coordinator 标为 unhealthy，直到 runtime 重建；timeout、rate limit 和 provider unavailable
只降级当前 turn；invalid input 保持原 typed request error，不映射为 unavailable。

当前 runtime 激活范围：

- 仅在长期 Memory 启用时激活 `memory_recall`。
- World Info 接收但忽略 context，保持关键词行为。
- canonical RAG 和 Character example role 尚未激活。
- 本接口不包含跨轮 embedding cache 或 Persistence schema。

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
- `JsonlTraceLogger`

Runtime configuration:

- `AEMEATH_ENABLE_TRACE_LOG=false`
- `AEMEATH_TRACE_LOG_ROOT=.aemeath/traces`

扩展约束：

- Trace 不得记录 API key。
- 默认 trace payload 应记录 id、状态、长度、原因等结构化摘要，避免默认保存完整 system prompt。
- Controller 写入 trace 前应对常见敏感 metadata key 做基础脱敏，例如 token、secret、password、cookie、authorization。
- Controller 应在 turn 中途异常时记录 `turn_failed`，再继续抛出异常交给上层处理。
- 后续 SQLite / JSONL / OpenTelemetry trace logger 都应实现 `TraceLoggerPort`。

### AppLogRecord

Application log records are for process and boundary events, such as CLI startup, FastAPI
request handling, worker lifecycle events, and operational failures. They are separate from agent
turn trace events and prompt-run logs.

- `event_name`
- `level`
- `created_at`
- `request_id`
- `turn_id`
- `session_id`
- `user_id`
- `character_id`
- `error_code`
- `payload`

### AppLoggerPort

```python
class AppLoggerPort(Protocol):
    def record(self, record: AppLogRecord) -> None:
        ...
```

Current implementations:

- `NoopAppLogger`
- `JsonlAppLogger`

Runtime configuration:

- `AEMEATH_ENABLE_APP_LOG=false`
- `AEMEATH_APP_LOG_ROOT=.aemeath/app_logs`

Boundary:

- App logs belong at CLI/API/service/worker boundaries.
- `DialogueController` is not injected with `AppLoggerPort` in v0.1.
- App log payloads must reuse `app.errors.redact_sensitive(...)`.
- App logs should not store full prompts, full model messages, or raw provider responses.

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


当前模式：

- `casual_chat`
- `comfort`
- `memory_recall`
- `planning`
- `lore_answer`
- `technical_help`
- `roleplay_scene`

扩展约束：

- 新的对话决策维度应进入对应模块 policy，而不是恢复全局 response mode。
- 检索策略、语气强度和 thinking 等行为应由对应模块或模型 provider 内部策略决定。

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
## 2026-06-04 Retrieval Interface Update

- `WorldInfoEvidenceProvider` is the active main-runtime implementation of
  `EvidenceProviderPort`.
- It preserves the public interface:

```python
build_evidence_pack(
    *,
    user_id: str,
    character_id: str,
    query: str,
    metadata: dict | None = None,
    semantic_context: TurnSemanticContext | None = None,
) -> EvidencePack
```

T-0135 added `semantic_context` as a backward-compatible optional parameter; World Info ignores it.

- World Info results are returned as `EvidencePack.lore_results`.
- `voice_results` remains empty for the World Info provider.
- Controller, Character, and Memory layers do not call `WorldInfoKeywordRetriever` directly.
