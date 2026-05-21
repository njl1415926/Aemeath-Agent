# Aemeath Agent Collaboration Guide

本文件定义 Aemeath Agent 项目的多 session 协作方式。所有 coding session 在开始工作前应先阅读本文件，再按需阅读 `docs/architecture.md`、`docs/interfaces.md`、`docs/task_log.md`。

## Manager Session 身份

本 session 是 Aemeath Agent 的 manager session。

职责：

- 维护整体架构、模块边界、接口契约和版本路线。
- 拆分任务，明确每个 worker session 的输入、输出、负责文件和验收标准。
- 审查 worker session 的设计是否破坏解耦、接口稳定性或后续扩展性。
- 维护 `docs/task_log.md`、`docs/architecture.md`、`docs/interfaces.md` 和关键 debug 记录。
- 在功能实现前优先定义稳定 port，在实现后更新文档和测试。

非职责：

- 不长期占用某个具体功能模块的细节实现。
- 不把临时实验直接变成核心架构。
- 不在没有接口说明的情况下让多个 session 同时修改同一组文件。

## Session 类型

### Manager Session

负责项目统筹。默认拥有以下文件的维护权：

- `AGENTS.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `docs/debug_log.md`
- 任务拆分和跨模块接口调整

Manager 可以少量修改代码以建立接口边界，但大块功能实现应拆给 worker session。

### Worker Session

负责一个清晰功能或模块的实现。开始前应拿到：

- 任务编号
- 目标能力
- 允许修改的文件范围
- 必须遵守的接口
- 验收命令

Worker 完成后应汇报：

- 修改了哪些文件
- 新增或变更了哪些接口
- 运行了哪些测试
- 还有哪些风险或未完成项

### Review Session

负责代码审查、架构一致性检查和回归风险检查。Review 默认不做大规模重写，只指出问题或做小修。

## 项目设计原则

1. Controller 只编排，不承载业务判断。

   `DialogueController` 应保持为流程编排层。角色、记忆、状态、检索、模型调用都通过注入的 port 完成。

2. 业务模块依赖协议，不依赖具体存储或模型供应商。

   示例：记忆读写依赖 `MemoryStore`，模型调用依赖 `ChatModelClient`。

3. RAG / tool / MCP 能力通过 adapter 接入，不进入核心业务层。

   Memory、RAG、Character、State 的核心策略留在内部模块。MCP 可以作为受控访问出口或外部资源来源，但不决定是否写记忆、如何塑造角色或如何更新关系状态。

4. SQLite 是 v0.1 实现，不是永久边界。

   后续可以替换为向量库、Postgres、文件索引或混合存储，但上层接口应尽量稳定。

5. Character 是可升级系统，不只是 YAML 文件。

   v0.1 使用 `aemeath.v0.1.yaml`。后续应逐步演进为 profile、voice、lore、examples、safety、version manifest 的组合式角色系统。

6. Prompt context 是系统集成点。

   `ContextBuilder` 汇合角色上下文、短期对话、长期记忆、关系状态和剧情证据。新增知识系统时优先接入 `EvidencePack`。

7. 所有跨 session 任务必须有文档落点。

   新增能力至少更新 `docs/task_log.md`。涉及边界或接口时更新 `docs/architecture.md` 或 `docs/interfaces.md`。

## 当前运行与验证

安装：

```bash
pip install -e .[dev]
```

运行 CLI：

```bash
python -m app.cli
```

测试：

```bash
pytest
ruff check .
```

真实 DeepSeek 调用需要 `.env`：

```env
AEMEATH_USE_MOCK_LLM=false
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_THINKING=disabled
DEEPSEEK_REASONING_EFFORT=
```

不要在日志、文档、提交信息或测试输出中写入真实 API key。

## 文件修改规则

- 修改代码前先确认负责范围，避免多个 session 同时改同一文件。
- 不回滚其他 session 或用户已有修改。
- 新接口应优先放在已有模块的 port/protocol 文件中。
- 新功能必须有最小测试；如果无法测试，需要在任务记录中说明原因。
- 涉及模型调用、角色行为、记忆写入、状态更新的变更，应记录验收样例。
- 跨模块新增能力必须说明 trace 落点；如果暂不落地 trace，需要在 `docs/task_log.md` 中说明原因。

## 推荐任务流

1. Manager 在 `docs/task_log.md` 创建任务。
2. Manager 指定 owner、scope、接口、验收标准。
3. Worker 实现任务并运行测试。
4. Worker 汇报变更。
5. Manager 或 Review 检查接口一致性。
6. Manager 更新任务状态和架构文档。
