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

2026-07 current-state note: this backlog item is superseded by later Character v0.2 work.
The current runtime defaults to `app/character/Aemeath.v0.2/` manifest via `CharacterRegistry`.
Keep this historical task only as context for remaining character-document governance work.

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

状态：`done`

目标：

- 在 Web UI / API Server 和 Memory scope 扩展前定义一等 Session 对象。
- 为 message history、memory extraction、prompt log 和未来 checkpoint 提供稳定归属。

建议 owner：

- Worker Session 4: Runtime / Session

建议范围：

- 新增 `app/session/*`
- `app/agent/dialogue_controller.py`
- `app/agent/message_store.py`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/main.py`
- `app/cli.py`
- `tests/*`

接口约束：

- 遵守 `docs/session_contract.md`。
- `session_id` 是全局唯一主键；`user_id` 和 `character_id` 是不可变归属字段。
- `AgentTurnInput` v0.1 保持兼容，不改成嵌套 Session 对象。
- Controller 只调用 `SessionStorePort.ensure_session(...)` 做编排，不实现归属策略。
- Session 生命周期为 `active | ended | archived`；ended 只能显式 resume，archived
  在 v0.1 不可恢复。
- CLI 切换当前 session 不等于结束 session；`/session new` 不隐式结束旧 session。
- `message_count` 与 `last_message_at` 是 Session read-model 字段，v0.1 SQLite
  从 messages 精确聚合，不由 Controller 维护计数。
- Session 不替代 Memory；`session_id` 默认是来源和窗口，不是全部长期记忆的隔离键。
- Worker 不修改 Memory v0.3 的提炼、embedding 或召回策略。
- Worker 不直接修改 manager-owned 架构文档；通过交付报告提出必要变更。

验收标准：

- 支持 session create / current / list / end / resume / archive。
- 同一 `session_id` 不能被其他 user 或 character 复用。
- resume 后能够读取之前的 message history。
- ended/archived session 的隐式 turn 被拒绝且无消息、日志或 memory job 副作用。
- Session read model 返回准确的 message count 与 last-message time。
- 当前直接构造 `AgentTurnInput` 的调用通过 `ensure_session` 保持兼容。
- 旧数据库能够幂等回填 sessions，不丢失 messages。
- Memory extraction jobs 继续使用一致的 `session_id + user_id + character_id`。
- 新增最小测试，`pytest` 与 `ruff check .` 通过。

前置设计：

- `docs/session_contract.md`

交付要求：

- 先提交设计核对，不立即实现。
- 实现完成后写入
  `docs/agents_communication/session_v0_1_delivery.md`。

实现摘要：

- 新增一等 `SessionRecord` 与 `SessionStorePort`。
- 支持 `active | ended | archived` 生命周期以及 create/list/end/resume/archive。
- Controller 在任何消息、prompt log、关系状态或 memory job 写入前校验 Session。
- CLI 支持 `/session current|new|list|end|resume|archive`。
- SQLite 幂等回填旧消息和 Memory job provenance。
- `message_count`、`last_message_at` 由 messages 精确聚合。
- 修复非默认角色 Session 写入消息后重启被误判为 legacy `aemeath` 的问题。

验收结果：

- `pytest`：86 passed。
- `ruff check .`：All checks passed。
- Manager 手工验证非默认角色 Session 重启成功。

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

状态：`done`

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

实现摘要：

- 新增 `tests/test_chinese_encoding_and_behavior.py`。
- 自动扫描源码、测试和文档中的常见 UTF-8-as-GBK mojibake 片段。
- 增加 Mock model、memory classifier、state classifier 的中文行为测试。
- 新增 `docs/encoding_policy.md`，记录 Windows 控制台显示乱码与真实文件编码的区分方法。

验收结果：

- `pytest tests/test_chinese_encoding_and_behavior.py`：4 passed。

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

Status: `done`

Goal:
- Implement an LLM-extracted long-term memory path after the prompt debug loop is usable.
- Support explicit remember triggers, 12-message window extraction jobs, and manual CLI extraction.
- Store each long-term memory as a vector-indexed chunk for active recall.
- Keep recent conversation as the short-term baseline.

Suggested owner:
- Memory worker session

Suggested scope:
- `app/memory/classifier.py`
- `app/memory/writer.py`
- `app/memory/reader.py`
- `app/agent/dialogue_controller.py`
- `app/memory/extractor.py`
- `app/memory/worker.py`
- `app/db/sqlite_store.py`
- `app/db/schema.sql`
- `app/cli.py`
- `tests/*`

Interface constraints:
- Keep `DialogueController` dependent on memory ports only.
- Do not put interaction memory into `EvidencePack`.
- Log injected memory ids through prompt run logs for debugging.

Acceptance:
- Explicit remember input enqueues an idempotent memory extraction job when long-term memory is
  enabled.
- The standalone memory worker can process pending jobs into long-term memories and embeddings.
- A 12-message session window enqueues one idempotent window extraction job.
- Manual `/memory extract [limit]` can synchronously extract recent session messages.
- Later turns actively retrieve vector-indexed long-term memories.
- Minimal runtime with long-term memory disabled still does not write long-term memory.
- Prompt run logs include injected `memory_ids`.
- `pytest`
- `ruff check .`

Implementation summary:
- Added `memory_extraction_jobs` and `long_term_memory_embeddings` SQLite tables.
- Added idempotent job enqueue / claim / complete / fail operations.
- Added `MemoryExtractionService` with Chinese JSON-only DeepSeek extraction prompt.
- Added `QueuedMemoryWriter` for hard-trigger and 12-message window jobs.
- Upgraded `MemoryReader` to active embedding recall when an embedding provider is configured.
- Added `python -m app.memory.worker` for standalone job processing.
- Added CLI `/memory extract [limit]` and `/memory debug on|off`.
- Extended `AgentTurnOutput` with `used_memories` summaries while keeping `used_memory_ids`.
- Default memory embedding provider is `hash`; `bge-m3` is explicit advanced configuration.
- Mock LLM mode uses a JSON memory extraction mock so `/memory extract` remains stable.
- CLI memory command branch catches exceptions and prints a non-fatal memory error.
- `docs/architecture.md` and `docs/interfaces.md` document Memory v0.3 runtime contracts.
- Session follow-up adds `SessionStorePort.require_session(...)` for read-only ownership checks.
- Long-term memory now separates `scope_*` visibility fields from `source_*` provenance fields.
- Keyword recall, vector recall, and fallback share one scope visibility predicate.
- Exact dedupe matches only the target scope tuple.
- Existing memories migrate transactionally and once-only to `user_global` through
  `memory_scope_provenance_v1`.
- Memory Admin extract/delete/disable paths enforce Session or memory ownership.

Validation:
- `pytest`: 95 passed
- `ruff check .`: All checks passed

### T-0121 v0.3 Release Closure

Status: `done`

Owner:
- Manager Session

Summary:
- Align package and README version metadata with v0.3.
- Add changelog and detailed migration/release notes.
- Confirm user-visible CLI UTF-8 text and extend the mojibake encoding guard.
- Run full tests, lint, diff checks, and a mock CLI smoke test.
- Commit and publish the `release/v0.3` branch and `v0.3` tag.

Key files:
- `README.md`
- `CHANGELOG.md`
- `docs/releases/v0.3.md`
- `pyproject.toml`
- `app/cli.py`
- `tests/test_chinese_encoding_and_behavior.py`

Acceptance:
- Release docs accurately describe Session v0.1 and Memory v0.3 behavior.
- Upgrade notes explain database backup and automatic migrations.
- No private source documents, local databases, prompt logs, or API keys are committed.
- `pytest`
- `ruff check .`

Validation:
- `pytest`: 95 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app`
- Mock CLI startup, Session creation, and `/exit` smoke test passed with a temporary database.
- Disabled long-term-memory worker exits cleanly.
- Git ignored-file and credential-pattern checks found no release-blocking files or secrets.

### T-0122 Audio / TTS Standalone Workspace Closure

Status: `done`

Owner:
- Manager Session

Summary:
- Record the local GPT-SoVITS Audio workspace as a standalone experimental module.
- Explicitly keep Audio out of the current Agent runtime flow.
- Document the future `TextToSpeechPort` / `AudioOutputPort` direction without implementing it.
- Ignore the local Audio workspace so model weights, source audio, training caches, runtime
  vendor files, archives, scripts, and generated outputs are not accidentally committed while
  Audio remains out of the runtime.

Key files:
- `.gitignore`
- `docs/audio_module.md`
- `docs/architecture.md`
- `docs/interfaces.md`

Current decision:
- Text Agent remains deployable without GPU.
- `Aemeath_roleplay_skill/audio/` is retained as local-only GPT-SoVITS work, but not wired into
  `DialogueController`, CLI, API, Prompt, Memory, RAG, or Session.
- Server deployment should treat TTS as optional, preferably through a separate GPU-backed service
  or adapter.

Validation:
- Documentation-only architecture update.
- No runtime code was changed.

### T-0123 Error Handling v0.1

Status: `done`

Owner:
- Worker Session 6
- Manager Session review

Summary:
- Added a shared error handling layer under `app/errors/`.
- Added stable `ErrorCode`, `ErrorInfo`, `AemeathError`, exception mapping, HTTP status mapping,
  and sensitive-value sanitization.
- Integrated structured error mapping with `DialogueController`, CLI output, DeepSeek retry
  handling, prompt-run failed records, and prompt log inspection.
- Kept Service/API/Auth/Structured Logging out of this scope.

Key files:
- `app/errors/`
- `app/agent/dialogue_controller.py`
- `app/cli.py`
- `app/model/deepseek.py`
- `app/observability/prompt_log_inspector.py`
- `app/observability/schemas.py`
- `docs/error_handling.md`
- `docs/agents_communication/error_handling_v0_1_delivery.md`
- `tests/test_errors.py`
- `tests/test_cli_errors.py`
- `tests/test_prompt_run_logger.py`
- `tests/test_deepseek_client.py`

Interface notes:
- Future API/service layers should reuse `map_exception()`, `ErrorInfo.to_public_dict()`, and
  `http_status_for_error()` instead of inventing separate error response rules.
- Runtime logs and prompt-run records must pass error payloads through the sanitizer before
  persistence.
- The sanitizer covers common API key variants such as `api-key`, `x-api-key`, and `x_api_key`,
  while avoiding broad false positives such as `ordinary-key`.

Manager review follow-up:
- Added tests for hyphenated API key redaction.
- Added failed prompt-run regression coverage.
- Added CLI normal-turn failure coverage for stable `[SESSION_ENDED]` output.

Validation:
- `pytest`: 105 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app`: passed

### T-0124 Observability And Structured Logging v0.1

Status: `done`

Owner:
- Worker Session: Observability And Structured Logging
- Manager Session review

Goal:
- Turn the existing observability ports into a minimal production-friendly logging baseline.
- Keep app logs, trace logs, and prompt-run logs clearly separated.
- Reuse the Error Handling sanitizer and error mapping instead of creating a second error
  taxonomy.

Planned scope:
- Add or refine structured app log schemas and ports.
- Add a JSONL trace logger implementation for existing `TraceLoggerPort`.
- Add runtime configuration for enabling/disabling structured logs and choosing local log roots.
- Wire the default runtime to the new trace logger when enabled; keep noop implementations for
  tests and disabled logging.
- Add tests for JSONL trace/app log records, sensitive-value redaction, failed-turn error fields,
  and prompt-run log compatibility.

Out of scope:
- FastAPI, Web UI, Auth, Service Layer, OpenTelemetry, cloud log shipping, and log query UI.
- Changing prompt-run logs into the general trace system.
- Persisting full system prompts or raw provider responses in trace/app logs by default.

Key files:
- `app/observability/`
- `app/errors/`
- `app/config.py`
- `app/main.py`
- `app/agent/dialogue_controller.py`
- `app/memory/worker.py`
- `scripts/inspect_prompt_log.py`
- `docs/observability.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `tests/`

Acceptance:
- Worker first submits a short design summary before implementation.
- App log, trace log, and prompt-run log boundaries are documented.
- JSONL trace/app logs use UTF-8 and `ensure_ascii=False`.
- Sensitive keys and text fragments are redacted using `app/errors/sanitizer.py`.
- Existing prompt-run logger and inspector tests still pass.
- `pytest`
- `ruff check .`

Implementation summary:
- Added `AppLogRecord` and `AppLoggerPort`.
- Added `JsonlAppLogger` and `NoopAppLogger` as the minimal app/process logging contract.
- Added `JsonlTraceLogger` for existing `TraceLoggerPort`.
- Added trace/app log configuration:
  - `AEMEATH_ENABLE_TRACE_LOG=false`
  - `AEMEATH_TRACE_LOG_ROOT=.aemeath/traces`
  - `AEMEATH_ENABLE_APP_LOG=false`
  - `AEMEATH_APP_LOG_ROOT=.aemeath/app_logs`
- Wired `app/main.py` to use `JsonlTraceLogger` when trace logging is enabled.
- Kept prompt-run logging separate and unchanged.
- Kept `AppLoggerPort` out of `DialogueController`; app logs remain for CLI/API/service/worker
  boundaries.
- Added focused tests for UTF-8 JSONL, `ensure_ascii=False`, redaction, enabled trace events,
  failed-turn error trace data, and disabled trace logging.

Manager acceptance:
- Accepted on 2026-07-06.
- No blocking issues found.
- App Log, Trace Log, and Prompt Log boundaries remain separate.
- `AppLoggerPort` is intentionally not injected into `DialogueController` in v0.1.
- Future Service/API/Worker Operations work may reuse `AppLoggerPort` at process and boundary
  layers.

Validation:
- `pytest`: 110 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app tests`: passed
- `git diff --check`: no whitespace errors, CRLF warnings only

### T-0125 Agent Service Layer v0.1

Status: `done`

Owner:
- Worker Session 8: Service Layer
- Manager Session review

Goal:
- Insert a stable application service boundary between CLI / future API / Web adapters and
  `DialogueController`.
- Give future Auth, FastAPI, Web, Streaming, Audio, and App Logging work a single integration
  point instead of letting them call the controller directly.
- Keep `DialogueController` as an orchestration layer and avoid moving Character, Memory, RAG,
  Prompt, Model, or State business policy into service code.

Planned scope:
- Add `app/services/` with minimal schemas and `AgentService`.
- Define `UserContext` as the future Auth insertion point.
- Define chat request/response DTOs around the existing `AgentTurnInput` / `AgentTurnOutput`.
- Decide, with manager approval, whether service methods return a structured `Result` object or
  raise mapped exceptions for adapter layers to present.
- Optionally add a thin `SessionService` if it removes CLI/session command coupling without
  expanding into a full account or permission system.
- Optionally migrate CLI normal chat to `AgentService` if behavior remains unchanged and file
  churn stays small.
- Reuse `app/errors` for error mapping and `app/observability` ports where appropriate.

Out of scope:
- Account registration/login.
- Token, cookie, OAuth, or password flows.
- Full owner-only permission model.
- FastAPI, Web UI, streaming API, SSE, WebSocket, and Audio output.
- Rewriting `DialogueController` or module business logic.
- Changing prompt assembly, memory policy, RAG policy, character behavior, or model provider
  logic.

Key files:
- `app/services/`
- `app/main.py`
- `app/cli.py`
- `app/schemas.py`
- `app/errors/`
- `app/observability/`
- `app/session/`
- `docs/session_contract.md`
- `docs/error_handling.md`
- `docs/observability.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `tests/`

Acceptance:
- Worker first submits a short design proposal before implementation.
- Service layer exposes stable DTOs and does not leak controller internals to future API/Web
  adapters.
- `UserContext` exists as a minimal future Auth insertion point.
- Existing CLI behavior is unchanged if CLI is migrated.
- Tests cover successful chat, session-ended failure, archived/invalid session behavior where
  practical, unknown character fallback, and error mapping shape.
- `DialogueController` remains orchestration-only.
- `pytest`
- `ruff check .`

Implementation summary:
- Added `app/services/` with `UserContext`, `ChatRequest`, `ChatResponse`,
  `UsedMemorySummary`, and `AgentService`.
- `AgentService.handle_chat(...)` returns `ChatResponse` on success and re-raises original
  exceptions on failure; adapters continue to use `map_exception(exc)`.
- Did not add `ServiceResult` / `ServiceError`.
- Added `build_agent_service(...)` in `app/main.py`, preserving existing `build_controller(...)`.
- Migrated only the CLI normal chat path to `AgentService`; `/session`, `/memory`, `/memory debug`,
  and CLI session command helpers remain unchanged.
- Deferred `SessionService`.
- `AgentService` accepts `AppLoggerPort` and defaults to `NoopAppLogger`; service app logs record
  summary fields only and do not include prompt text, model messages, user message text, provider
  raw responses, or secrets.
- Added service tests for successful chat, ended session failure, archived session failure,
  unknown character fallback warning, error mapping, and app-log secret non-disclosure.

Manager acceptance:
- Accepted on 2026-07-06.
- No blocking issues found.
- `AgentService` remains a thin application boundary and does not absorb Character, Memory, RAG,
  Prompt, Model, State, or Session policy.
- `ChatResponse.character_id` as the requested target character is acceptable for v0.1; active
  fallback character can be added later if API/Web needs it.
- Keeping `build_controller(...)` and adding `build_agent_service(...)` is the right transition
  shape while direct controller tests still exist.
- CLI normal chat migration is accepted; `/session`, `/memory`, and `/memory debug` staying on the
  old path is intentional.
- `SessionService`, Auth, owner-only permissions, FastAPI, Web, Streaming, and Audio remain
  deferred.

Validation:
- `pytest`: 115 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app tests`: passed
- `git diff --check`: no whitespace errors, CRLF warnings only

### T-0126 Auth / Account System v0.1

Status: `done`

Owner:
- Worker Session 9: Auth / Account System

Goal:
- Add a local account model and default account bootstrap.
- Add optional dev/static token parsing that resolves to `UserContext`.
- Add a minimal owner-only permission helper.
- Keep Auth and permissions out of `DialogueController`.

Scope:
- Added `app/accounts/` with `AccountRecord`, `AccountStorePort`, and `AccountService`.
- Added `app/auth/` with dev token hashing, `AuthService`, local default resolver, and auth
  errors.
- Added `app/permissions/` with `PermissionService.require_owner(...)`.
- Added an `accounts` SQLite table and idempotent historical `user_id` backfill.
- Added config:
  - `AEMEATH_DEV_AUTH_TOKEN`
  - `AEMEATH_DEV_AUTH_USER_ID`
  - `AEMEATH_DEFAULT_USER_ID`
  - `AEMEATH_DEFAULT_CHARACTER_ID`
- Added error codes:
  - `AUTH_REQUIRED`
  - `AUTH_INVALID_CREDENTIALS`
  - `AUTH_ACCOUNT_DISABLED`
  - `PERMISSION_DENIED`

Boundary:
- CLI continues to use the default local account and user-visible behavior is unchanged.
- `AgentService` trusts the supplied `UserContext`.
- `AuthService` and account bootstrap handle account status and dev token resolution.
- Session ownership failures continue to use `SESSION_OWNERSHIP_DENIED`.
- Generic non-session owner-only failures use `PERMISSION_DENIED`.
- Raw tokens, cookies, authorization headers, and passwords must not be logged or returned in
  public errors.

Key files:
- `app/accounts/`
- `app/auth/`
- `app/permissions/`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/config.py`
- `app/main.py`
- `app/errors/`
- `docs/auth_and_permissions.md`
- `tests/test_accounts.py`
- `tests/test_auth.py`
- `tests/test_permissions.py`

Deferred:
- FastAPI
- Web UI
- username/password login
- password hashing
- registration/login pages
- OAuth
- refresh tokens
- cookie session
- admin UI
- complex RBAC
- shared sessions
- `DialogueController` auth changes

Manager acceptance:
- Accepted on 2026-07-07.
- No blocking issues found.
- Scope stayed within local accounts, default account bootstrap, optional dev/static token auth,
  and minimal owner-only permission helper.
- No username/password login, registration, FastAPI, Web, OAuth, refresh-token, cookie-session,
  complex RBAC, or `DialogueController` auth flow was introduced.
- `AgentService` continues to trust `UserContext`; account status and token resolution stay in
  Auth/Account services.
- Added `.env.example` entries for default identity and dev auth token settings.

Validation:
- `pytest`: 125 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app tests`: passed
- `git diff --check`: no whitespace errors, CRLF warnings only

### T-0127 SQLAlchemy Persistence Backend v0.1

Status: `done`

Owner:
- Worker Session 10: SQLAlchemy Persistence Design

Goal:
- Add an opt-in SQLAlchemy persistence backend while keeping `sqlite_raw` as the default.
- Internally split SQLAlchemy persistence into repositories and expose a temporary
  `SqlAlchemyStore` facade for current port compatibility.
- Avoid PostgreSQL, async SQLAlchemy, Alembic revisions, old `data/aemeath.db` migration, and
  removal of `SQLiteStore`.

Scope:
- Added SQLAlchemy as a runtime dependency.
- Added SQLAlchemy models and fresh SQLite `create_all` initialization.
- Added repository split:
  - `MessageRepository`
  - `SessionRepository`
  - `AccountRepository`
  - `RelationshipRepository`
  - `MemoryRepository`
  - `MemoryJobRepository`
  - `MemoryEmbeddingRepository`
- Added `SqlAlchemyStore` facade that only composes and forwards to repositories.
- Added `create_store(...)` factory and `AEMEATH_DB_BACKEND=sqlite_raw|sqlalchemy`.
- Wired `app/main.py` and `app/memory/worker.py` through the store factory.
- Added `docs/persistence.md`.
- Added parameterized store contract tests under `tests/store_contract/`.

Boundary:
- Default backend remains `sqlite_raw`.
- SQLAlchemy backend is opt-in and experimental.
- `SQLiteStore` remains intact.
- No Alembic files were added.
- No PostgreSQL or async support was added.
- No `DialogueController`, Character, RAG, Prompt, or Model strategy changes.
- `app/memory/worker.py` was touched only for store factory wiring; memory business logic was not
  changed.
- `app/session/*` was not modified.

Current SQLAlchemy backend support:
- Fresh SQLite databases through SQLAlchemy models.
- Existing store ports through `SqlAlchemyStore`.
- Account bootstrap for `user_default`.
- Session/message read model parity for current tests.
- Memory scope visibility, exact dedupe, jobs, and embedding BLOB recall for current tests.

Deferred:
- Alembic baseline and migration files.
- PostgreSQL dialect support.
- async SQLAlchemy.
- Old raw SQLite database migration into SQLAlchemy backend.
- Making SQLAlchemy the default backend.
- Full infinite-loop memory worker process test; current coverage uses a single-job worker smoke.

Validation:
- `pytest tests/store_contract -q`: 24 passed
- SQLAlchemy normal runtime smoke: passed
- SQLAlchemy long-term memory job enqueue smoke: passed
- SQLAlchemy disabled memory worker smoke: passed
- SQLAlchemy enabled memory worker single-job smoke: passed
- SQLAlchemy two-store job claim smoke: passed
- Embedding shape mismatch parity test: passed
- `pytest`: 149 passed
- `ruff check .`: All checks passed

Manager review:
- Accepted on 2026-07-07.
- Memory module review passed; no blocking findings.
- Persistence worker added the requested SQLAlchemy memory worker smoke, concurrent claim smoke,
  and embedding shape mismatch parity test.
- Session module found and persistence worker fixed a `list_sessions(..., limit=N)` ordering bug:
  SQLAlchemy now sorts by `updated_at DESC` before applying `LIMIT`, matching raw SQLite.
- Added a cross-backend session contract test for newest-session windowing.
- Re-ran `pytest -q tests/store_contract/test_sqlalchemy_memory_worker_smoke.py`: 2 passed.
- Re-ran `pytest -q tests/store_contract/test_memory_contract.py`: 10 passed.
- Re-ran `pytest -q tests/store_contract/test_session_message_contract.py`: 6 passed.
- Re-ran `pytest -q tests/store_contract`: 24 passed.
- Re-ran `pytest`: 149 passed.
- Re-ran `ruff check .`: All checks passed.
- Re-ran `python -m compileall -q app tests`: passed.
- Re-ran `git diff --check`: no whitespace errors, CRLF warnings only.
- Architecture decision confirmed: keep `sqlite_raw` as default, keep SQLAlchemy backend opt-in,
  and keep `SqlAlchemyStore` as a temporary facade over repositories until service/API boundaries
  are ready to accept narrower repository injection.

Follow-up review requests:
- Memory module review: passed on 2026-07-07.
- Session module review: passed on 2026-07-07 after the `list_sessions` ordering fix.

### T-0128 FastAPI Service v0.1

Status: `done`

Owner:
- Worker Session 11: FastAPI Service v0.1

Goal:
- Add the first HTTP API adapter around the existing service/auth/session/error/logging
  boundaries.
- Keep `DialogueController` free of HTTP, token, cookie, account, and permission logic.
- Do not add Web UI, streaming, username/password login, OAuth, refresh tokens, PostgreSQL,
  Alembic, Docker, or runtime strategy changes.

Scope:
- Added `app/api/` with `create_app(settings: Settings | None = None)`.
- Added versioned endpoints:
  - `GET /health`
  - `POST /v1/chat`
  - `GET /v1/sessions`
  - `POST /v1/sessions`
  - `GET /v1/sessions/{session_id}/messages`
- Added a thin `SessionService` for session/message HTTP boundary behavior.
- Added `AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER=false`; `/v1/*` requires bearer token by default.
- Added optional `api` dependency extra and FastAPI/httpx test dependencies in `dev`.
- Documented API runtime and the current `POST /v1/chat` `ensure_session(...)` auto-create
  behavior in `docs/fastapi_service.md` and README.

Boundary:
- Chat route calls `AgentService.handle_chat(...)`, not `DialogueController`.
- Auth route dependency reuses `AuthService` / `LocalDefaultAccountResolver` and emits
  `UserContext`.
- API errors reuse `map_exception(...)`, `ErrorInfo.to_public_dict()`, and
  `http_status_for_error(...)`.
- FastAPI validation errors map to `INVALID_INPUT` and do not expose FastAPI `detail`.
- API app logs record only summary fields and keep App Log, Trace Log, and Prompt Log separate.
- Streaming remains deferred for a future `/v1/chat/stream`.

Validation:
- `pytest`: 161 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app tests`: passed

Manager review:
- Accepted on 2026-07-08.
- Re-ran `pytest -q tests/test_api.py`: 12 passed.
- Re-ran `pytest`: 161 passed.
- Re-ran `ruff check .`: All checks passed.
- Re-ran `python -m compileall -q app tests`: passed.
- Re-ran `git diff --check`: no whitespace errors, CRLF warnings only.
- Confirmed chat route calls `AgentService.handle_chat(...)` and does not import
  `DialogueController`.
- Confirmed `/v1/*` auth defaults to bearer token and local default-user API access is explicit
  opt-in via `AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER=true`.
- Confirmed API errors reuse the existing error mapper/status helper and validation errors use
  the stable `INVALID_INPUT` shape.
- Confirmed session message listing rejects cross-user access through `SessionService`.

Warnings:
- Test run emits dependency/runtime warnings: FastAPI `TestClient` / Starlette deprecation and
  Python 3.12 SQLite datetime adapter deprecation. These are non-blocking and do not affect the
  FastAPI v0.1 acceptance.

### T-0129 Architecture Review Follow-up Fixes

Status: `done`

Owner:
- Manager Session

Source:
- `docs/agents_communication/architecture_workflow_review_v0_1.md`

Goal:
- Fix the high-priority and small medium-priority issues found by Architecture And Workflow Review
  v0.1 before continuing with Streaming API.

Scope:
- Added `AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false`.
- FastAPI app factory now builds API runtime settings with prompt-run logging controlled by the
  API-specific flag, keeping CLI local-debug behavior separate.
- Startup now clears the configured dev user's `dev_token_hash` when `AEMEATH_DEV_AUTH_TOKEN` is
  unset, making token removal revoke the previous dev token for that database/account.
- `AgentService` now copies `UserContext.request_id` into `AgentTurnInput.metadata`, allowing
  trace/prompt metadata to correlate with API request ids.
- Memory worker failure handling now persists and prints sanitized mapped errors instead of raw
  exception text.
- FastAPI runtime disables model fallback by default through
  `AEMEATH_API_ENABLE_MODEL_FALLBACK=false`, while CLI fallback remains enabled by default.
- Updated README, `.env.example`, `docs/fastapi_service.md`, `docs/observability.md`, and
  `docs/auth_and_permissions.md`.

Deferred:
- API/model fallback policy is resolved for v0.1: CLI fallback remains enabled by default, while
  FastAPI fallback defaults to disabled through `AEMEATH_API_ENABLE_MODEL_FALLBACK=false`.
- `PermissionService` remains future-only until a generic non-session permission boundary needs it.
- Full docs drift cleanup remains a follow-up docs task.

Validation:
- `pytest -q tests/test_api.py`: 16 passed.
- `pytest -q tests/test_api.py tests/test_auth.py tests/test_memory_worker.py`: 26 passed.
- `pytest`: 168 passed.
- `ruff check .`: All checks passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: no whitespace errors, CRLF warnings only.

Warnings:
- FastAPI `TestClient` / Starlette deprecation warning remains.
- Python 3.12 SQLite datetime adapter deprecation warning remains.

### T-0130 Lightweight Current-State Docs Refresh

Status: `done`

Owner:
- Manager Session

Source:
- Architecture And Workflow Review v0.1 docs drift findings.

Goal:
- Refresh the most visible current-state documentation after FastAPI v0.1, SQLAlchemy persistence
  v0.1, and architecture-review follow-up fixes.

Scope:
- Updated README validation count to the latest full test run.
- Added current-state notes in `docs/architecture.md` clarifying that
  `WorldInfoEvidenceProvider` is the active main-runtime evidence provider and that API model
  fallback is disabled by default.
- Updated `docs/interfaces.md` provider list to include the active `WorldInfoEvidenceProvider`.
- Marked historical Character Manifest backlog item as superseded by later Character v0.2 work.

Deferred:
- Full rewrite of historical planning documents.
- Large-scale encoding cleanup of older archived notes.

Validation:
- Documentation-only change; no runtime tests required.

### T-0131 Embedding Foundation v0.1

Status: `done`

Owner:
- Worker Session 13: Embedding Foundation

Goal:
- Extract embedding into a shared, provider-neutral foundation for Memory, RAG, Character examples,
  and future context-budget selection.
- Make vector-space identity explicit and prevent vectors from different signatures from mixing.

Scope:
- Added the batch-first `app/embeddings/` port, schemas, provider factory, profile loader,
  qualification CLI, typed errors, and AppLogger integration.
- Added SiliconFlow BGE-M3, local BGE-M3, and deterministic Hash providers.
- Preserved `app/retrieval/embeddings.py` and legacy Memory methods for one compatibility release.
- Added canonical profile packaging, path/digest validation, release gates, batch ordering, and safe
  provider diagnostics.

Release decision:
- `deterministic_hash_v1` is approved for deterministic tests and explicit development.
- T-0138 later qualified and approved `siliconflow_pro_bge_m3_v1` for normal API-backed runtime.
  `local_bge_m3_v1` remains `review`; runtime code must not bypass the approval gate.
- Provider failures never trigger an implicit Hash fallback.

Validation:
- Embedding delivery suite: 114 passed.
- Delivery full suite: 282 passed.
- Hash qualification, wheel resource packaging, Ruff, compileall, and diff checks passed.

### T-0132 Canonical Embedding Artifact Persistence v0.1

Status: `done`

Owner:
- Persistence Worker
- RAG Worker review
- Manager Session acceptance

Goal:
- Persist exact-signature, content-bound embedding artifacts for Memory and RAG without changing
  their business workflows.
- Keep raw SQLite and SQLAlchemy behavior aligned while preserving legacy evidence for one
  compatibility release.

Scope:
- Added canonical artifact helpers and Memory/RAG artifact tables.
- Artifact identity is `target id + signature fingerprint + purpose`; different spaces coexist.
- Added content hash, canonical signature JSON, vector format/dimension, vector digest, and stale or
  corrupt artifact validation.
- Added SQL fingerprint/purpose prefiltering, SQLite writer serialization, and raw/SQLAlchemy
  concurrency parity.
- Added strict SQLAlchemy artifact schema inspection with `STORAGE_MIGRATION_REQUIRED` for incomplete
  experimental databases.
- Migrates only byte-for-byte verified legacy deterministic Hash vectors. Other legacy rows remain
  isolated and diagnosable without blocking startup.
- RAG migration cleanup is idempotent, including databases whose migration marker already exists.

Boundary:
- `MemoryReader`, Memory extraction policy, Session behavior, `DialogueController`, and active
  `WorldInfoEvidenceProvider` behavior were not changed.
- Canonical APIs are parallel to the legacy APIs and become active only through later Memory/RAG
  integration tasks.

Validation:
- Persistence worker full suite: passed.
- Manager full regression during review: 304 passed.
- Final RAG review: 34 passed; no blocking or non-blocking findings.
- Ruff, compileall, and diff checks passed.

Manager review:
- Accepted on 2026-07-26 after Persistence fixed exact legacy-row accounting and idempotent RAG
  temporary-table cleanup.
- RAG independently confirmed verified-Hash suppression, BGE/unknown/corrupt legacy reporting,
  namespace isolation, compatibility entry points, and repeated migration initialization.

### T-0133 Memory Canonical Embedding Integration

Status: `done`

Owner:
- Worker Session 3: Memory
- Persistence Worker for approved store-contract additions

Goal:
- Move Memory extraction and recall from compatibility methods to exact-signature canonical
  artifacts without changing `DialogueController` or Memory scope/provenance rules.

Approved direction:
- Commit Memory facts before generating derived document artifacts.
- Derive pending state from active Memories lacking a valid artifact for the selected exact
  signature; do not add a pending table in the first version.
- Batch document embedding, idempotent artifact upsert, and bounded single-worker repair.
- Use exact-signature vector recall; when coverage is incomplete or query embedding fails, use
  explicit keyword supplementation without comparing keyword scores to cosine scores.
- Preserve explicit Chinese recall fallback behavior.
- Remove synthetic query warmup and wire Memory/embedding business events through AppLogger without
  logging content, queries, vectors, hashes, provider bodies, or secrets.

Required contract work:
- Completed by Persistence: atomic exact-active Memory create-or-get with stable scope/content
  identity, including raw/SQLAlchemy partial unique expression-index enforcement and fail-fast handling
  of legacy duplicate active identities.
- Completed by Persistence: eligible Memory count using the same scope, namespace, type, and confidence
  filters as canonical recall.
- Completed by Persistence: stable, bounded exact-signature artifact-gap listing for repair. The API is
  Worker-internal, can scan across users, and has no scheduling/retry policy.
- Provider-neutral query embedding source returning an exact signature and vector.
- Repair cadence/backoff and unambiguous fact/artifact result fields.

Dependency:
- T-0131 and T-0132 are complete.
- SiliconFlow remains unavailable to normal runtime until its profile receives separate approval.

Manager review:
- Persistence prelude accepted on 2026-07-26 after strict raw SQLite/SQLAlchemy parity review.
- The active-memory identity gate now verifies SQLite index metadata, exact key expressions, and an
  exact partial predicate while preserving single-quoted literal case, whitespace, escaped quotes,
  and literal-contained parentheses during normalization.
- Contract coverage confirms malformed expanded, narrowed, and literal-mutated indexes fail with
  `STORAGE_MIGRATION_REQUIRED`; archived identities remain recreatable after restart.
- Memory reader, extractor, repair, worker, and observability integration was delivered on
  2026-07-26.
- Manager runtime review confirmed the Extractor, Repair, Worker, observability, Persistence, and
  cross-module boundaries, but requested two focused Reader fixes before final acceptance:
  canonicalize direct query text with the same NFC/LF rules used for document artifacts, and
  re-check hydrated vector records against active/status and query filters before prompt use.
- Final Manager acceptance completed on 2026-07-26 after both Reader fixes landed. Regression coverage
  now includes NFC and CRLF/LF-equivalent query recall plus archive/disable races between vector search
  and record hydration; invalidated records are neither returned nor marked as used.
- T-0133 is complete. Approved-profile expansion, multi-worker repair coordination, extraction-job
  retry, and shared-turn query embedding remain separate follow-up work rather than acceptance gaps.

### T-0134 RAG Canonical Managed Generation Runtime

Status: `done`

Owner:
- Worker Session 2: Retrieval / RAG
- Persistence Worker for the completed generation-store prerequisite interfaces

Goal:
- Adapt vector RAG to the approved Embedding Foundation and canonical artifact store without
  replacing the active `WorldInfoEvidenceProvider`.
- Guarantee that query-time retrieval uses one exact-signature, complete, immutable ready
  generation and never mixes vector spaces or half-built artifacts.

Scope:
- Added canonical corpus inventory and retrieval-metadata fingerprints.
- Added isolated SQLite generation directories, file manifests, checkpoints, OS process locking,
  and atomic active-generation mapping.
- Added batch-first document builds, full/incremental rebuild selection, stale chunk deletion,
  interruption resume, ready validation, and provider-neutral canonical query retrieval.
- Added standalone `build`, `status`, and `query` tooling under
  `app.retrieval.manage_rag_index`; no vector provider was wired into `app/main.py`.
- Kept legacy RAG APIs as one-release compatibility shims and prohibited legacy vector promotion
  into managed generations.

Initial Manager review:
- Rejected on 2026-07-27 because corrupted ready generations could be reactivated, incomplete
  generation directories blocked the index, changed write batch size could resume an incompatible
  batch-index checkpoint, staging resume skipped inventory validation, and failed active-file
  verification did not restore the previous active bytes.

Review revision:
- Added one `validate_ready_generation()` path used by registry resolution, no-op, ready reuse,
  activation, and future rollback activation.
- Ready validation now checks inventory identity/order/fingerprint, read-only SQLite integrity,
  WAL state, exact-signature coverage, and zero artifact diagnostics.
- Corrupt or incomplete non-active generation directories are isolated with safe diagnostics;
  generation numbers are allocated from all valid directory-name prefixes.
- Added `write_batch_size` to build-plan identity so a changed batch partition never consumes an
  old batch-index checkpoint.
- Staging resume validates persisted inventory before any ready transition.
- Active-file replacement now snapshots and restores the previous bytes when reopen verification
  fails.
- Canonical retrieval returns `index_corrupt` with no hits when artifact diagnostics appear.

Boundary:
- Persistence schema and prerequisite APIs were not changed.
- Memory, Session, Controller, and Character behavior were not changed.
- `app/main.py` continues to use `WorldInfoEvidenceProvider`; managed vector RAG activation still
  requires a separate Manager decision.

Validation:
- Focused RAG generation/Persistence suite: 31 passed.
- Full suite after review revision: 380 passed.
- Ruff, compileall, and diff checks passed; diff check reported CRLF conversion warnings only.

Final Manager acceptance:
- Accepted on 2026-07-27 after independently re-running the focused and full regression suites and
  reviewing all six fault-injection scenarios. No blocking or non-blocking correctness findings
  remain in the approved T-0134 scope.
- Full ready-generation validation currently runs during registry resolution. Before managed RAG is
  wired into the dialogue hot path, benchmark this behavior and, if needed, add a correctness-safe
  validated-handle cache keyed by immutable active-generation identity.
- T-0138 later completed SiliconFlow BGE-M3 Profile approval. Production corpus build review and
  active `EvidenceProviderPort` composition remain separate follow-up gates. The World Info provider
  stays active until those gates are explicitly approved.

### T-0135 Semantic Query Shared Turn Context v0.1

Status: `done`

Owner:
- Worker Session 15: Semantic Query
- Manager Session for cross-worker review coordination and final acceptance

Goal:
- Construct one deterministic, non-persistent semantic query per dialogue turn and share its exact
  resolved embedding with Memory and future RAG / Character consumers.
- Prevent duplicate query embedding calls while keeping retrieval policy inside each consumer.

Scope:
- Added `app/semantic/` schemas, versioned query budget, deterministic dependency-message builder,
  coordinator port, disabled implementation, typed errors, and safe observability.
- Added `SynchronizedEmbeddingProvider` around the public observable Provider returned by the
  existing Factory. The complete delegate call, including retry/backoff, is serialized without
  changing provider batch behavior or terminal events.
- Added a Local BGE query token guard that uses untruncated tokenizer output and preserves the
  existing document truncation path.
- `DialogueController` excludes the current saved message by id, calls the coordinator once, and
  passes the same immutable `TurnSemanticContext` to Memory and Evidence ports.
- Memory consumes the shared `memory_recall` vector when ready and does not perform a second query
  embedding. Failed/skipped contexts preserve keyword and explicit-recall fallback.
- Evidence providers accept the optional context; active World Info behavior remains unchanged.

Boundary:
- Only `memory_recall` is active in the current composition root.
- Managed canonical RAG remains tooling-only; Character example selection remains static.
- No query/vector persistence, cross-turn cache, Context Budget, Persistence schema, Profile
  approval, or implicit Hash fallback was added.
- At T-0135 acceptance, SiliconFlow and local BGE-M3 remained `review` and required fresh
  qualification after the canonical canary formatting change. T-0138 later approved only the
  SiliconFlow Profile; local BGE-M3 remains `review`.

Cross-worker review:
- Embedding Foundation accepted the wrapper, Local BGE token guard, exception behavior, and
  composition order after the canary line-ending issue was resolved.
- Memory accepted ready-vector consumption, no-second-call behavior, fallbacks, and scope/provenance
  preservation.
- RAG accepted the optional Evidence interface and confirmed World Info and managed-generation
  boundaries remain unchanged.
- Character accepted that its provider, rules, and static examples remain unchanged.
- Observability accepted content-free summaries, allowlisted terminal events, and redaction
  boundaries.

Manager acceptance:
- Accepted on 2026-07-28 after five cross-worker reviews and final canary governance.
- `bge_m3_canary_v1.json` is UTF-8 without BOM, uses LF, and has exactly one trailing LF.
- Both BGE profiles reference the new canonical artifact digest. Older qualification candidates are
  stale and must not be approved; T-0138 used a fresh candidate to approve only SiliconFlow.

Validation:
- Full suite: 422 passed.
- `ruff check .`: passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed; Windows checkout reported only expected CRLF conversion warnings.

### T-0136 Deterministic Prompt Context Budget v0.1

Status: `done`

Owner:
- Worker Session 14: Context Budget And Compression
- Manager Session for final contract review

Goal:
- Add a deterministic, optional prompt-size guard after context assembly without changing Semantic
  Query, retrieval, Character source selection, or model-provider interfaces.

Scope:
- Added `ContextCompressorPort`, `ContextBudgetPolicy`, `NoopContextCompressor`, and
  `DeterministicContextCompressor`.
- Added exact compiled-message character estimation shared with `PromptCompiler`.
- Added default-off settings for 24000 chars, 8 recent messages, 4 Memories, and 4 results per Canon
  and Voice section.
- Protected identity/rules, safety boundaries, relationship state, unknown sections, and current
  input. Optional sections trim in Style, Voice, Canon, Memory, Recent order while preserving
  upstream item order.
- Added safe `context_budget` metadata plus `context_budget_applied` trace without removed content.
- CLI and FastAPI share the same policy through the existing composition root.

Boundary:
- No tokenizer, LLM summary, vector selection, secondary embedding call, cross-turn cache, or
  Persistence change was added.
- Existing public memory/evidence ids retain retrieval semantics; final Prompt membership is exposed
  only under the budget report.
- The default remains disabled pending real prompt-quality evaluation.

Manager acceptance:
- Accepted on 2026-07-28 with no blocking or non-blocking implementation findings.
- Manager added one regression test covering the complete cross-section trim priority.
- Remaining product risks are the character-count/token-count difference, allowed protected-floor
  overflow, and voice-quality impact when Style examples are removed.

Validation:
- Focused Context/Semantic/Prompt/Observability suite: 53 passed before the Manager priority test.
- Final full suite: 438 passed.
- `ruff check .`: passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed; Windows checkout reported only expected CRLF conversion warnings.

### T-0137 Data Evolution Contract v0.1

Status: `pending`

Priority:
- Before external multi-user beta; deferred during the current local runtime validation.

Goal:
- Keep existing Sessions, source messages, Character behavior, and derived Memory data evolvable
  after real conversations have been created.

Planned scope:
- Record the Character revision used by each Session and define compatible upgrade behavior.
- Add explicit Memory schema and extraction-policy revisions without changing source-message truth.
- Define re-extraction, supersession, archive, rollback, and backup rules for derived Memories.
- Preserve exact-signature embedding artifacts as rebuildable derived data.
- Add raw SQLite and SQLAlchemy migration/contract tests before enabling the policy for production
  data.

Boundary:
- This task is documentation/backlog only in the current cycle. No schema, Session, Character, or
  Memory runtime change is included yet.

### T-0138 SiliconFlow BGE-M3 Profile Qualification And Runtime Activation

Status: `done`

Owner:
- Manager Session

Goal:
- Complete the real SiliconFlow `Pro/BAAI/bge-m3` qualification gate and make its canonical Profile
  available to the normal Memory/Semantic runtime without bypassing Foundation safety checks.

Required work:
- Capture a fresh candidate against the current version-controlled BGE canary artifact.
- Review per-observation cosine and coordinate-delta metrics and freeze explicit thresholds.
- Commit the reviewed binary baseline and exact digests while the Profile remains `review`.
- Run a real API `verify`, then separately promote the Profile to `approved` only after it passes.
- Select the approved Profile in local `.env`, disable Hash, and smoke-test Memory query/document
  embedding through the normal Factory.
- Update Embedding, architecture, configuration, and validation documentation.

Boundary:
- Do not expose the SiliconFlow API key, provider response bodies, canary text, or vectors.
- Do not delete deterministic Hash artifacts; exact-signature spaces remain valid side by side.
- Do not switch active World Info evidence retrieval or managed vector RAG in this task.

Delivery:
- Captured a fresh candidate from the current canonical `bge_m3_canary_v1` artifact and committed a
  24,576-byte float32 baseline for six 1024-dimensional canaries.
- Approved thresholds are cosine per vector >= `0.9999` and maximum absolute coordinate delta <=
  `0.003`. Fresh capture and independent/default-batch verification both passed.
- The committed baseline digest is
  `sha256:2636ec030d9d8bf123ad0e3ab016b20f36bca8de259756d93c26b1a3f0762b63`; the
  qualification fingerprint is
  `sha256:9117a6ed90373361c1374c4b87e065c451ac4e38e9ee3abc8735317b70b1d64e`.
- Formal API verification observed minimum cosine `0.9999483512870173` and maximum coordinate delta
  `0.0024224817752838135`; every batch and single-order observation passed.
- Promoted `siliconflow_pro_bge_m3_v1` to `approved` only after formal verification. Normal Factory
  query/document calls returned the canonical 1024-dimensional signature.
- Local `.env` now selects the SiliconFlow Profile with Hash disabled. A temporary-database smoke
  created a Memory fact, wrote its document artifact, and recalled it through embedding similarity.
- Existing deterministic Hash artifacts were retained. Active evidence remains
  `WorldInfoEvidenceProvider`; managed vector RAG was not wired into the dialogue runtime.

Validation:
- Embedding/Memory/Semantic focused suite: 174 passed.
- Full project regression: 437 passed.
- Ruff, compileall, wheel resource packaging, and diff checks passed. Details are recorded in
  `docs/agents_communication/siliconflow_bge_m3_profile_approval_delivery.md`.

### T-0139 Runtime Guardrails v0.1

Status: `done`

Owner:
- Worker Session 16; final acceptance by Manager Session.

Goal:
- Add the minimum single-process admission, cost, transport, lifecycle, and safe-observability
  controls needed before a small FastAPI deployment without adding policy to `DialogueController`.

Delivery:
- Added `(user_id, session_id)` serialization plus bounded dialogue, shared LLM, and single-slot
  Embedding concurrency gates.
- Added authenticated FastAPI user/IP token buckets with bounded LRU cleanup, stable `429`, and the
  longest applicable `Retry-After`.
- Added pre-persistence message/metadata limits, persistent DeepSeek HTTP transport with phase
  timeouts, bounded retries/output, and CLI-dialogue-only fallback isolation.
- Added explicit `ApplicationRuntime`, `RuntimeResources`, and `EmbeddingProviderRuntime` ownership;
  FastAPI, CLI, Memory Worker, qualification, and RAG tooling close owned resources deterministically.
- Made App/Trace logging best effort and all JSONL writers safe for concurrent lines while preserving
  Prompt Log's fail-loud behavior.
- Documented the single-process and trusted-proxy deployment boundary; distributed rate limits and
  multi-worker coordination remain deferred.

Manager acceptance:
- Initial review found close re-entry deadlocks, incorrect dual-bucket `Retry-After`, unbounded bucket
  sweeping, an invalid configurable Embedding concurrency path, and a missing keepalive setting.
- All five corrections were implemented and covered by focused fault-injection tests.
- Final independent validation on 2026-07-29: 43 focused tests and 506 full-project tests passed;
  `ruff check app tests`, `python -m compileall -q app tests`, and `git diff --check` passed. Only the
  existing Starlette, legacy Embedding configuration, and Python 3.12 SQLite warnings remain.

### T-0140 Deployment Readiness v0.1

Status: `pending`

Owner:
- Worker Session 17 / Deployment Worker; staged review and final acceptance by Manager Session.

Goal:
- Deliver the smallest Ubuntu 22.04 single-ECS, single-user closed-test deployment for the existing
  FastAPI service without changing `DialogueController`, Character, Memory, RAG, or active Evidence
  behavior.

Approved scope:
- API-only first release using Docker Compose, Caddy, one Uvicorn worker, `sqlite_raw`, HTTPS static
  Bearer auth, Context Budget enabled, empty CORS, and no Memory Worker or SiliconFlow mount.
- Add reproducible non-root image assets, production configuration and file-secret contracts,
  bounded readiness adapters, ingress controls, online SQLite backup/restore, log retention, and WSL2
  plus ECS staging gates.
- Keep `AEMEATH_ENABLE_LONG_TERM_MEMORY=false`. Enabling the Memory Worker requires separately
  approved SQLite dual-backend WAL/busy-timeout and fenced job lease/stale-recovery work.

Manager design acceptance:
- `docs/agents_communication/deployment_readiness_v0_1_design.md` was approved on 2026-07-29 after
  lifecycle, readiness, WAL-sidecar restore, secret-file, and Worker-gating corrections.
- Implementation must proceed by D1-D8 checkpoints. D5 remains outside the API-only critical path and
  cannot start until its two focused designs receive separate Manager approval.
- The design-stage `506 passed` result is only the prior regression baseline; implementation requires
  fresh full regression and environment smoke evidence before final acceptance.

Manager WSL review on 2026-07-30:
- Ubuntu 22.04 WSL2 and Docker Desktop Linux/amd64 became available and the first real D1 build was
  executed. The unchanged image failed because the Python 3.10 lock omitted the conditional
  `exceptiongroup` dependency.
- A temporary WSL-only probe then exposed use of `datetime.UTC`, which is unavailable on the declared
  Python 3.10 target. The probe passed all remaining preflight, read-only, mount, secret, health,
  image-content, auth, and SIGTERM checks after temporary diagnostic corrections.
- D1 therefore remains `Changes Requested`; details and required fixes are recorded in
  `docs/agents_communication/deployment_readiness_v0_1_wsl_manager_review.md`. T-0140 remains
  `pending`, and the diagnostic probe is not an accepted release image.

Manager D1 revalidation on 2026-07-30:
- The Worker added the pinned/hashed Python 3.10 `exceptiongroup` dependency and replaced the two
  Python 3.11-only `datetime.UTC` uses with `timezone.utc`.
- Manager rebuilt the official repository image with `--no-cache` for `linux/amd64`; the locked
  Python 3.10 install, production preflight, non-root/read-only runtime, writable state mounts,
  `0440` file-secret contract, health/readiness/auth smoke, content/secret scans, and graceful
  SIGTERM shutdown all passed.
- Fresh regression evidence: `584 passed, 1 skipped`; Ruff, compileall, lock validation, and
  `git diff --check` passed. No real Provider call or project `.env` access occurred.
- D1-D3 are accepted. T-0140 remains `pending`; D4 is authorized to begin, while D5 remains under
  its separately approved WAL/busy-timeout and fenced lease/stale-recovery gates.

Manager D4 review on 2026-07-30:
- The default API/Caddy topology, non-root/read-only/capability boundary, fixed network and ports,
  file-secret scope, body limit, disabled profiles, and restart/down-up persistence passed independent
  WSL Compose smoke. Fresh regression evidence was `592 passed, 1 skipped`; Ruff, compileall, lock
  validation, and `git diff --check` passed.
- D4 remains `Changes Requested`: Caddy advertises unreachable HTTP/3 `:8443` while Compose exposes
  TCP 443 only; proxy health remains healthy while API is stopped and HTTPS returns 502; the runbook
  installs a root-only env file but shows non-sudo Compose commands that fail with permission denied.
- Required narrow corrections and reproduction evidence are recorded in
  `docs/agents_communication/deployment_readiness_v0_1_d4_manager_review.md`. T-0140 remains
  `pending`; D5-D8 are not authorized by this review.

Manager D4 revalidation on 2026-07-31:
- The Worker restricted Caddy to h1/h2, extended proxy health to the local Caddy admin endpoint plus
  provider-free `api:8000/health`, and aligned all production Compose commands with the controlled
  root/sudo operator contract without weakening env or Secret permissions.
- Manager rebuilt both linux/amd64 images from the current worktree and independently reproduced:
  no HTTPS `Alt-Svc`; proxy unhealthy about 67.6 seconds after API stop with HTTPS 502; both services
  healthy about 16.7 seconds after API restart with HTTPS 200; root-only env denial for the ordinary
  user and successful root/sudo `compose config -q`.
- Fresh validation was `594 passed, 1 skipped` and D4 targeted `18 passed, 1 skipped`; Ruff,
  compileall, deploy lock validation, and `git diff --check` passed. The runbook now allows up to
  about 75 seconds for the proxy failure gate, accounting for interval, timeout, retries, and phase.
- D4 is `accepted`. T-0140 remains `pending`; D6 is authorized to begin under the approved staged
  design. D5 remains blocked on its separately approved WAL/busy-timeout and fenced
  lease/stale-recovery gates.
