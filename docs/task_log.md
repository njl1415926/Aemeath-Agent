# Aemeath Agent Task Log

本文档由 manager session 维护，用于记录项目任务、owner、范围、状态和验收标准。新 session 开工前应先读取本文件。

## Status Legend

- `todo`：尚未开始。
- `in_progress`：正在执行。
- `blocked`：等待外部信息或前置任务。
- `review`：实现完成，等待检查。
- `done`：已完成并通过验收。

## Current Manager Decision

本项目进入多 session 协作模式。

当前 manager session 身份：

- 负责整体架构设计。
- 负责接口设计。
- 负责任务拆解和 session 边界管理。
- 负责检查代码是否破坏解耦、可扩展性和接口稳定性。

默认原则：

- Worker session 不直接改架构文档，除非任务要求。
- Worker session 不同时修改同一模块的核心接口。
- 涉及接口变化必须回到 manager session 统一。

## Completed Tasks

### T-0001 v0.1 最小对话闭环

状态：`done`

范围：

- CLI 对话入口。
- 角色卡加载。
- SQLite 消息、记忆、关系状态持久化。
- 长期记忆写入与检索。
- 关系 / 情绪状态更新。
- Mock model 和 DeepSeek model。

关键文件：

- `app/cli.py`
- `app/main.py`
- `app/agent/dialogue_controller.py`
- `app/character/provider.py`
- `app/memory/*`
- `app/state/*`
- `app/model/*`
- `app/db/sqlite_store.py`

验收：

- `pytest`
- `ruff check .`

### T-0002 DeepSeek v4 参数错误修复

状态：`done`

问题：

- `DEEPSEEK_THINKING=disabled` 时仍发送 `DEEPSEEK_REASONING_EFFORT=high`，DeepSeek 返回 HTTP 400。

修复：

- `app/model/deepseek.py` 根据 thinking 状态动态构造请求体。
- `.env.example` 和 README 默认将 `DEEPSEEK_REASONING_EFFORT` 设为空。
- 增加 `tests/test_deepseek_client.py`。

记录：

- `docs/debug_log.md`

验收：

- `pytest`：7 passed
- `ruff check .`：All checks passed
- 真实 DeepSeek 烟测返回 `pong`

### T-0003 Manager Session 文档与接口边界

状态：`done`

范围：

- 明确 manager session 身份。
- 新增多 session 协作指南。
- 新增架构文档。
- 新增接口文档。
- 新增任务日志。
- 将 `DialogueController` 的依赖显式抽象为 ports。
- 将 memory reader / writer 从具体 `SQLiteStore` 类型改为 `MemoryStore` 协议。

关键文件：

- `AGENTS.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `app/agent/ports.py`
- `app/agent/dialogue_controller.py`
- `app/memory/reader.py`
- `app/memory/writer.py`
- `app/agent/response_generator.py`

验收：

- `pytest`
- `ruff check .`

### T-0004 MCP-ready 低成本接口预留

状态：`done`

范围：

- 新增 retrieval port，作为未来 RAG / lore / voice / MCP resource adapter 的统一入口。
- 新增空证据 provider，并接入 `DialogueController`，保持 v0.1 行为不变。
- 新增 tool calling 数据结构与 ports，但暂不启用工具调用。
- 更新架构、接口和协作文档，明确 MCP 是 adapter，不是核心业务层。

关键文件：

- `app/retrieval/ports.py`
- `app/retrieval/provider.py`
- `app/tools/schemas.py`
- `app/tools/ports.py`
- `app/agent/dialogue_controller.py`
- `app/main.py`
- `AGENTS.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`

验收：

- `pytest`
- `ruff check .`

### T-0005 Architecture Review Session X 采纳项

状态：`done`

来源：

- `docs/architecture_review_session_x.md`

已立即采纳：

- `RelationshipStore` 改用 `character_id` 作为稳定关系状态 key。
- `relationship_states` 新 schema 使用 `character_id`，并保留旧 `character_name` 表的迁移兼容。
- 新增 `PromptContext`、`PromptSection`、`PromptCompiler`，`ContextBuilder` 先产出结构化 context 再编译 messages。
- 新增 `ConversationRun`、`TraceEvent`、`TraceLoggerPort` 和 `NoopTraceLogger`。
- `DialogueController` 写入最小 trace 事件，但 v0.1 默认不持久化。
- 新增 `MemoryQuery`、`MemorySearchResult`，明确 interaction memory 检索边界。
- `docs/interfaces.md` 补齐 `EvidenceProviderPort` 和 `TraceLoggerPort`。
- 明确 `EvidencePack.memory_results` 暂不用于当前 interaction memory read path。

暂不立即实现，转入 backlog：

- Memory update / merge / conflict resolver。
- Memory decay policy。
- ModelRequest / ModelResponse。
- Critic / Rewriter hook。
- SessionStore / SessionRecord。
- ToolSpec 权限 scope、side effect level、rate limit 等增强。

关键文件：

- `app/state/store.py`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/agent/context_builder.py`
- `app/agent/prompt_context.py`
- `app/observability/*`
- `app/memory/schemas.py`
- `app/memory/reader.py`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`

验收：

- `pytest`
- `ruff check .`

### T-0006 Session X 二次审查小修

状态：`done`

来源：

- Session X 对 T-0005 后架构的二次建议。

范围：

- 统一单次 turn 内使用的 active `character_id`，当输入 `character_id` 与当前加载角色不一致时写入 warning 和 trace。
- `DialogueController` 改为显式 `build_context()`，记录 prompt section 摘要，再通过 `PromptCompiler` 编译 model messages。
- `MemoryReader` 调用改为使用 `MemoryQuery.character_id`，当前仅作为 Aemeath 单角色记忆的 trace / 未来过滤预留，不改变存储 schema。
- Trace 写入前增加基础 payload 脱敏，避免 metadata 中的 token、secret、password、cookie、authorization 等敏感字段直接进入 trace。
- Controller 中途异常时记录 `turn_failed` trace，再继续抛出异常。
- 更新 `docs/interfaces.md` 说明上述约束。

关键文件：

- `app/agent/dialogue_controller.py`
- `docs/interfaces.md`
- `docs/task_log.md`

验收：

- `pytest`：10 passed
- `ruff check .`：All checks passed

### T-0007 Multi-character Registry 小改

状态：`done`

范围：

- 新增 `CharacterRegistry`，将角色入口从单文件 provider 升级为 registry-ready。
- 保持当前单角色配置兼容，`app/main.py` 仍注册 `aemeath` 单卡。
- `CharacterProviderPort` 支持按 `character_id` 加载 profile / runtime context。
- `DialogueController` 每轮根据 `AgentTurnInput.character_id` 请求角色资料；未知角色降级默认角色并写入 warning / trace。

关键文件：

- `app/character/provider.py`
- `app/agent/ports.py`
- `app/agent/dialogue_controller.py`
- `app/main.py`
- `tests/test_character_registry.py`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`

验收：

- `pytest`：12 passed
- `ruff check .`：All checks passed

### T-0008 Character v0.2 外部配置与管理文档同步

状态：`done`

范围：

- 将 `.env.example` 和本地 `.env` 的默认 Character 来源更新为 `app/character/Aemeath.v0.2`。
- 更新架构和接口文档，说明 v0.2 manifest 是默认来源，`Aemeath.v0.1/character.yaml` 是 fallback，root-level `aemeath.v0.1.yaml` 已废弃但可显式兼容。
- 将 `source_doc_ids` 从核心溯源设计降级为兼容字段，v0.2 Character 质量控制优先依赖 rule id、`source_type`、`review_status` 和人工审核。
- 将 Character v0.2 后续优化项记录进 backlog。

关键文件：

- `.env`
- `.env.example`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`

验收：

- `pytest`
- `ruff check .`

## Active Tasks

### T-0117 Memory v0.2 Minimal Runtime

状态：`done`

Owner:
- Memory Worker Session 3

目标：
- 默认使用 recent conversation 作为 v0.2 minimal memory。
- 长期记忆读写默认关闭，并通过配置显式启用。
- 不引入 embedding memory。
- 不恢复 response_mode。
- 不让 `DialogueController` 承担复杂 memory 判断。

范围：
- `app/config.py`
- `app/main.py`
- `app/memory/*`
- `app/agent/dialogue_controller.py`
- `tests/*`
- `.env.example`

接口约束：
- `DialogueController` 仍固定调用 `MemoryReaderPort` / `MemoryWriterPort` /
  `MemoryWriteClassifierPort`。
- `ContextBuilder` 仍通过 `recent_messages` 和 `long_term_memories` 接收上下文。
- v0.2 minimal runtime 中 `long_term_memories` 可以为空，`recent conversation` 必须保留。
- 不把 interaction memory 放入 `EvidencePack.memory_results`。

验收标准：
- 默认配置下，长期记忆不读、不写。
- 显式启用长期记忆时，现有长期记忆读写测试仍通过。
- prompt 的 `recent_conversation` section 不包含本轮 user input。
- `PromptCompiler` 最后一条 user message 仍是本轮输入。
- `pytest`
- `ruff check .`

实现摘要：
- 新增 `AEMEATH_ENABLE_LONG_TERM_MEMORY=false` 默认配置。
- 默认 runtime 注入 disabled memory reader / writer / classifier，长期记忆不读不写。
- 显式启用长期记忆时继续使用 v0.1 memory reader / writer / classifier。
- `DialogueController` 过滤本轮 `user_message_id` 后再传入 recent messages，避免 prompt 重复本轮输入。

验收结果：
- `pytest`：50 passed
- `ruff check .`：All checks passed

## Backlog

### T-0101 角色资料系统 v0.2：Character Manifest

状态：`todo`

目标：

- 将单一 YAML 角色卡升级为 manifest 驱动。
- 支持 profile、voice、lore、examples、safety、version metadata。

建议 owner：

- Character worker session

建议范围：

- `app/character/*`
- `Aemeath_roleplay_skill/*`
- `docs/Aemeath_Roleplay_Document_Management_System.md`
- `docs/interfaces.md`

验收标准：

- v0.1 YAML 仍可加载。
- 新 manifest 可输出相同的 `CharacterRuntimeContext`。
- 增加 character provider 测试。

### T-0114 Character Style Examples Prompt Budget

状态：`todo`

目标：

- 优化 Character v0.2 的 `style_examples` 注入策略，降低默认 prompt 体积。
- 在角色语气稳定性和 token 成本之间建立可测试的折中。

建议 owner：

- Character worker session

建议范围：

- `app/character/Aemeath.v0.2/style_examples.yaml`
- `app/character/Aemeath.v0.2/style_examples_short.yaml`
- `app/character/provider.py`
- `tests/test_character_manifest.py`
- 可新增 `docs/character_prompt_budget.md`

接口约束：

- 不改变 `CharacterProviderPort`。
- 不让 `DialogueController` 参与 style example 选择。
- 优先通过 `inject`、response mode、数量上限或短样例库控制注入量。

验收标准：

- 每个 response mode 注入的 style examples 数量有明确上限或策略。
- 默认 casual_chat 不应注入过多长样例。
- 测试覆盖 response_mode 下的样例选择。

### T-0115 Character Fallback Metadata Cleanup

状态：`todo`

目标：

- 将 Character v0.2 fallback 信息从 `source_doc_ids` 的临时兼容表达迁移到更合适的 warning / trace / load metadata。
- 保持 `source_doc_ids` 作为兼容字段，但不再承担 fallback 事件语义。

建议 owner：

- Character + Observability worker session

建议范围：

- `app/character/loader.py`
- `app/character/provider.py`
- `app/observability/*`
- `app/agent/dialogue_controller.py`
- `tests/*`

接口约束：

- 不破坏 `CharacterRuntimeContext` 兼容。
- fallback 必须继续可见，不能静默。
- 不把 skill md 文件 source 当作 Character 质量控制核心。

验收标准：

- v0.2 加载失败时 trace / warning 能明确记录 primary path、fallback path、error type。
- `source_doc_ids` 可为空或只保留粗粒度兼容信息。
- 测试覆盖 fallback 可观测性。

### T-0102 Lore / Voice Retriever

状态：`todo`

目标：

- 将剧情文本、lore_and_voice 和 skill 文档转为可检索资料。
- 检索结果通过 `EvidencePack` 注入 `ContextBuilder`。

建议 owner：

- Retrieval worker session

建议范围：

- `app/retrieval/*`
- `app/agent/context_builder.py`
- `Aemeath_roleplay_skill/*`

验收标准：

- 输入 query 可以返回 `RetrievalResult`。
- `ContextBuilder` 能把 lore / voice evidence 放入 prompt。
- 不让 `DialogueController` 直接读取文档路径。
- 通过 `RetrieverPort` / `EvidenceProviderPort` 接入，不直接耦合具体索引实现。

### T-0103 结构化 Trace

状态：`todo`

目标：

- 为每一轮 turn 持久化可调试 trace。
- 支持查看 response mode、memory ids、evidence ids、state delta、model provider、warnings。
- v0.1 已有 `TraceEvent` / `ConversationRun` / `TraceLoggerPort` 最小契约，本任务负责落库和查询。

建议 owner：

- Observability worker session

建议范围：

- `app/schemas.py`
- `app/agent/dialogue_controller.py`
- `app/observability/*`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `docs/debug_log.md`

验收标准：

- 每轮对话产生 trace record。
- trace 不包含 API key。
- 测试覆盖 trace 写入。

### T-0108 Memory Update / Merge / Conflict Policy

状态：`todo`

目标：

- 将 `MemoryDecision.operation` 中的 `update` / `merge` 从名义支持升级为真实能力。
- 增加 memory conflict resolution 和 update plan。

建议 owner：

- Memory worker session

建议范围：

- `app/memory/*`
- `app/db/sqlite_store.py`
- `app/db/schema.sql`
- `tests/*`

接口约束：

- 新增能力应通过 memory layer 暴露，不进入 `DialogueController`。
- `MemoryWriter` 负责执行计划，classifier / resolver 负责决策。

验收标准：

- 支持更新已有 memory。
- 支持合并重复或冲突 memory。
- trace 中可记录 update / merge 的原因和来源。

### T-0109 Memory Namespace / Type Policy

状态：`todo`

目标：

- 定义 interaction memory、canon lore、voice evidence 的 namespace / source_type 规范。
- 防止用户互动记忆被误当作原作剧情证据。

建议 owner：

- Memory + Retrieval worker session

建议范围：

- `app/memory/schemas.py`
- `app/retrieval/schemas.py`
- `docs/interfaces.md`
- 可新增 `docs/memory_design.md`

验收标准：

- 明确 interaction memory 与 canon evidence 的边界。
- 新增检索 / prompt 测试覆盖 namespace 分流。

### T-0110 ModelRequest / ModelResponse Contract

状态：`todo`

目标：

- 将 `ChatModelClient.chat(...) -> str` 升级为可返回 provider、model、usage、latency、finish_reason、warnings、tool_calls 的结构化模型响应。

建议 owner：

- Model worker session

建议范围：

- `app/model/*`
- `app/agent/response_generator.py`
- `app/observability/*`
- `tests/*`

验收标准：

- 旧调用路径保持兼容或有明确 adapter。
- Trace 可以记录模型 provider、model、latency、usage。

### T-0111 Critic / Rewriter Hook

状态：`todo`

目标：

- 为角色一致性、lore grounding、safety rewrite 预留 runtime hook。
- 默认实现为 noop，不影响 v0.1 输出。

建议 owner：

- Quality / Runtime worker session

建议范围：

- `app/agent/*`
- `app/observability/*`
- `docs/architecture.md`
- `docs/interfaces.md`

验收标准：

- Controller 不直接承载 critic 逻辑。
- critic / rewrite 结果可进入 trace。

### T-0112 Session Contract

状态：`todo`

目标：

- 在 Web UI / API Server 前定义一等 Session 对象。

建议 owner：

- Runtime / API worker session

建议范围：

- `app/schemas.py`
- `app/agent/message_store.py`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`

验收标准：

- 支持 session list / resume。
- Session 使用 `session_id + user_id + character_id` 明确归属。

### T-0113 LangGraph Runtime Spike / Migration

状态：`todo`

目标：

- 在当前 ports-and-adapters 架构稳定后，引入 LangGraph 作为可选 runtime 编排层。
- 将当前线性 `DialogueController` 流程映射为显式 graph nodes / edges / state。
- 积累 LangGraph 在多分支、工具循环、checkpoint、human-in-the-loop、多 agent 协作中的工程经验。

推荐前置任务：

- T-0102 Lore / Voice Retriever
- T-0103 结构化 Trace
- T-0110 ModelRequest / ModelResponse Contract
- T-0111 Critic / Rewriter Hook

建议 owner：

- Runtime / LangGraph worker session

建议范围：

- `app/agent/*`
- 可新增 `app/runtime/*`
- `app/observability/*`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `pyproject.toml`

接口约束：

- LangGraph 只能替换或并行实现编排层，不应改写 memory、character、retrieval、model、state 的核心业务接口。
- 现有 `CharacterProviderPort`、`MemoryReaderPort`、`EvidenceProviderPort`、`ContextBuilderPort`、`ResponseGeneratorPort`、`StateUpdaterPort`、`TraceLoggerPort` 应作为 graph node 调用的 service。
- 先定义 `AgentRuntimeState`，再把现有 controller step 化，最后再迁移到 LangGraph。
- 保留当前 `DialogueController` 作为 fallback runtime，直到 LangGraph runtime 测试稳定。

验收标准：

- 至少完成一个与当前 CLI 等价的 LangGraph runtime spike。
- 同一输入下，LangGraph runtime 与当前 controller runtime 产生等价的关键 trace：module decision reasons、memory ids、evidence ids、state delta、warnings。
- `pytest` 与 `ruff check .` 通过。
- 文档说明哪些流程适合进入 graph，哪些业务能力仍保持 service / port。

风险：

- 过早迁移会把业务边界和框架概念绑死。
- 需要避免在 graph node 内直接访问 SQLite、角色文档路径或模型供应商细节。

### T-0104 Model Error Taxonomy

状态：`todo`

目标：

- 将模型调用错误拆成明确类型。
- CLI 继续温和 fallback，debug / trace 保留准确错误。

建议 owner：

- Model worker session

建议范围：

- `app/model/*`
- `app/agent/response_generator.py`
- `tests/test_deepseek_client.py`

验收标准：

- bad request、auth、rate limit、timeout 至少有可区分异常。
- fallback 不泄露密钥。
- 单元测试覆盖错误映射。

### T-0105 LLM Memory Classifier

状态：`todo`

目标：

- 将规则式记忆写入判断升级为可替换 classifier。
- 输出仍保持 `MemoryDecision`。

建议 owner：

- Memory worker session

建议范围：

- `app/memory/classifier.py`
- `app/memory/writer.py`
- `app/model/*`
- `tests/test_memory_writer.py`

验收标准：

- Mock 模式下测试稳定。
- 真实模型 classifier 可配置启用。
- 不让 writer 直接调用 LLM。

### T-0106 编码与中文规则测试

状态：`todo`

目标：

- 检查源码中中文字符串在 Windows 控制台显示乱码的问题。
- 为 memory classifier、state classifier 和模块内部 policy 增加中文行为测试。

建议 owner：

- Quality worker session

建议范围：

- `app/memory/classifier.py`
- `app/state/classifier.py`
- `tests/*`

验收标准：

- 关键中文触发词测试通过。
- 源文件以 UTF-8 保存。
- 不改变现有公开接口。

### T-0107 Tool Calling & MCP Adapter

状态：`todo`

目标：

- 在内部 tool ports 稳定后，添加本地工具调用和可选 MCP adapter。
- 将 MCP 作为工具 / 资源访问适配层，而不是核心业务层。

建议 owner：

- Tooling / MCP worker session

建议范围：

- `app/tools/*`
- `app/retrieval/*`
- `app/agent/dialogue_controller.py`
- `app/main.py`
- `docs/architecture.md`
- `docs/interfaces.md`

接口约束：

- 工具发现通过 `ToolRegistryPort`。
- 工具需求判断通过 `ToolNeedDetectorPort`。
- 工具执行前必须经过 `ToolPolicyPort`。
- 工具执行通过 `ToolExecutorPort`。
- MCP client/server 只能作为 adapter 实现这些 port 或 `RetrieverPort`。

验收标准：

- 不破坏无工具调用的 v0.1/v0.2 路径。
- 工具结果可以被结构化记录，且不包含 API key。
- 至少有一个 mock tool 或 fake MCP adapter 的测试。

### T-0116 Remove Global Response Mode Router

状态：`done`

目标：
- 从主运行时移除全局 `ResponseModeRouter`。
- 保留 Controller 对各模块稳定 port 的固定调用。
- 将是否读取记忆、是否检索剧情/语音证据、是否注入角色样例的判断收回各模块内部。

实现摘要：
- `DialogueController` 不再注入或调用 `ResponseModeRouter`。
- `AgentTurnOutput` 不再返回 `response_mode`。
- `ContextBuilder` 不再向 prompt 注入 `Response mode: ...`。
- `EvidenceProviderPort.build_evidence_pack(...)` 的正式接口不再要求 `response_mode`。
- `MemoryReader.retrieve_query(...)` 内部可对低信息输入直接返回 `[]`。
- `CharacterProvider.build_runtime_context(...)` 改为接收 `user_input` / `metadata`，内部推断 legacy context tags。
- `app/agent/response_mode.py` 已删除。
- Tool 预留接口移除 `response_mode` 参数。

兼容说明：
- Character manifest 中的 `inject.response_modes` 暂时作为 legacy tag 兼容，不是新架构契约。
- 部分 provider 方法保留可选 `response_mode` keyword 以兼容旧测试/调试脚本。

验收：
- `pytest` 通过，23 passed。
- `ruff check .` 通过。

后续：
- Character worker 应迁移 `inject.response_modes` 到 `inject.context_tags` 或更成熟的 CharacterContextPolicy。
- RAG worker 应继续强化 EvidenceProvider 内部 policy，不再要求外部 mode。
- 后续 trace 应记录模块内部 decision reason，而不是 response mode。

## Task Template

### T-XXXX 标题

状态：`todo`

目标：

- 

建议 owner：

- 

建议范围：

- 

接口约束：

- 

验收标准：

- 

风险：

- 
### T-0102 World Info RAG Runtime Integration

Status: `done`

Summary:
- `WorldInfoEvidenceProvider` is now wired in `app/main.py` as the active main-runtime
  `EvidenceProviderPort`.
- World Info keyword matches enter prompt context through `EvidencePack.lore_results`.
- `uid 160` home/cabin is key-triggered only.
- `uid 86` base worldview is policy-added only for the approved trigger uid set.
- No-match daily input returns an empty evidence pack.

Validation:
- `ruff check .`
- `pytest -q`

### T-0118 Prompt Assembly And JSONL Prompt Run Log

Status: `done`

Summary:
- Reordered prompt context sections so RAG evidence is placed before long-term memory and
  recent conversation stays closest to the final user message.
- Added `PromptRunLoggerPort` with JSONL and noop implementations.
- Runtime prompt logs can be written to `.aemeath/sessions/YYYY/MM/DD/rollout-*.jsonl`.
- Prompt run records include messages, prompt sections, context metadata, response, warnings,
  status, and failure details when available.
- `.aemeath/` is ignored as local runtime data.

Validation:
- `pytest`
- `ruff check .`

### T-0119 Prompt Log Inspect And Early Eval Samples

Status: `done`

Owner:
- Manager Session

Summary:
- Added a lightweight prompt log inspector for local JSONL prompt run logs.
- Added early manual eval samples for daily chat, World Info RAG, user emotion, memory recall,
  and prompt safety checks.
- Kept this as local debugging/eval tooling, not an industrial trace viewer.

Key files:
- `app/observability/prompt_log_inspector.py`
- `scripts/inspect_prompt_log.py`
- `docs/prompt_debug_and_eval.md`
- `tests/test_prompt_log_inspector.py`

Usage:
- `python scripts/inspect_prompt_log.py`
- `python scripts/inspect_prompt_log.py --limit 3`
- `python scripts/inspect_prompt_log.py --show-system`

Validation:
- `pytest`
- `ruff check .`

### T-0120 Memory v0.3 Explicit Long-term Memory

Status: `todo`

Goal:
- Implement a conservative long-term memory path after the prompt debug loop is usable.
- Start with explicit user requests such as "remember ..." rather than broad automatic memory.
- Keep recent conversation as the short-term baseline.

Suggested owner:
- Memory worker session

Suggested scope:
- `app/memory/classifier.py`
- `app/memory/writer.py`
- `app/memory/reader.py`
- `app/agent/dialogue_controller.py`
- `tests/*`

Interface constraints:
- Keep `DialogueController` dependent on memory ports only.
- Do not put interaction memory into `EvidencePack`.
- Log injected memory ids through prompt run logs for debugging.

Acceptance:
- Explicit remember input writes one long-term memory when long-term memory is enabled.
- Later recall can retrieve that memory.
- Minimal runtime with long-term memory disabled still does not write long-term memory.
- `pytest`
- `ruff check .`
