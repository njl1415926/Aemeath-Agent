# Aemeath Agent Architecture

## 2026-06-08 v0.3 Release Baseline

v0.3 keeps `DialogueController` as an orchestration layer and promotes Session and
long-term Memory to explicit runtime systems:

```text
CLI / FastAPI
  -> SessionStorePort
  -> DialogueController
       -> CharacterProviderPort
       -> SemanticQueryCoordinatorPort
       -> MemoryReaderPort / MemoryWriterPort
       -> EvidenceProviderPort
       -> ContextBuilderPort
       -> ResponseGeneratorPort
       -> StateUpdaterPort
       -> MessageStore
```

Session provides stable ownership, lifecycle, and provenance. Memory independently decides
visibility through `user_global`, `character`, and `session` scopes. Extraction jobs and
embeddings are implementation details behind Memory ports; RAG remains behind
`EvidenceProviderPort`. This keeps later API, tool, MCP, or LangGraph adapters from becoming
business-policy owners.

2026-07 current-state note: normal CLI turns and FastAPI chat now enter through `AgentService`
before reaching `DialogueController`. FastAPI also adds auth, request-id, API error mapping, and
app-log behavior at the HTTP adapter boundary.

## 2026-07-26 Embedding Foundation And Artifact Persistence

Embedding is now a shared infrastructure capability rather than a Retrieval-owned implementation:

- `app/embeddings/` owns the batch-first `EmbeddingProviderPort`, versioned
  `EmbeddingSpaceProfile`, exact `EmbeddingModelSignature`, provider factory, qualification probes,
  typed errors, and embedding observability.
- `siliconflow_pro_bge_m3_v1` is approved for normal API-backed Memory/Semantic runtime;
  `deterministic_hash_v1` remains approved for deterministic tests and explicit development only.
  Local BGE-M3 remains behind the qualification and approval gate. No provider failure may trigger
  an implicit Hash fallback.
- Memory and RAG persistence can store multiple canonical embedding artifacts for one target. The
  artifact identity is `target id + signature fingerprint + purpose`; full canonical signature,
  content hash, vector format, dimension, vector digest, and current target content are validated
  before scoring.
- Raw SQLite and SQLAlchemy Memory stores expose parallel canonical artifact APIs while retaining
  legacy methods for one compatibility release. RAG follows the same split through
  `SqliteVectorStore`.
- Legacy Hash rows are promoted only after byte-for-byte verification. BGE, unknown, stale, corrupt,
  or mismatched legacy rows remain isolated and are reported as `legacy_unverified`.

T-0133 now uses this foundation end to end in the enabled Memory workflow. `MemoryReader` obtains a
provider-neutral resolved query embedding and searches only the exact canonical signature. It uses
the eligible Memory count to detect incomplete coverage, keeps vector hits first, and fills remaining
slots through scope-consistent keyword recall. Query failures and no-hit cases degrade to keyword
recall; explicit Chinese recall also retains the active-memory fallback.

`MemoryExtractionService` commits Memory facts through atomic exact-active create-or-get before one
batch of `document` embeddings. Artifact failure leaves facts committed and derives pending work from
exact-signature gaps. The single Memory Worker repairs bounded gap pages on an independent interval
with failure backoff. Other signatures remain valid coexisting spaces. The active dialogue RAG
provider remains `WorldInfoEvidenceProvider`, so this Memory integration does not change the RAG
runtime path.

As of T-0138, the local composition root can select the approved SiliconFlow Profile through
`AEMEATH_EMBEDDING_PROFILE=siliconflow_pro_bge_m3_v1`. Memory extraction, repair, recall, and the
turn-level Semantic Query coordinator share one synchronized 1024-dimensional Provider instance.
Existing Hash artifacts remain valid side-by-side derived data, but exact-signature queries never
mix them with SiliconFlow artifacts. Managed vector RAG remains a standalone tool and World Info
remains the active dialogue evidence provider.

## 2026-07-28 Shared Turn Semantic Query

T-0135 adds a non-persistent semantic-query coordination layer between recent-message loading and
the independently owned Memory / Evidence consumers:

```text
DialogueController
  -> save current user message
  -> load recent user/assistant messages, excluding the current message by id
  -> SemanticQueryCoordinatorPort.prepare_turn_context(...)
       -> deterministic query construction and budget
       -> one batch-first purpose=query embedding call for active roles
       -> immutable TurnSemanticContext
  -> MemoryReaderPort.retrieve_query(..., semantic_context=...)
  -> EvidenceProviderPort.build_evidence_pack(..., semantic_context=...)
```

`DialogueController` only adapts inputs, invokes the coordinator once, and passes the same context
instance onward. Query rewriting, dependency-message selection, embedding validation, and consumer
fallback remain inside the semantic and consumer modules. Query text and vectors are turn-local and
must not be persisted in `PromptContext.metadata`, trace payloads, prompt-run metadata, or app logs.

The enabled long-term-memory runtime shares one
`SynchronizedEmbeddingProvider(ObservableEmbeddingProvider(concrete_provider))` across the semantic
coordinator, Memory reader compatibility source, and Memory extraction. The synchronized wrapper
serializes the complete provider call without changing provider batches, retries, results, errors, or
terminal embedding events. Local BGE query input is tokenized without truncation and fails loudly on
token overflow; document embedding keeps its existing truncating behavior.

The current composition root activates only the `memory_recall` semantic role. World Info receives
the optional context but intentionally ignores it, Character example selection is unchanged, and
managed canonical RAG remains tooling-only until a separately approved runtime integration task.

## 2026-07-28 Deterministic Prompt Context Budget

T-0136 adds an optional `ContextCompressorPort` after complete `PromptContext` assembly and before
`PromptCompiler`. It does not change Semantic Query, Memory retrieval, Evidence retrieval, or their
upstream ordering:

```text
ContextBuilder
  -> assemble complete PromptContext
  -> ContextCompressorPort.compress(...)
       -> protected sections remain intact
       -> deterministic section limits and character-budget trimming
       -> safe kept/dropped ids and counts in PromptContext.metadata.context_budget
  -> PromptCompiler
```

The default remains disabled. When enabled, v0.1 uses a deterministic compiled-message character
estimate with defaults of 24000 chars, 8 recent messages, 4 Memories, 4 Canon results, and 4 Voice
results. Global trimming order is Style, Voice, Canon, Memory, then Recent. Character identity and
rules, safety boundaries, relationship state, unknown sections, and the current user input are
protected. If protected content alone exceeds the target, the prompt remains over budget and reports
`protected_floor_exceeded`; v0.1 never truncates protected text or individual lines.

The budget report contains section/source ids and counts but no removed body text. Existing
`memory_ids`, `evidence_ids`, `used_memory_ids`, and `used_memories` retain pre-budget retrieval
semantics; final prompt membership is represented by `context_budget.kept_source_ids` and
`dropped_source_ids`.

## 2026-06-08 Manager Update: Session Contract v0.1

Session is promoted from a transient string to a first-class runtime object. The stable identity
is a globally unique `session_id`; `user_id` and `character_id` define ownership and filtering.
A v0.1 session belongs to exactly one user and one character.

The planned runtime boundary is:

```text
CLI / API
  -> SessionStorePort.create/get/list/end/resume/archive
  -> AgentTurnInput(session_id, user_id, character_id)
  -> DialogueController
  -> SessionStorePort.ensure_session
  -> MessageStore / Memory / RAG / Model
```

Session owns dialogue continuity and provenance. It does not replace memory. Long-term memory may
use `session_id` as source provenance while retaining user-global or character-scoped retrieval.
Session lifecycle is `active -> ended -> active` or `active/ended -> archived`. `ended` requires
an explicit resume; archived sessions cannot be resumed in v0.1. CLI selection is not lifecycle:
creating or selecting another session does not implicitly end the previous one.

`message_count` and `last_message_at` belong to the Session read model. The v0.1 SQLite adapter
computes them from `messages`, avoiding a hidden transaction dependency between `MessageStore`
and `SessionStorePort`. A future storage adapter may materialize the projection without changing
the public contract.
The decision-complete contract is maintained in `docs/session_contract.md`.

## 2026-06-04 Manager Update: Prompt Assembly And JSONL Logs

Prompt assembly now keeps a stable section order:

```text
base_instructions
character_context / persona / speech / style / boundaries
canon_evidence
voice_evidence
interaction_memories
relationship_state
recent_conversation
current user message
```

`DialogueController` records prompt-run snapshots through `PromptRunLoggerPort`.
The default runtime uses `JsonlPromptRunLogger` when
`AEMEATH_ENABLE_PROMPT_RUN_LOG=true`, writing local JSONL files under
`.aemeath/sessions/YYYY/MM/DD/rollout-*.jsonl`. This is an observability layer feature:
it records prompt/response debugging data and does not replace `MessageStore`, memory, trace,
or RAG evidence.

## 2026-06-03 Manager Update: Module-Autonomous Decisions

当前运行时不再使用全局 `ResponseModeRouter`。Controller 的职责是稳定编排：

```text
DialogueController
  -> CharacterProviderPort.build_runtime_context(...)
  -> MemoryReaderPort.retrieve_query(...)
  -> EvidenceProviderPort.build_evidence_pack(...)
  -> ContextBuilderPort.build_context(...)
  -> ResponseGeneratorPort.generate(...)
```

是否读取记忆、是否检索剧情/语音证据、是否注入更多角色样例，都由对应模块内部决定。
不需要工作时，模块返回空结果，例如 `[]` 或 `EvidencePack()`。这样可以避免一个中心 mode
分类器膨胀，也方便后续接 memory embedding、RAG、tool calling、MCP 和 LangGraph。

旧的 `response_mode` 仅保留在少量兼容参数和 Character manifest legacy 字段中，不再是架构边界。

本文档描述 Aemeath Agent 当前架构、模块边界和后续扩展方向。它是 manager session 分配任务和审查实现的主要依据。

## 当前运行基线

v0.3 及其当前开发 checkpoint 已跑通一个最小但可扩展的角色对话闭环：

1. CLI 接收用户输入。
2. Dialogue Controller 编排一次 turn。
3. Character Provider 提供爱弥斯角色上下文。
4. Message Store 读取短期上下文并写入对话日志。
5. Semantic Query Coordinator 为已激活消费者构造并共享本轮查询向量。
6. Memory Reader 检索长期记忆。
7. Evidence Provider 检索剧情 / 世界信息证据。
8. Context Builder 组装 prompt。
9. Model Client 生成回复。
10. State Updater 更新关系 / 情绪状态。
11. Memory Writer 按配置提交长期记忆提炼任务。
12. Session Store 验证会话归属、状态并提供消息统计。

## 高层数据流

```text
CLI / FastAPI / future Web
  |
  v
AgentService
  |
  v
AgentTurnInput
  |
  v
DialogueController
  |-- CharacterProvider
  |-- MessageStore
  |-- RelationshipStore
  |-- SemanticQueryCoordinator
  |-- MemoryReader
  |-- EvidenceProvider
  |-- ContextBuilder -> PromptContext -> PromptCompiler
  |-- ResponseGenerator -> ChatModelClient
  |-- StateUpdater
  |-- MemoryWriteClassifier -> MemoryWriter
  |-- TraceLogger
  |-- PromptRunLogger
  |
  v
AgentTurnOutput
```

## 分层设计

### Application Service Layer

位置：

- `app/services/schemas.py`
- `app/services/agent_service.py`
- `app/main.py`

职责：

- 在 CLI / FastAPI / future Web 和 `DialogueController` 之间提供稳定应用边界。
- 将 `UserContext` / `ChatRequest` 转换为内部 `AgentTurnInput`。
- 将 `AgentTurnOutput` 转换为 adapter 友好的 `ChatResponse`。
- 成功时返回 DTO；失败时继续抛出原异常，由 adapter 调用 `map_exception(...)` 映射。
- 可选记录 app-level summary log，但不记录 prompt、model messages、用户消息全文或 provider 原始响应。

当前核心入口：

- `build_agent_service()`
- `AgentService.handle_chat()`

### Auth / Account / Permission Layer

位置：

- `app/accounts/`
- `app/auth/`
- `app/permissions/`
- `docs/auth_and_permissions.md`

职责：

- 提供本地 `accounts` 模型和默认账号 bootstrap。
- 可选解析 dev/static bearer token，并输出 `UserContext`。
- 提供最小 owner-only permission helper。
- 保持账号状态、token、cookie、authorization 等 Auth 细节在 CLI / FastAPI adapter
  边界，不进入 `DialogueController`。

当前边界：

```text
CLI / FastAPI adapter
  -> AuthService / LocalDefaultAccountResolver
  -> UserContext
  -> AgentService
  -> DialogueController
```

`AgentService` v0.1 信任传入的 `UserContext`，不解析 token，不处理 cookie，不执行登录。
账号状态校验由 `AuthService` / `AccountService` 完成。Session 归属错误继续使用
`SESSION_OWNERSHIP_DENIED`；非 session 的通用 owner-only 拒绝使用 `PERMISSION_DENIED`。

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
- `app/character/Aemeath.v0.2/`
- `app/character/Aemeath.v0.1/character.yaml`

职责：

- 加载角色卡。
- 输出 `CharacterProfile` 和 `CharacterRuntimeContext`。
- 屏蔽角色资料来源细节。
- 通过 `CharacterRegistry` 按 `character_id` 选择角色 provider。

当前默认注册 `app/character/Aemeath.v0.2/` 多文件 manifest。`app/character/Aemeath.v0.1/character.yaml` 是 fallback；root-level `app/character/aemeath.v0.1.yaml` 已废弃但仍可显式加载兼容。后续可升级为多角色 manifest + 多文档加载 + 角色知识索引。

### Context Layer

位置：

- `app/agent/context_builder.py`
- `app/agent/context_budget.py`
- `app/agent/prompt_context.py`
- `app/prompts/dialogue_prompt.v0.1.md`
- `app/retrieval/schemas.py`
- `app/retrieval/ports.py`
- `app/retrieval/provider.py`

职责：

- 合并角色上下文、短期对话、长期记忆、关系状态和剧情证据。
- 按可选确定性预算裁剪已组装的 `PromptContext`。
- 输出模型可消费的 messages。

`EvidencePack` 是后续剧情知识、语音风格证据和 RAG 结果进入 prompt 的预留接口。

`EvidenceProviderPort` 是 RAG / lore / voice evidence 的统一入口。v0.1 默认使用 `EmptyEvidenceProvider`，保持现有行为不变；后续可以替换为 `CompositeEvidenceProvider` 或 MCP adapter。

`PromptContext` 是 prompt-time 的一等对象。`ContextBuilder` 先输出结构化 `PromptContext`，再由 `PromptCompiler` 编译为 model messages。这样后续 trace、eval、token budget、critic 都可以引用稳定 section。

### Retrieval Layer

Current active provider: `WorldInfoEvidenceProvider`.

`EmptyEvidenceProvider` remains available as a no-evidence implementation for tests or explicitly
disabled RAG runs, but it is no longer the default provider in `app/main.py`.

2026-07 current-state note: the main runtime wires `WorldInfoEvidenceProvider` through
`EvidenceProviderPort`. Older references to `EmptyEvidenceProvider` as the default describe the
pre-World-Info runtime and should not guide new worker implementation.

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

2026-07 current-state note: CLI runtime keeps graceful model fallback enabled by default.
FastAPI runtime disables model fallback by default through
`AEMEATH_API_ENABLE_MODEL_FALLBACK=false`, so provider failures surface as stable API errors.

### Error Handling Layer

Position:

- `app/errors/`
- `docs/error_handling.md`

Responsibilities:

- Provide a stable error contract shared by CLI, FastAPI adapters, trace records, and prompt
  run logs.
- Map runtime exceptions into `ErrorInfo` with stable `ErrorCode`, public message, retryability,
  details, and internal message fields.
- Keep public error output separate from internal diagnostics.
- Provide HTTP status mapping for future FastAPI work without coupling the current runtime to
  FastAPI.
- Redact sensitive values before errors are written to logs, traces, prompt-run records, or user
  output.

Current implementation:

- `ErrorCode` defines stable categories for config, session, character, memory, model, storage,
  validation, and internal errors.
- `AemeathError` can carry a stable code and public message from business modules.
- `map_exception(exc)` converts known exceptions into `ErrorInfo`.
- `http_status_for_error(error_info)` is used by FastAPI and remains available for future HTTP adapters.
- `redact_sensitive(...)` redacts common secret fields, including `api_key`, `apikey`, `api-key`,
  `x-api-key`, `x_api_key`, `authorization`, `cookie`, `password`, `secret`, and `token`, while
  avoiding broad false positives such as ordinary `ordinary-key` fields.

Boundary:

- `DialogueController` still only orchestrates. It records structured failure metadata and
  re-raises the original exception.
- CLI and FastAPI/service adapters decide how to present mapped public errors.
- Future service and FastAPI layers should use `map_exception()` and `ErrorInfo.to_public_dict()`;
  they should not return raw `str(exc)` to users.

### Audio / TTS Experimental Workspace

位置：

- `Aemeath_roleplay_skill/audio/`
- `docs/audio_module.md`

职责：

- 保存当前本地 GPT-SoVITS v2ProPlus 训练、标注和推理实验。
- 作为后续 Aemeath 语音输出能力的候选适配层。
- 暂不参与主对话 runtime，不进入 `DialogueController`、Prompt、RAG、Memory 或 Session。

当前状态：

- 已有本地 GPT-SoVITS 小模型和常驻 TTS console。
- 该工作区依赖本地 Windows GPT-SoVITS runtime、训练素材、模型权重和 GPU 推理资源。
- 因服务器 GPU 成本较高，现阶段仅登记为独立实验模块，不接入 CLI / API / Web 流程。
- 整个 `Aemeath_roleplay_skill/audio/` 当前视为本地工作区，通过 `.gitignore` 排除，
  不进入普通源码仓库。

未来接入原则：

- 文本回复应先正常生成并返回，Audio/TTS 作为回复后的可选输出能力。
- TTS 失败不得影响文本回复。
- 核心业务层不直接依赖 GPT-SoVITS、音频播放库或云端 TTS SDK。
- 正式接入前应先定义 `TextToSpeechPort` / `AudioOutputPort`、配置结构和 trace 落点。
- 如果部署到服务器，建议优先评估独立 TTS 服务进程或远程 TTS 服务，而不是让 Web API
  进程直接持有 GPU 模型。

### Memory Layer

位置：

- `app/memory/store.py`
- `app/memory/reader.py`
- `app/memory/writer.py`
- `app/memory/classifier.py`
- `app/memory/admin.py`
- `app/memory/disabled.py`
- `app/memory/extractor.py`
- `app/memory/worker.py`

职责：

- 管理长期用户记忆。
- 判断是否写入记忆。
- 检索相关记忆。
- 提供 CLI 管理命令。

当前实现是关键词检索 + 规则式写入判断。后续可以升级为 embedding 检索、LLM 记忆抽取、记忆合并、记忆衰减。

v0.2 minimal runtime 默认使用 recent conversation 作为 memory baseline，并通过
`AEMEATH_ENABLE_LONG_TERM_MEMORY=false` 关闭长期记忆读写。关闭时 `app/main.py`
注入 disabled memory reader / writer / classifier，`DialogueController` 仍固定调用
`MemoryReaderPort` / `MemoryWriterPort` / `MemoryWriteClassifierPort`，不承担长期记忆策略判断。
`MemoryAdminService` 仍保留对已有长期记忆的管理能力。

v0.3 long-term memory runtime 在显式开启 `AEMEATH_ENABLE_LONG_TERM_MEMORY=true` 后启用：

- `MemoryWriteClassifier` 识别显式记忆触发词。
- `QueuedMemoryWriter` 将 hard trigger 和 12-message window 写入
  `memory_extraction_jobs`，使用 `idempotency_key` 防止重复 job。
- 独立进程 `python -m app.memory.worker` 领取 pending job，调用
  `MemoryExtractionService` 使用 DeepSeek / mock extraction client 提炼 JSON memories。
- 提炼出的 Memory 事实先原子写入 `long_term_memories`，再批量写入 exact-signature
  `long_term_memory_embedding_artifacts`；artifact 失败不回滚事实，由 repair 补齐。
- `MemoryReader` 优先使用同一 turn 的 `TurnSemanticContext` 检索 exact-signature canonical
  artifact；未传共享 context 的独立管理/兼容调用仍可使用 provider-neutral direct query source。
  coverage 不完整时使用关键词补位，结果进入
  `ContextBuilder.long_term_memories` 和 prompt run log 的 `memory_ids`。
- 独立 Worker 按 bounded batch、独立 interval 和失败退避修复当前 signature 的 artifact
  缺口；其他 signature 合法共存，当前版本不提供 extraction job 自动重试。
- 长期记忆的作用域与来源分离：
  - `scope + scope_character_id + scope_session_id` 决定召回可见性；
  - `source_session_id + source_job_id + source_message_ids_json` 只记录来源。
- 关键词、向量和 fallback 统一使用 Memory Store 内部的召回 scope predicate。
- 精确去重按目标 scope tuple 匹配，不由当前对话可见的其他 scope memory 抑制写入。
- 旧长期记忆通过版本化事务迁移为 `user_global`，保持原有跨 Session 行为。
- CLI 支持 `/memory extract [limit]` 同步提炼当前 session 最近消息，支持
  `/memory debug on|off` 显示本轮召回内容。

运行时通过 `AEMEATH_EMBEDDING_PROFILE` 选择 approved profile，并统一经过 Embedding
Factory。当前仅 `deterministic_hash_v1` 获批，且必须显式开启 Hash；BGE-M3 和 SiliconFlow
profile 在批准前会 fail-fast。旧 `AEMEATH_MEMORY_EMBEDDING_PROVIDER` 仅保留一个兼容版本。

当前边界约定：

- Interaction memory 走 `MemoryReaderPort`。
- Canon / lore / voice evidence 走 `EvidenceProviderPort`。
- `MemoryQuery` / `MemorySearchResult` 是后续 filter、评分、解释和 trace 的稳定输入输出对象。
- Memory Admin 在读取 Session 消息前通过 `SessionStorePort.require_session(...)` 校验
  `session_id + user_id + character_id`；删除和禁用 memory 使用 owner-scoped 原子更新。

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

2026-07-06 update: observability is split into three separate artifacts:

```text
App Log      process/runtime/application boundary events
Trace Log    one agent turn's internal structured events
Prompt Log   prompt/messages/sections/response snapshot for prompt debugging
```

Current JSONL trace persistence is implemented by `JsonlTraceLogger`, wired by `app/main.py`
when `AEMEATH_ENABLE_TRACE_LOG=true`. The default root is `.aemeath/traces`, and records are
UTF-8 JSONL with `ensure_ascii=False`.

The minimal app logging contract is `AppLogRecord`, `AppLoggerPort`, `JsonlAppLogger`, and
`NoopAppLogger`. App logging belongs at CLI/API/service/worker boundaries, not inside
`DialogueController`.

Prompt-run logging remains separate through `PromptRunLoggerPort` and keeps its existing local
debugging purpose under `.aemeath/sessions`. Trace and app logs should default to ids, counts,
statuses, durations, error codes, and sanitized metadata rather than full prompt text.

### Error Handling Observability Notes

Prompt-run logging now preserves failed-turn error fields:

- `error_code`
- `error_type`
- `error_public_message`
- sanitized `error`

The prompt log inspector displays these fields when present and falls back to unknown/none values
for older logs. Structured logging should reuse the same `app/errors` sanitizer and error mapping
instead of introducing a separate error taxonomy.

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
  creates CharacterRegistry
  creates WorldInfoEvidenceProvider
  creates NoopTraceLogger
  creates NoopAppLogger / JsonlAppLogger for AgentService
  creates one synchronized Embedding Provider when long-term Memory is enabled
  creates SemanticQueryCoordinator / DisabledSemanticQueryCoordinator
  creates MemoryReader/Writer
  creates ResponseGenerator(ChatModelClient)
  injects all dependencies into DialogueController
  creates AgentService as the app boundary

AgentService
  accepts UserContext + ChatRequest
  returns ChatResponse or re-raises original exceptions

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

   - module decision reasons
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

### v0.4 推荐主线

1. AgentRuntimeState
2. Controller step 化
3. LangGraph Runtime Spike
4. Tool calling loop
5. Checkpoint / resume / human-in-the-loop

LangGraph 的定位是可选 runtime 编排层。它可以替换当前手写 `DialogueController` 的流程编排，但不应替代 memory、character、retrieval、model、state 等业务 service。迁移顺序应是先稳定 ports，再抽 `AgentRuntimeState`，再把现有步骤 node 化，最后再引入 LangGraph。

## 已知技术债

1. `MemoryWriteClassifier`、`StateUpdateClassifier` 和部分模块内部 policy 当前包含规则式中文关键词，且部分源码文本在 Windows 控制台显示为乱码。后续应统一检查源文件编码并增加行为测试。
2. `ContextBuilder` 已接入默认关闭的字符预算和 prompt section 裁剪；精确 tokenizer 预算、
   摘要压缩和基于质量评估的默认启用仍未实现。
3. `FallbackChatModelClient` 的 CLI/API 默认策略已经分离；后续仍可进一步完善降级响应的结构化观测。
4. `CharacterProvider` 当前默认使用 `app/character/Aemeath.v0.2/` manifest，v0.1 YAML 仅作为兼容 fallback；后续仍需完善多角色与角色文档治理。
5. Memory update / merge / conflict / decay 仍未真正实现，只保留了决策字段和 backlog。
## 2026-06-04 Retrieval Runtime Update

- The active main-runtime `EvidenceProviderPort` implementation is now
  `WorldInfoEvidenceProvider`.
- `DialogueController` still depends only on `EvidenceProviderPort` and does not know about
  World Info JSON paths, keyword matching, constants, or retriever internals.
- `WorldInfoEvidenceProvider` returns World Info matches through `EvidencePack.lore_results`.
- Embedding/vector RAG remains available as retrieval tooling, but it is not the active provider
  wired by `app/main.py`.
