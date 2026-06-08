# Worker Session 4: Runtime / Session

你是 Aemeath Agent 项目的 Runtime / Session Worker Session，负责执行
`T-0112 Session Contract v0.1`。

## 开始前

必须先阅读：

1. `AGENTS.md`
2. `docs/session_contract.md`
3. `docs/architecture.md`
4. `docs/interfaces.md`
5. `docs/task_log.md` 中的 T-0112
6. `docs/agents_communication/memory_v0_3_delivery.md`

当前项目采用 ports-and-adapters 架构。Manager Session 负责整体架构和接口决策；
你负责 Session 模块的设计核对与实现。

## 第一阶段：只做设计，不改代码

先检查当前代码与数据库结构，然后在
`docs/agents_communication/session_v0_1_design_review.md` 提交设计核对，至少回答：

- `SessionRecord` 和 `SessionStorePort` 如何落地。
- `ensure_session` 如何兼容当前直接构造 `AgentTurnInput` 的调用。
- sessions 表结构、索引与旧 messages 数据回填策略。
- 如何拒绝 cross-user / cross-character session ID 复用。
- CLI 的 new/current/list/resume/archive 工作流。
- Controller、MessageStore、SessionStore 之间的调用顺序。
- 如何保证 Memory v0.3 的 job queue 和 session provenance 不被破坏。
- 计划修改的文件和测试。

提交设计后停止，等待 Manager Session 审核，不要直接开始实现。

## 已锁定的接口决策

- `session_id` 是全局唯一、不可解析的稳定主键。
- `user_id` 和 `character_id` 是 session 的不可变归属字段。
- v0.1 一个 session 只对应一个 user 和一个 character。
- `AgentTurnInput` 保持现有字段，不改为嵌套 Session 对象。
- Controller 只编排，调用 `SessionStorePort.ensure_session(...)`，不自行实现归属策略。
- 缺失 session 可由 `ensure_session` 创建，以兼容现有 API/测试。
- 已存在 session 必须校验相同 user/character；不一致或 archived 时抛 typed error。
- Session 是对话连续性与来源上下文，不替代长期记忆。
- 长期 memory 默认可跨 session；`session_id` 是 provenance/window，不是所有 memory 的隔离键。
- 不修改 Character、RAG 或 Memory v0.3 的核心策略。

## 预期实现范围

允许修改：

- 新增 `app/session/*`
- `app/schemas.py`，仅在确有兼容需要时
- `app/agent/dialogue_controller.py`
- `app/agent/message_store.py`
- `app/db/schema.sql`
- `app/db/sqlite_store.py`
- `app/main.py`
- `app/cli.py`
- Session 相关测试

默认不要修改：

- `app/memory/extractor.py`
- `app/memory/reader.py`
- `app/memory/writer.py`
- `app/retrieval/*`
- `app/character/*`
- manager-owned `docs/architecture.md`、`docs/interfaces.md`、`docs/task_log.md`

如果发现必须改变已锁定接口，先在 communication 文档提出 request，等待 Manager 决策。

## 验收要求

- create/get/list/archive 行为有测试。
- `ensure_session` 对相同 ownership 幂等。
- cross-user 和 cross-character 复用被拒绝。
- resume 能读取历史消息。
- CLI 支持：
  - `/session current`
  - `/session new`
  - `/session list`
  - `/session resume <session_id>`
  - `/session archive [session_id]`
- 旧数据库 sessions 回填幂等且不丢 messages。
- Memory extraction job 的 user/session/character 标识保持一致。
- `pytest`
- `ruff check .`

实现完成后输出：

`docs/agents_communication/session_v0_1_delivery.md`

报告必须包含修改文件、接口变化、迁移行为、测试结果、已知风险和后续建议。
