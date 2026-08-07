# Aemeath Agent 统一错误处理设计

## 2026-07-06 Manager Review Update

Error Handling v0.1 is accepted after the worker follow-up patch.

Additional coverage confirmed:

- Sensitive key variants `api-key`, `x-api-key`, and `x_api_key` are redacted.
- Ordinary fields such as `ordinary-key` are not over-redacted.
- Failed controller prompt-run logs include `error_code`, `error_type`,
  `error_public_message`, and sanitized `error`.
- CLI normal-turn failure output keeps a stable public error code, including `[SESSION_ENDED]`.

Final validation:

- `pytest`: 105 passed
- `ruff check .`: All checks passed
- `python -m compileall -q app`: passed

## 2026-08 Streaming Chat Addendum

Streaming chat adds stable `CHAT_REQUEST_IN_PROGRESS`, `CHAT_REQUEST_CONFLICT`,
`CHAT_STREAM_CANCELLED`, and `CHAT_STREAM_INTERRUPTED` codes. Cancelled and interrupted terminals
are non-retryable in v0.1. Provider-specific finish reasons and raw response/exception bodies are
never persistence reasons or public errors.

Before `text/event-stream` headers, errors use the normal HTTP JSON envelope and status mapping.
After headers, business failures terminate through exactly one safe `turn.failed` event. A raw ASGI
`OSError` or Starlette `ClientDisconnect` from downstream send is classified as
`client_disconnected`, suppressed after complete off-loop cleanup, and must not rewrite a durable
completion that already won its persistence CAS.

本文档记录 v0.3 之后 `app/errors/` 的第一阶段错误契约。目标是在接入
FastAPI、Web UI、更多 worker 或外部工具之前，先建立一套 CLI、API、Trace、
Prompt Log 都能复用的稳定错误体系。

## 设计目标

- 对外暴露稳定 `error_code`，不要让调用方依赖 Python 异常类名或原始异常文本。
- 区分用户可见消息和内部调试详情。
- API key、authorization、cookie、token、password、secret 等敏感信息不得进入错误响应、
  trace 或 prompt log。
- `DialogueController` 仍保持编排职责，不在 Controller 内写复杂错误分类逻辑。
- 第一阶段不要求业务模块全部改成继承统一异常；通过 mapper 兼容现有异常。
- 为未来 HTTP status 映射预留接口，但不依赖 FastAPI。

## 模块结构

```text
app/errors/
  __init__.py
  base.py       # ErrorInfo, AemeathError
  codes.py      # ErrorCode
  sanitizer.py  # 敏感字段与文本脱敏
  mapper.py     # map_exception(exc) -> ErrorInfo
  http.py       # http_status_for_error(error_info) -> int
```

### ErrorInfo

`ErrorInfo` 是边界层应使用的标准错误对象。

字段：

- `code`：稳定错误码，类型为 `ErrorCode`。
- `public_message`：可展示给用户或 API 调用方的安全消息。
- `internal_message`：脱敏后的内部错误摘要，用于 trace、prompt log、debug。
- `error_type`：原始异常类型名，用于排查。
- `retryable`：是否适合重试。
- `metadata`：脱敏后的结构化补充信息。

常用输出：

- `to_public_dict()`：只包含 `code/message/retryable`，适合 API response。
- `to_log_dict()`：包含内部摘要和 metadata，适合 trace / prompt log。

### AemeathError

`AemeathError` 是未来新业务异常可以继承或直接抛出的统一基类。第一阶段没有强制改造
已有 `SessionError`、`MemoryExtractionError` 等异常，以避免大规模重写。

## 当前接入点

### CLI

`app/cli.py` 在 `/session`、`/memory` 和普通 turn 失败时调用 `map_exception(exc)`。
CLI 继续保持原有交互方式，但输出稳定错误码，例如：

```text
[memory] command failed: Memory extraction returned an invalid model response. [MEMORY_INVALID_MODEL_OUTPUT]
Aemeath> 当前处理失败：Session has ended. Resume it before sending messages. [SESSION_ENDED]
```

### DialogueController

`DialogueController` 不负责错误分类，只在失败边界调用 `map_exception(exc)`：

- `session_validation_failed` trace 记录 `ErrorInfo.to_log_dict()`。
- `turn_failed` trace 记录结构化错误对象。
- failed prompt run 记录 `error_code`、`error_type`、`error_public_message` 和脱敏后的
  `error`。

Controller 仍然在记录后继续抛出原异常，由 CLI 或未来 API 边界决定如何展示。

### Prompt Log Inspector

`app/observability/prompt_log_inspector.py` 会在失败记录中展示：

- `Error code`
- `Error type`
- `User message`
- `Error`

旧 prompt log 中没有这些字段时，inspector 会显示 `(unknown)` 或 `(none)`。

## 错误码语义

### 通用

- `INTERNAL_ERROR`：未分类内部错误。边界层应返回通用失败消息。
- `INVALID_INPUT`：用户输入或调用参数不合法。

### Auth / Permission

- `AUTH_REQUIRED`：请求缺少身份凭证。
- `AUTH_INVALID_CREDENTIALS`：dev/static token 等凭证无效。
- `AUTH_ACCOUNT_DISABLED`：账号存在但已禁用。
- `PERMISSION_DENIED`：非 session 资源的通用 owner-only 权限拒绝。

### 配置

- `CONFIG_INVALID`：配置值格式或取值非法。
- `CONFIG_MISSING_SECRET`：运行所需密钥缺失，例如真实模型模式下缺少模型 API key。

### Session

- `SESSION_NOT_FOUND`：请求的 session 不存在。
- `SESSION_OWNERSHIP_DENIED`：session 不属于当前用户或角色。对外消息不应泄露 session
  是否真实存在或归属信息。
- `SESSION_ENDED`：session 已结束，需要显式 resume。
- `SESSION_ARCHIVED`：session 已归档，当前版本不可继续使用。
- `SESSION_INVALID_TRANSITION`：非法生命周期切换。
- `SESSION_MIGRATION_FAILED`：session 数据迁移失败。

### Character

- `CHARACTER_NOT_FOUND`：请求了未注册的角色。
- `CHARACTER_LOAD_FAILED`：角色文件读取失败，或 fallback 也无法加载。
- `CHARACTER_INVALID_MANIFEST`：角色 manifest/card 结构非法。

### Memory

- `MEMORY_INVALID_SOURCE`：记忆写入或提炼的消息不属于当前 user/session/character。
- `MEMORY_EXTRACTION_FAILED`：记忆提炼流程失败的通用错误。
- `MEMORY_INVALID_MODEL_OUTPUT`：模型返回的记忆提炼结果不是合法 JSON 或结构不符合契约。

### Model

- `MODEL_BAD_REQUEST`：模型请求被 provider 判定为参数非法。
- `MODEL_AUTH_FAILED`：模型鉴权失败。
- `MODEL_RATE_LIMITED`：模型服务限流。
- `MODEL_TIMEOUT`：模型请求超时。
- `MODEL_UNAVAILABLE`：模型服务不可用、连接失败或 provider 5xx。
- `MODEL_INVALID_RESPONSE`：模型响应成功返回，但结构不符合预期。

### Storage

- `STORAGE_UNAVAILABLE`：数据库或存储层暂时不可用。
- `STORAGE_MIGRATION_FAILED`：通用存储 schema 迁移失败。
- `STORAGE_CONSTRAINT_VIOLATION`：唯一约束、外键或业务存储约束冲突。

## HTTP status 映射

`app/errors/http.py` 提供 `http_status_for_error(error_info)`，未来 API 层可直接复用。

当前建议映射：

- `400`：`INVALID_INPUT`
- `401`：`AUTH_REQUIRED`、`AUTH_INVALID_CREDENTIALS`
- `403`：`AUTH_ACCOUNT_DISABLED`、`PERMISSION_DENIED`、`SESSION_OWNERSHIP_DENIED`、
  `MEMORY_INVALID_SOURCE`
- `404`：`SESSION_NOT_FOUND`、`CHARACTER_NOT_FOUND`
- `409`：session 生命周期冲突、存储约束冲突
- `429`：`MODEL_RATE_LIMITED`
- `500`：配置、迁移、未知内部错误
- `502`：模型 bad request、鉴权失败、响应非法、记忆提炼模型输出非法
- `503`：模型或存储暂时不可用
- `504`：模型请求超时

API response 示例：

```json
{
  "error": {
    "code": "MODEL_TIMEOUT",
    "message": "The model request timed out. Please try again.",
    "retryable": true
  }
}
```

## 特殊处理策略

这些错误不能只做普通展示，需要边界层或对应 adapter 做额外决策：

- `CONFIG_MISSING_SECRET`：启动期应失败退出，不进入半可用状态。
- `SESSION_OWNERSHIP_DENIED`：对外不要泄露 session 是否存在或属于谁。
- `SESSION_ENDED`：可恢复，提示用户 resume；Controller 不应产生消息、记忆或状态副作用。
- `SESSION_ARCHIVED`：不可继续使用，通常返回冲突错误。
- `MODEL_AUTH_FAILED`：不适合重试；后续应避免被 fallback 完全掩盖。
- `MODEL_RATE_LIMITED`、`MODEL_TIMEOUT`、`MODEL_UNAVAILABLE`：可重试或 fallback。
- `MEMORY_INVALID_SOURCE`：安全/一致性错误，不应静默跳过。
- `MEMORY_INVALID_MODEL_OUTPUT`：不能写入长期记忆，应记录失败并等待重试或人工排查。
- `SESSION_MIGRATION_FAILED`、`STORAGE_MIGRATION_FAILED`：阻断类错误，继续写入可能扩大损坏。

## 模型请求重试

`DeepSeekClient` 当前已支持临时错误重试：

- 默认 `max_retries=3`，含义是初始请求失败后最多再重发 3 次。
- 对 `TimeoutError`、`URLError` 和 HTTP `408/429/500/502/503/504` 重试。
- 不重试 HTTP `400/401/403`。
- 默认指数退避：`0.5s -> 1s -> 2s`。

注意：当前重试结果尚未结构化写入 trace，例如 `retry_count`、`fallback_used`。这可以作为后续
Model Error Taxonomy 或 ModelRequest/ModelResponse 任务的一部分。

## 新增错误时的流程

1. 如果是新的稳定错误类别，先在 `app/errors/codes.py` 增加 `ErrorCode`。
2. 在 `app/errors/mapper.py` 中增加现有异常到 `ErrorInfo` 的映射。
3. 如果未来 API 需要不同 HTTP 语义，在 `app/errors/http.py` 增加映射。
4. 如果错误中可能包含密钥、header、cookie、URL query、prompt 内容，先确认
   `sanitizer.py` 能脱敏。
5. 增加或更新 `tests/test_errors.py`，至少覆盖 code、public message、retryable 或 HTTP status。

## 不应做的事

- 不要在 API response 中返回 `str(exc)`。
- 不要把 API key、authorization、cookie、token、password、secret 写入错误响应。
- 不要让 Controller 判断 HTTP status 或模型 provider 细节。
- 不要为了错误码一次性重写所有业务异常继承链。
- 不要让 Prompt Log 默认保存未脱敏的 provider 原始响应或完整异常上下文。
