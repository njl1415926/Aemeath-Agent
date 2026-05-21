# Aemeath Agent Debug Log

本文档记录 Aemeath Agent 开发过程中的关键问题、定位过程、根因、修复方式与验证结果。

## 2026-05-20 DeepSeek v4 调用返回“模型服务暂时不可用”

### 问题现象

运行 v0.1 CLI 并调用真实模型时，Agent 没有返回 DeepSeek 的真实回复，而是进入 fallback，输出：

```text
模型服务暂时不可用，我先保证对话不中断。
```

用户已经在 `.env` 中配置了 `DEEPSEEK_API_KEY`，因此需要确认问题是环境变量加载、API key、模型名、接口地址还是请求体参数。

### 当时相关配置

`.env` 中的关键配置为：

```env
AEMEATH_USE_MOCK_LLM=false
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_THINKING=disabled
DEEPSEEK_REASONING_EFFORT=high
```

出于安全原因，debug 过程中只确认 API key 存在与长度，不记录、不打印、不保存真实 key。

### 定位过程

1. 检查 `.env` 是否被读取。

   结果：`.env` 被正确读取，`AEMEATH_USE_MOCK_LLM=false` 生效，API key 存在。

2. 检查官方 API 形态。

   DeepSeek 官方文档确认：

   - Chat Completions endpoint 可以使用 `https://api.deepseek.com/chat/completions`
   - `deepseek-v4-pro` 是可调用模型
   - v4 thinking 参数通过 `thinking` 控制

3. 直接向 DeepSeek 发送多组最小请求。

   测试结果：

   - `deepseek-v4-pro` + `thinking={"type":"enabled"}` + `reasoning_effort=high`：成功
   - `deepseek-v4-pro` + `thinking={"type":"disabled"}` 且不发送 `reasoning_effort`：成功
   - `deepseek-v4-flash` + `thinking={"type":"disabled"}`：成功
   - `deepseek-chat` 最小请求：成功
   - 当前代码请求体 `thinking={"type":"disabled"}` + `reasoning_effort=high`：失败

4. 捕获到 DeepSeek 返回的 HTTP 400 错误：

```json
{
  "error": {
    "message": "thinking options type cannot be disabled when reasoning_effort is set",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_request_error"
  }
}
```

### 根因

问题不是 `.env` 没有加载，也不是 API key 无效。

根因是 `DeepSeekClient` 固定把以下两个参数同时发送给 API：

```json
{
  "thinking": {"type": "disabled"},
  "reasoning_effort": "high"
}
```

DeepSeek v4 的参数语义要求：

- 当 `thinking.type=enabled` 时，可以设置 `reasoning_effort`
- 当 `thinking.type=disabled` 时，不能设置 `reasoning_effort`

因此服务端返回 HTTP 400。当前 `FallbackChatModelClient` 捕获 HTTPError 后进入 mock fallback，所以用户只看到“模型服务暂时不可用”。

### 修复内容

#### 1. 修复 DeepSeek 请求体构造

位置：

- `app/model/deepseek.py`

新增 `_build_payload()`，根据当前配置动态构造请求体：

- 始终发送 `model`、`messages`、`temperature`
- 当 `DEEPSEEK_THINKING` 非空时发送 `thinking`
- 只有当 `thinking=enabled` 且 `DEEPSEEK_REASONING_EFFORT` 非空时，才发送 `reasoning_effort`
- 当 `thinking=disabled` 时，即使 `.env` 中误填了 `DEEPSEEK_REASONING_EFFORT=high`，客户端也不会发送非法组合

核心逻辑：

```python
thinking = self.thinking.strip().lower()
if thinking:
    payload["thinking"] = {"type": thinking}

reasoning_effort = self.reasoning_effort.strip().lower()
if thinking == "enabled" and reasoning_effort:
    payload["reasoning_effort"] = reasoning_effort
```

#### 2. 修复默认配置

位置：

- `app/config.py`
- `.env`
- `.env.example`
- `README.md`

将默认配置从：

```env
DEEPSEEK_REASONING_EFFORT=high
```

改为：

```env
DEEPSEEK_REASONING_EFFORT=
```

这样默认是关闭 thinking 的普通 v4 调用。如果后续需要开启 reasoning，再同时配置：

```env
DEEPSEEK_THINKING=enabled
DEEPSEEK_REASONING_EFFORT=high
```

#### 3. 增加回归测试

位置：

- `tests/test_deepseek_client.py`

新增测试：

- `thinking=disabled` 时不发送 `reasoning_effort`
- `thinking=enabled` 时保留 `reasoning_effort`

### 验证结果

本地自动化验证：

```text
pytest
7 passed

ruff check .
All checks passed
```

真实 DeepSeek 烟测：

```text
model=deepseek-v4-pro
thinking='disabled'
reasoning_effort=''
live_reply='pong'
```

说明修复后真实模型调用已经成功。

### 后续注意点

1. `.env` 是真实运行配置，`.env.example` 只是模板。

   `Settings.from_env()` 通过 `python-dotenv` 的 `load_dotenv()` 读取项目根目录下的 `.env`。

2. 不要在日志、文档、测试输出中记录真实 API key。

   debug 时只允许记录：

   - key 是否存在
   - key 长度
   - 使用的 base url
   - 使用的模型名
   - HTTP 状态码
   - 服务端错误信息

3. DeepSeek v4 的 thinking 参数应作为独立能力开关管理。

   推荐默认配置：

   ```env
   DEEPSEEK_MODEL=deepseek-v4-pro
   DEEPSEEK_THINKING=disabled
   DEEPSEEK_REASONING_EFFORT=
   ```

   如果后续为复杂角色推理、剧情一致性审查、记忆冲突判断等任务启用深度思考，再显式配置：

   ```env
   DEEPSEEK_THINKING=enabled
   DEEPSEEK_REASONING_EFFORT=high
   ```

4. 未来建议将模型错误分层。

   当前 fallback 能保证 CLI 对话不中断，但对 debug 不够友好。后续可以新增：

   - `ModelClientError`
   - `ModelAuthError`
   - `ModelRateLimitError`
   - `ModelBadRequestError`
   - `ModelUnavailableError`

   这样 CLI 可以继续保持温和兜底，而开发日志或 trace viewer 可以记录更精确的错误类型。

### 参考

- DeepSeek Chat Completion API: https://api-docs.deepseek.com/api/create-chat-completion
- DeepSeek Thinking Mode: https://api-docs.deepseek.com/guides/thinking_mode
