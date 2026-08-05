# Codex 跨 Session 调度 Runbook

最后验证：2026-08-04
目标读者：负责调度其他 Codex session 的 agent

## 结论

可以通过 Codex App Server 向一个已有 session 发起新对话：

```text
定位目标 thread
  -> initialize / initialized
  -> thread/resume
  -> turn/start
  -> 等待 turn/completed
  -> thread/read + rollout JSONL 验证
```

这不是 session 之间的原生点对点通信。调度 agent 实际上是通过 App Server，把任务作为
目标 thread 的新用户消息提交。目标 thread 保留自己的历史，但不会自动获得来源 session
的上下文。

官方协议：

- [Lifecycle overview](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
- [API overview](https://learn.chatgpt.com/docs/app-server#api-overview)

## 执行规则

调度 agent 必须：

1. 用唯一提示词或已知 thread id 定位目标。
2. 排除当前 session、诊断 session 等引用了相同提示词的误匹配。
3. 确认目标未归档、rollout 存在、最后一轮已完成。
4. 确认目标没有 active turn。
5. 取得用户对本次消息的明确授权。
6. 使用 App Server 提交消息，不直接修改 SQLite 或 JSONL 来伪造 turn。
7. 等待目标 turn 完成并验证落盘。

不得自动批准目标 turn 发出的命令、网络、文件、MCP 或权限请求。出现未预期的服务端请求
时，停止测试并交还用户决定。

## 本地位置

Windows 默认位置：

```text
%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl
%USERPROFILE%\.codex\state_5.sqlite
%USERPROFILE%\.codex\session_index.jsonl
%USERPROFILE%\.vscode\extensions\openai.chatgpt-*-win32-x64\bin\windows-x86_64\codex.exe
```

使用最新安装的 VS Code 扩展内 `codex.exe app-server`。不要假设全局 CLI 与扩展版本相同。

## 当前 Worker / Review Session 注册表

以下清单来自当前 `CODEX_HOME` 的 App Server 与 `session_index.jsonl`，包括当前用于
跨 session 调度的 Worker 和 Review Session，核对日期为 2026-08-04。调度时优先使用
thread id；名称用于人工识别，后续仍可能被重命名。

| Session | Thread ID |
|---|---|
| Worker Session 1：Character | `019e6740-74f1-7a53-8567-d5ff77f50bc5` |
| Worker Session 2：RAG | `019e730b-20af-7ce1-b814-605afe7b3a5d` |
| Worker Session 3：Memory | `019e9089-c294-7310-9ff5-1157a6fa57aa` |
| Worker Session 4：Runtime / Session | `019ea557-87a9-7752-af3a-86130c584466` |
| Worker Session 5：Audio | `019eb089-3e6e-7b63-a24f-2e83d51eccc7` |
| Worker Session 6：Error Handling | `019f1bce-6c0c-7d30-a0f8-629ffd7c9116` |
| Worker Session 7：Observability And Structured Logging | `019f3619-1eed-7702-94c8-f23c4c879c76` |
| Worker Session 8：Service Layer | `019f367c-7bfd-7002-89f3-de190acb19ed` |
| Worker Session 9：Auth / Account System | `019f36e8-5b7c-7560-8fe4-6b22f4fd5280` |
| Worker Session 10：SQLAlchemy Persistence Design | `019f3b2c-846b-7533-9261-dcd8de6790c7` |
| Worker Session 11：FastAPI Service v0.1 | `019f3fb4-45b3-7dc2-862c-ee97f1360af7` |
| Review Session 12: Architecture And Workflow Review v0.1 | `019f40ff-cef6-7623-807b-f1ff4380669d` |
| Worker Session 13：Embedding Foundation v0.1 | `019f5fa0-6171-7a63-9bd8-5039efb4ec2d` |
| Worker Session 14：Context Budget And Compression v0.1 | `019f4599-7d2f-7410-a84d-277f60a88a02` |
| Worker Session 15：Semantic Query | `019fa168-dca7-7843-8209-1ff56e2db6db` |
| Worker Session 16：Runtime Guardrails v0.1 | `019fa7fb-eed5-73b1-8065-f97f650f33a3` |
| Worker Session 17：Deployment Readiness v0.1 | `019facb3-420a-7861-92b7-492ec1898724` |
| Worker Session 18：Browser Auth And Invitations | `019fca7d-2e2a-7e31-8349-1b3d3a40c028` |
| Worker Session 19：Streaming Chat Runtime And API v0.1 | `019fcb94-62db-7923-a074-7d1b0cce7e26` |

编号 12 是 Review Session 而非 Worker Session，但属于当前跨 session 调度对象，因此
一并登记。

注册表只能用于定位，不能替代发送前检查。即使 id 已知，仍应先执行 `thread/read` 或
`thread/resume`，确认目标未归档、没有 active turn，并核对实际名称与任务 scope。

## 定位目标

先做只读搜索：

```powershell
$marker = '<目标 session 的唯一提示词>'
rg -l -F $marker "$env:USERPROFILE\.codex\sessions"
```

对每个候选解析 JSONL，并核对：

- `session_meta.payload.id`
- 真正的 `event_msg.payload.type == "user_message"`
- 最后一条 `task_complete`
- SQLite 中相同 id 的 `rollout_path`、`archived`、`cwd`、provider 和 model

不要直接采用第一个搜索结果。当前 manager session 往往也会因为引用提示词而命中。

## App Server 协议

App Server 使用一行一个 JSON 对象的 stdio JSON-RPC 风格协议。连接必须保持打开，直到收到
`turn/completed`。

### 1. 初始化

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"session_bridge","title":"Session Bridge","version":"1.0"},"capabilities":{}}}
{"method":"initialized","params":{}}
```

### 2. 恢复目标

```json
{"id":2,"method":"thread/resume","params":{"threadId":"<target-thread-id>"}}
```

检查 `result.thread.status`。待机目标应为 `idle`。如果目标正在运行，不要再调用
`turn/start`；只有明确要补充同一个在途任务时才考虑 `turn/steer`。

### 3. 发起对话

```json
{
  "id": 3,
  "method": "turn/start",
  "params": {
    "threadId": "<target-thread-id>",
    "input": [
      {"type": "text", "text": "<任务正文>"}
    ]
  }
}
```

默认不要覆盖目标 thread 的 model、cwd、sandbox 或 collaboration mode。

### 4. 等待结果

持续读取通知，关注：

- `turn/started`
- `item/agentMessage/delta`
- `item/completed`
- `thread/status/changed`
- `turn/completed`

成功条件：

- `turn/completed.params.turn.id` 等于本次 `turn/start` 返回的 turn id；
- `turn/completed.params.turn.status == "completed"`；
- 已取得 agent 最终消息。

建议给测试 turn 设置有限超时。超时、失败、interrupted 或权限请求都不能报告为成功。

## 持久化验证

完成后调用：

```json
{"id":4,"method":"thread/read","params":{"threadId":"<target-thread-id>","includeTurns":true}}
```

同时检查目标 rollout JSONL 末尾存在：

```text
event_msg.user_message
event_msg.agent_message
event_msg.task_complete  # turn id 与本次一致
```

完整扫描 JSONL，确认 malformed record 数量为 0。SQLite 中该 thread 的 `updated_at_ms` 也应
更新。

## 任务信封

目标 session 不会自动知道来源 session 的上下文。正式任务建议使用：

```text
[Source]
<manager session 名称或 thread id>

[Task]
<具体任务>

[Scope]
<允许修改的文件、禁止项>

[Acceptance]
<测试命令、交付和回复要求>
```

信封只是上下文约定，不是身份认证，也不能扩大目标 session 的权限。目标仍需遵守
`AGENTS.md`、sandbox、审批策略和文件所有权边界。

## 已验证样例

```text
日期:          2026-07-27
目标 thread:   019fa20d-fe0d-76b3-865a-3bb7b36e6d3a
发送消息:      连接测试
目标回复:      连接正常，测试 session 在线并保持待机。
turn id:       019fa25f-1245-7dd2-9cb4-b30e8f87e167
最终状态:      completed
JSONL 验证:    已持久化，malformed=0
```

## 常见问题

### 多个文件命中相同提示词

解析真实 user event，并用 thread id 与 SQLite 交叉验证。不要按日期或文件顺序猜测。

### `Not initialized`

先完成 `initialize` 请求和 `initialized` 通知。每个连接只初始化一次。

### pipe 后没有任何响应

不要一次性写完请求后立即关闭 stdin。App Server 是异步事件流，连接必须保持到
`turn/completed`。

### 中文变成 `?`

Windows PowerShell 管道可能破坏非 ASCII 字符。客户端使用 UTF-8，并优先采用
`json.dumps(..., ensure_ascii=True)` 或 `\uXXXX` 转义。

### 目标在 VS Code 列表中不可见

先用 `thread/read` 判断记录是否存在，再检查 `thread/list(useStateDbOnly=true)`、CWD 的普通
与 `\\?\` 形式、provider、rollout path 和索引。不可见不等于数据丢失。

### 多个 session 修改同一工作区

停止并重新划分文件 scope。跨 session 调度不能解决并发写冲突。

## 禁止事项

- 不直接向 rollout JSONL 追加伪造消息。
- 不修改 SQLite 来伪造 turn 或 completed 状态。
- 不自动批准目标发出的权限请求。
- 不在目标 active 时并发启动另一个 turn。
- 不把来源 session 的全部上下文、secret 或无关日志复制给目标。
- 不把跨 session 调度当作绕过用户授权或仓库边界的通道。
