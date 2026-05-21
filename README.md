# Aemeath Agent

Aemeath Agent v0.1 是一个以《鸣潮》爱弥斯为原型的 CLI 对话 Agent，目标是跑通最小记忆闭环。

## v0.1 Features

- CLI 多轮对话
- 角色卡 YAML 加载
- SQLite 对话日志
- 长期记忆写入与检索
- 关系 / 情绪状态更新
- Memory Admin 命令：`/memory list|delete|disable`
- MockLLMClient（测试默认不依赖真实模型）

## v0.1 Boundaries

当前版本不包含：剧情知识库、RAG、Critic、Trace Viewer、Web UI、多 Agent、MCP。

## Quick Start

1. 安装依赖

```bash
pip install -e .[dev]
```

2. 配置本地环境变量

`.env.example` 是模板，真实配置放在本地 `.env`。`.env` 已被 `.gitignore` 忽略，不会提交到仓库。

Mock 模式：

```env
AEMEATH_USE_MOCK_LLM=true
```

使用 DeepSeek v4：

```bash
copy .env.example .env
```

然后编辑 `.env`：

```env
AEMEATH_USE_MOCK_LLM=false
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_THINKING=disabled
DEEPSEEK_REASONING_EFFORT=
```

如果希望速度优先，可以把 `DEEPSEEK_MODEL` 改为 `deepseek-v4-flash`。

3. 启动 CLI

```bash
python -m app.cli
```

## CLI Commands

- `/help`
- `/exit`
- `/memory list`
- `/memory delete <memory_id>`
- `/memory disable <memory_id>`

## Test

```bash
pytest
ruff check .
```
