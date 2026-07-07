# Aemeath Agent Codex 后续开发调度计划

最后更新：2026-07-06

本文档是 manager session 用来调度后续 Codex worker session 的中文路线图。它说明接下来应该按什么顺序开发、每一步适合开什么 worker session、应该读哪些文档、允许修改哪些范围，以及每个阶段的验收标准。

## 当前基线

当前已发布基线：

- v0.3 文本 Agent runtime。
- 以 CLI 为主要运行入口。
- 已有 Session contract 和运行时归属校验。
- 已有 Character v0.2 manifest。
- 已有 World Info RAG。
- 已有 recent conversation。
- 已有可选长期记忆 Memory v0.3，包括提炼 job 和 worker。
- 已有 prompt JSONL log。
- 本地 Audio / GPT-SoVITS 工作区已登记，但没有接入 runtime。

当前架构原则：

```text
CLI / 未来 API / 未来 Web
  -> Service layer
  -> DialogueController
       -> CharacterProviderPort
       -> MemoryReaderPort / MemoryWriterPort
       -> EvidenceProviderPort
       -> ContextBuilderPort
       -> ResponseGeneratorPort
       -> StateUpdaterPort
       -> MessageStore / SessionStorePort
       -> Observability ports
```

当前主要限制：

- 还没有 FastAPI 服务端。
- 还没有账号系统。
- 还没有统一生产级结构化日志。
- 还没有上下文压缩。
- SQLite migration 目前写在 `SQLiteStore` 初始化逻辑中，还没有独立迁移框架。
- Memory worker 已存在，但 worker 运维能力还比较基础。
- Audio/TTS 仍是本地 GPU-heavy 实验模块，应继续保持在主 runtime 之外。

## 协作规则

启动任何新 worker session 前：

1. 先查看 `git status --short --branch`。
2. 确认是否已有其他 session 正在修改同一批文件。
3. 阅读 `AGENTS.md`、`docs/architecture.md`、`docs/interfaces.md` 和本文档。
4. 如果任务涉及跨模块接口变更，先让 manager session 审批接口。
5. 每个 worker 完成后都应汇报：
   - 修改了哪些文件；
   - 新增或变更了哪些接口；
   - 运行了哪些测试；
   - 还有哪些风险或后续事项。

除非 manager 明确拆分 scope，不要让多个 worker 同时修改同一组核心文件。

## 阶段 0：收束当前进行中的工作

状态：已于 2026-07-06 完成。

### 目标

先完成并验收当前正在推进的 Error Handling 工作，再开始更大范围的服务端开发。

### 为什么优先

统一异常体系会被 API、Service Layer、结构化日志、Worker 管理和前端错误提示共同依赖。

### 推荐 Codex 调用

```text
Worker Session: Error Handling Review And Integration
```

### 输入文档与文件

- `docs/error_handling.md`
- `app/errors/`
- `tests/test_errors.py`
- 当前相关 diff：
  - `app/agent/dialogue_controller.py`
  - `app/cli.py`
  - `app/model/deepseek.py`
  - `app/observability/*`

### 范围

- 审查当前 Error Handling 实现。
- 如有必要，补齐测试。
- 确保 CLI 行为不被破坏。
- 确保 prompt log 和 trace payload 使用脱敏后的错误字段。
- 如果实现改变了公开契约，更新 `docs/architecture.md`、`docs/interfaces.md` 和 `docs/task_log.md`。

### 验收

- `pytest`
- `ruff check .`
- CLI 仍能稳定处理 `/session`、`/memory` 和普通对话失败。
- API key、authorization header、cookie、token、password、secret 不出现在公开错误响应或日志中。

### Manager Gate

已通过。Service Layer 与 FastAPI 的规划可以继续，但建议先排定 Observability 和
Context Compression 这两项上下文稳定性工作。

## 阶段 1：结构化日志与 Trace 持久化

### 目标

建立适合部署环境的结构化日志，同时保持 prompt log 作为独立调试产物。

### 推荐 Codex 调用

```text
Worker Session: Observability And Structured Logging
```

### 输入文档与文件

- `docs/error_handling.md`
- `app/observability/`
- `app/agent/dialogue_controller.py`
- `app/memory/worker.py`
- `scripts/inspect_prompt_log.py`

### 目标设计

将可观测性拆成三类：

```text
App Log      启动、配置、请求、CLI/API 边界、worker 生命周期
Trace Log    一轮 Agent 内部事件
Prompt Log   prompt/messages/sections/response 快照
```

### 建议文件

- `app/logging/`
- `app/observability/`
- `app/config.py`
- `docs/observability.md`
- `tests/*`

### 可考虑接口

- `AppLoggerPort`
- `JsonlTraceLogger`
- `TraceLogRecord`
- 复用 `app/errors` 中的 sanitizer

### 验收

- App log 以结构化 JSON 或结构化 dict 形式记录。
- 日志在可用时包含关联字段：
  - `request_id`
  - `turn_id`
  - `session_id`
  - `user_id`
  - `character_id`
  - `event_name`
  - `error_code`
- 敏感字段被脱敏。
- 现有 prompt log 功能仍可用。
- `pytest`
- `ruff check .`

### Manager 说明

这一阶段可以和 Context Compression 并行开发，只要文件范围不冲突。它最好在 FastAPI 前完成，这样 API request log 能复用同一套日志系统。

## 阶段 2：Application Service Layer

### 目标

在 CLI/API/Web 和 `DialogueController` 之间插入稳定的应用服务边界。

### 为什么在 FastAPI 前做

FastAPI 不应直接调用 `DialogueController`。Service Layer 负责统一处理：

- 请求校验；
- 用户与 session 归属检查；
- 错误映射；
- response DTO；
- 未来流式输出；
- 未来 Audio 输出；
- app-level logging。

### 推荐 Codex 调用

```text
Worker Session: Agent Service Layer
```

### 输入文档与文件

- `app/main.py`
- `app/cli.py`
- `app/agent/dialogue_controller.py`
- `app/schemas.py`
- `docs/session_contract.md`
- `docs/error_handling.md`

### 建议文件

- `app/services/__init__.py`
- `app/services/agent_service.py`
- `app/services/session_service.py`
- `app/services/schemas.py`
- `tests/*`

### 目标流

```text
CLI / future API
  -> AgentService.handle_chat(...)
  -> SessionService / permission checks
  -> DialogueController.handle_user_message(...)
  -> AgentService response DTO
```

### 验收

- CLI 可迁移到 `AgentService`，且用户可见行为不变。
- `DialogueController` 仍保持编排职责。
- 既有测试通过。
- 新测试覆盖 service 层中的成功对话、无效 session、ended session、archived session、未知角色 fallback。
- `pytest`
- `ruff check .`

### Manager Gate

此阶段完成前，不建议做 FastAPI、Web 或 Streaming API。

## 阶段 3：账号系统与 Owner-only 权限模型

### 目标

定义 `user_id` 从哪里来，并一致地执行 owner-only 访问控制。

### 推荐 Codex 调用

```text
Worker Session: Auth And Permission v0.1
```

### 输入文档与文件

- `docs/session_contract.md`
- `app/session/`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- 阶段 2 后的 `app/services/`

### 建议文件

- `app/auth/`
- `app/permissions/`
- `docs/auth_and_permissions.md`
- `tests/*`

### 第一阶段范围

不要一开始做完整 OAuth。先做最小本地账号模型：

```text
Account
  id
  username
  password_hash 或 dev-token identity
  status
  created_at
  last_login_at
```

推荐权限规则：

```text
owner-only
```

### 验收

- 调用方身份能解析为稳定 `user_id`。
- Session list/read/resume/end/archive 只允许 owner。
- Memory list/delete/disable/extract 只允许 owner。
- 测试覆盖跨用户拒绝，并且不会泄露资源是否存在或归属信息。
- 权限错误通过统一错误系统映射。
- `pytest`
- `ruff check .`

### 延后事项

- OAuth。
- refresh token。
- 管理员后台。
- 共享 session。
- 多用户房间。

## 阶段 4：上下文压缩

### 目标

增加 token-budget 友好的对话摘要能力，但不替代原始消息或长期记忆。

### 推荐 Codex 调用

```text
Worker Session: Context Compression
```

### 输入文档与文件

- `app/agent/context_builder.py`
- `app/agent/prompt_context.py`
- `app/agent/dialogue_controller.py`
- `app/model/client.py`
- `docs/interfaces.md`

### 建议文件

- `app/context_compression/`
- `docs/context_compression.md`
- `tests/*`

### 目标边界

```text
Recent messages
  -> CompressionPolicy
  -> SessionSummary / ContextSummary
  -> PromptContext section
```

### 规则

- Summary 是 prompt 支撑，不是长期记忆。
- Summary 不能删除原始消息。
- Summary 生成失败不能让对话失败。
- Summary 必须记录来源消息范围。
- `ContextBuilder` 通过稳定 schema 或 port 接收 summary 数据。

### 验收

- 长 recent history 可以被压缩成稳定 `PromptSection`。
- 原始 recent messages 仍可用。
- Summary 带有 source metadata。
- 测试覆盖无 summary、注入 summary、summary 失败 fallback。
- `pytest`
- `ruff check .`

### Manager 说明

这一阶段可以和结构化日志并行，但如果需要修改 `DialogueController`，必须先经过 manager 审批，避免和 Service Layer 冲突。

## 阶段 5：数据库迁移与备份策略

### 目标

从散落在 `SQLiteStore` 初始化中的迁移逻辑，逐步走向明确的迁移和备份纪律。

### 推荐 Codex 调用

```text
Worker Session: Database Migration Policy
```

### 输入文档与文件

- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `docs/session_contract.md`
- `docs/releases/v0.3.md`

### 建议文件

- `docs/database_migration_policy.md`
- 可选：`app/db/migrations/`
- `tests/*`

### 第一阶段范围

不要求立即替换所有 migration。先定义并记录：

- migration version table 归属；
- 启动迁移顺序；
- 事务迁移规则；
- 失败回滚预期；
- 升级前备份规则；
- SQLite 并发说明；
- 生产环境路径配置。

### 验收

- 现有 SQLite 测试通过。
- 迁移策略说明如何添加下一次 schema change。
- 如果引入 migration 文件，至少有测试覆盖幂等和失败回滚。
- `pytest`
- `ruff check .`

### 延后事项

- 完整 Alembic 风格框架。
- Postgres migration。
- 在线迁移。

## 阶段 6：Worker 运维与异步任务管理

### 目标

让 Memory worker 更适合部署环境。

### 推荐 Codex 调用

```text
Worker Session: Worker Operations
```

### 输入文档与文件

- `app/memory/worker.py`
- `app/memory/store.py`
- `app/db/sqlite_store.py`
- 阶段 1 的 logging，如果已完成。

### 建议改进

- 结构化 worker 日志；
- stale running job 回收；
- max attempts；
- 更清楚的失败原因；
- worker heartbeat 或轻量状态；
- job debug 命令或脚本；
- 部署说明。

### 验收

- 失败 job 可见，不会静默无限循环。
- stale job 可以安全重试或回收。
- worker 启动/关闭日志是结构化的。
- 既有 memory extraction 行为保持兼容。
- `pytest`
- `ruff check .`

### 延后事项

- Celery/RQ/Dramatiq。
- 分布式锁。
- 外部队列服务。

## 阶段 7：配置与密钥管理

### 目标

为服务端部署准备配置系统，避免泄露密钥。

### 推荐 Codex 调用

```text
Worker Session: Configuration And Secrets
```

### 输入文档与文件

- `app/config.py`
- `.env.example`
- `docs/error_handling.md`
- 阶段 1 logging

### 建议文件

- `docs/configuration.md`
- `tests/*`

### 范围

- 增加 `AEMEATH_ENVIRONMENT`。
- 定义 dev/test/prod 行为。
- 启动时校验必要密钥。
- 文档化所有环境变量。
- 区分本地 `.env` 和生产环境变量。
- 配置错误通过统一错误系统映射。

### 验收

- 真实模型模式缺少必要密钥时产生稳定 config error。
- `.env.example` 只包含安全占位符。
- 测试覆盖 bool/int/float 配置解析和缺失密钥行为。
- `pytest`
- `ruff check .`

## 阶段 8：FastAPI 服务端 API

### 目标

在 Service Layer、Auth、Error Mapping、Logging、Config 准备好之后，添加第一版 HTTP API。

### 推荐 Codex 调用

```text
Worker Session: FastAPI Server v0.1
```

### 输入文档与文件

- `app/services/`
- `app/auth/`
- `app/errors/`
- `app/logging/`
- `docs/interfaces.md`

### 建议文件

- `app/api/server.py`
- `app/api/schemas.py`
- `app/api/dependencies.py`
- `app/api/routes/chat.py`
- `app/api/routes/sessions.py`
- `app/api/routes/memory.py`
- `app/api/routes/health.py`
- `tests/*`

### 第一批 API

```text
GET  /health
POST /sessions
GET  /sessions
POST /sessions/{session_id}/resume
POST /sessions/{session_id}/end
POST /sessions/{session_id}/archive
POST /chat
```

### 验收

- API 使用 service layer，不直接调用 `DialogueController`。
- API response schema 稳定。
- API 错误使用统一错误 payload。
- 用户身份通过选定 auth 方式解析。
- 测试覆盖 happy path 和权限拒绝。
- `pytest`
- `ruff check .`

### 延后事项

- streaming。
- WebSocket。
- Audio。
- admin endpoints。

## 阶段 9：Streaming Chat API

### 目标

在不破坏当前同步 turn path 的前提下，支持未来真实流式输出。

### 推荐 Codex 调用

```text
Worker Session: Streaming Chat Design Spike
```

### 输入文档与文件

- `app/model/client.py`
- `app/model/deepseek.py`
- `app/services/agent_service.py`
- `app/api/`

### 第一阶段输出

优先做设计文档和小型接口 spike，不急着完整实现。

可考虑接口：

```text
StreamingChatModelClient.stream_chat(...)
AgentStreamService.stream_user_message(...)
```

### 重要规则

保持当前 `handle_user_message(...)` 稳定，在旁边新增 streaming path。

### 验收

- 设计说明 stream 完成后如何保存完整 assistant message。
- 设计说明 failed/incomplete stream 如何写 prompt log 和 trace。
- 设计说明取消生成和客户端断连行为。

## 阶段 10：最小 Web 前端

### 目标

基于 API 构建一个小型 Web UI。

### 推荐 Codex 调用

```text
Worker Session: Web MVP
```

### 输入文档

- 阶段 8 API。
- `docs/interfaces.md`。

### MVP 范围

- 登录或开发身份选择；
- session 列表；
- create/resume/end session；
- chat 窗口；
- warning/error 展示；
- 可选 memory debug panel。

### 验收

- Web 只调用 API。
- 不直接访问 SQLite 或 Controller。
- 基础移动端和桌面布局可用。
- API 错误信息被安全展示。
- 记录手动 smoke test。

### 延后事项

- prompt log viewer。
- admin memory panel。
- audio playback。
- streaming UX。

## 阶段 11：测试体系与 CI

### 目标

让回归检查在部署前更可靠。

### 推荐 Codex 调用

```text
Worker Session: Test And CI Hardening
```

### 输入

- 现有全部测试；
- 未来 API tests；
- 未来 service tests。

### 范围

- 整理 unit/integration/API tests。
- 增加 smoke test scripts。
- 增加 GitHub Actions 或本地 CI 文档。
- 确保测试不依赖真实 API key。
- 确保测试不会写入真实 `data/aemeath.db`。

### 验收

- `pytest`
- `ruff check .`
- `python -m compileall -q app`
- CI 或等价文档能在干净环境跑完所有检查。

## 阶段 12：部署

### 目标

让文本 Agent 能作为服务部署。

### 推荐 Codex 调用

```text
Worker Session: Deployment v0.1
```

### 输入

- 阶段 8 API。
- 阶段 6 worker operations。
- 阶段 7 config。

### 建议文件

- `Dockerfile`
- `docker-compose.yml`
- `docs/deployment.md`
- `scripts/backup_sqlite.ps1` 或文档化备份命令

### 最小部署形态

```text
web api process
memory worker process
sqlite data volume
prompt/trace/app log volume
environment variables / secrets
```

### 验收

- Text-only deployment 不需要 GPU。
- Memory worker 可以作为独立进程启用。
- Health check 可用。
- 数据和日志挂载在镜像外。
- 有备份/恢复说明。
- 真实密钥不写入镜像或文档。

## 阶段 13：Memory 质量提升

### 目标

在服务端基础设施稳定后，再提升 Memory 质量。

### 推荐 Codex 调用

```text
Worker Session: Memory Quality v0.4
```

### 候选范围

- `user_global` / `character` / `session` scope classifier；
- 记忆合并和冲突处理；
- 记忆衰减；
- 更好的提炼 prompt；
- 更好的召回评分；
- recall precision eval samples。

### 依赖

建议在 logging、service layer、permission、worker operations 稳定后再做。Memory 质量工作非常依赖 trace 和 worker 状态，否则调试成本会很高。

## 阶段 14：Audio / TTS 接入决策

### 目标

决定是否以及如何接入本地 GPT-SoVITS 或远程 TTS 服务。

### 推荐 Codex 调用

```text
Worker Session: Audio Adapter Design
```

### 输入

- `docs/audio_module.md`
- `docs/interfaces.md`
- 未来 API/Web 设计

### 规则

Audio 是可选的 post-response 能力：

```text
AgentTurnOutput.response
  -> optional TextToSpeechPort
  -> audio metadata / file / URL / stream
```

### 验收

- 文本 Agent 仍可在无 GPU 环境部署。
- TTS 失败不会导致文本对话失败。
- GPT-SoVITS 细节留在 adapter 或 sidecar service 内。
- 接口支持 local、remote 和 disabled provider。

## 建议并行方式

相对安全的并行轨道：

```text
Track A: Error Handling -> Service Layer -> FastAPI
Track B: Structured Logging -> Worker Operations
Track C: Context Compression
Track D: DB Migration Policy -> Deployment
Track E: Auth/Permission，在 Service Layer 设计稳定后启动
```

避免并行修改：

- `app/agent/dialogue_controller.py`
- `app/main.py`
- `app/db/sqlite_store.py`
- `app/schemas.py`
- `docs/interfaces.md`

这些文件是集成热点，worker 修改后应由 manager 先审查，再让下一个 worker 接力。

## 推荐立即执行顺序

除非优先级变化，否则建议按以下顺序调用 Codex：

1. 启动 Observability / Structured Logging worker。
2. 如果 Context Compression 能先提交设计并避免直接修改 `DialogueController`，可以并行启动。
3. 启动 Agent Service Layer worker。
4. 启动 Auth + Permission worker。
5. 启动 DB Migration Policy worker。
6. 启动 Worker Operations worker。
7. 启动 FastAPI Server worker。
8. 启动 Test/CI hardening worker。
9. 启动 Deployment worker。

## 通用 Worker Prompt 模板

```text
Worker Session: <Name>

You are working on Aemeath Agent. Read AGENTS.md first, then read:

- docs/architecture.md
- docs/interfaces.md
- docs/task_log.md
- docs/codex_development_sequence_plan_cn.md
- <task-specific docs>

Do not start implementation immediately. First summarize:

1. your understanding of the current architecture;
2. the exact scope you propose;
3. files you expect to modify;
4. interfaces you need to add or change;
5. tests you plan to run;
6. risks or manager decisions needed.

After manager approval, implement within the approved scope. Do not modify unrelated modules or
revert other sessions' changes.
```

## Manager Review Checklist

每个 worker 交付后，manager 应检查：

- 是否保持 `DialogueController` 作为编排层？
- 新能力是否位于 port 或 service 后面？
- 错误是否通过统一错误系统映射？
- 日志是否结构化并脱敏？
- 是否遵守 user/session/memory ownership 规则？
- 测试是否聚焦且足够？
- 如果接口或架构发生变化，是否更新文档？
- 是否避免提交本地 runtime data、`.env`、数据库文件、prompt logs、source docs 和 Audio 工作区产物？
