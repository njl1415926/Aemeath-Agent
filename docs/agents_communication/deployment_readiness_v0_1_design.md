# Deployment Readiness v0.1 设计

- Worker：Worker Session 17 / Deployment Worker
- 状态：**Approved — Implementation Authorized**
- 设计日期：2026-07-29
- 目标环境：阿里云 ECS，Ubuntu 22.04 LTS x86_64
- 本地验收环境：WSL2 Ubuntu 22.04
- 核对基线：`checkpoint/semantic-query-20260728` / `5b1fe0b feat: add runtime guardrails`
- 回归基线：Manager 提供且 `docs/task_log.md` 记录为 `506 passed`；本轮未重新执行运行时回归
- 本轮边界：只提交本设计文档；不实施 Docker、API、脚本或配置改动，不更新任务状态

## 0. 结论摘要

首发 v0.1 冻结为**单用户、封闭测试、单机、单 API 进程**部署：Caddy 负责 TLS、原始请求体上限和反向代理；FastAPI/Uvicorn 固定一个 application worker；`AEMEATH_ENABLE_LONG_TERM_MEMORY=false`，Compose 定义但**不创建、不启动 Memory Worker**。首发 API 只经 HTTPS 调用 DeepSeek；SiliconFlow/Memory Worker 需通过专项门禁后才启用。镜像不安装 GPU、CUDA、PyTorch 或本地 embedding 依赖。

这套方案适合低流量、单 ECS 的早期上线，不是高可用方案。它明确保留现有业务边界：

- active Evidence Provider 仍为 `WorldInfoEvidenceProvider`；`data/rag_indexes` 仅作为现有 generation 工具产物持久化，不接入在线检索。
- 不修改 `DialogueController`、Character、Memory、RAG 的业务策略。
- 不接入 Audio/GPT-SoVITS、PostgreSQL/Alembic、Streaming、Web UI 或完整账号系统。
- API model fallback、mock LLM、hash embedding 和免认证 local default user 在生产环境启动时必须被拒绝。
- `AEMEATH_ENABLE_CONTEXT_BUDGET=true`；CORS allowlist 为空；静态 Bearer 只允许封闭测试使用。
- App Log 保留 30 天、Trace Log 保留 14 天、Prompt Log 关闭，只允许既有安全摘要字段。
- SQLite backup 为 nightly + 每次升级前，暂定 RPO 24 小时、RTO 2 小时。

整体架构和 API-only 首发范围已通过 Manager 复审，可以按第 21 节分阶段实施。SQLite 双进程连接策略和 `running` job lease/stale recovery 不阻塞 API-only 首发，却是启用 Memory Worker 前的双重硬门禁。详见第 12、17、18 节。

## 1. 已核对的真实实现

### 1.1 当前已经具备

1. `app.api.server:create_app` 可以作为 Uvicorn factory 启动；一个 app 内只构造一个 `ApplicationRuntime`，lifespan 退出时同步调用 `application_runtime.close()`。
2. `GET /health` 当前是固定的轻量存活响应，不访问数据库或 Provider。
3. API 认证、Session admission、全局并发门禁、Provider 并发门禁、按 user/IP 的 token bucket 和外层 timeout 已实现。
4. `python -m app.memory.worker` 可作为独立进程运行；它与 API 使用同一个 `AEMEATH_DB_PATH`，并有独立的 DeepSeek/SiliconFlow 客户端与进程内并发门禁。
5. SQLite raw backend 是默认且稳定的生产候选；SQLAlchemy backend 仍标注为 experimental。
6. DeepSeek 对话/抽取和 SiliconFlow BGE-M3 都是外部 HTTP API；生产部署不需要 GPU。
7. App、Trace、Prompt logger 分别按 `root/YYYY/MM/DD/*.jsonl` 写文件；文件名包含时间戳和随机后缀，API 与 Worker 不会固定写同一个文件。
8. RAG generation 工具支持 `AEMEATH_RAG_INDEX_ROOT`；在线 runtime 仍固定装配 `WorldInfoEvidenceProvider`。

### 1.2 当前缺口

1. 仓库没有 `Dockerfile`、Compose、反向代理配置、部署 preflight、数据库备份/恢复脚本或日志清理脚本。
2. 只有 `/health`，没有 `/ready`；现有 `/health` 不能证明 SQLite 与必需资源可用。
3. FastAPI 没有 CORS 配置；原始 HTTP body 也没有在 ASGI 解析前受限。
4. `Settings.from_env()` 当前无环境模式，且无条件调用 `load_dotenv()`；没有 `_FILE` secret 读取约定，也没有 prod-only fail-fast 校验。
5. `Memory Worker` 只捕获 `KeyboardInterrupt`，没有明确的 `SIGTERM` drain 协议；启动/停止主要写普通 stdout，而不是完整的结构化生命周期事件。
6. SQLite raw connection 使用默认 journal/timeout 行为；没有显式 `WAL`、`busy_timeout` 或跨进程写竞争验收合同。API 与 Worker 共享 DB 时存在 `database is locked` 风险。
7. Worker 被 `SIGKILL`、宿主机断电或 OOM kill 时，已 claim 的 `running` job 没有已验证的 stale recovery 机制。
8. JSONL logger 没有文件大小轮转、总量限制或保留期；Docker stdout 也尚未约束日志驱动。
9. 仓库声明 Python `>=3.10`，但没有部署 lockfile；只依赖浮动的 `pyproject.toml` 无法保证日后重建得到同一依赖集合。

以上缺口是实施清单，不代表已完成。

### 1.3 实现核对定位

| 核对位置 | 用于确认的部署事实 |
|---|---|
| `app/api/server.py`、`app/runtime/resources.py`、`app/api/routes/health.py` | factory/lifespan、同步 `ApplicationRuntime.close()`、单 runtime、当前只有轻量 `/health`、尚无 CORS/readiness |
| `app/api/routes/chat.py`、`app/api/dependencies.py` | bearer user 边界、`request.client.host` 的限流输入、API 请求路径 |
| `app/main.py` | runtime 装配、API fallback 拒绝、WorldInfo active、长期记忆共享 embedding wrapper |
| `app/config.py`、`.env.example` | 当前环境名/default/validate、无条件 dotenv、尚无 `_FILE` secret 合同 |
| `app/memory/worker.py` | 独立进程、共享 DB、job repair/claim、Provider 生命周期、缺少 SIGTERM drain |
| `app/db/store_factory.py`、`app/db/sqlite_store.py`、`app/db/session.py` | raw SQLite 默认、连接/事务、未显式配置 WAL/busy timeout |
| `app/observability/*` | JSONL 路径/文件生命周期、App/Trace 安全字段、Prompt Log 的高敏边界 |
| `pyproject.toml`、`.gitignore` | Python 3.10+、API extra、无部署 lock、state/source/audio 排除范围 |
| 指定 architecture/interfaces/task/service/observability/error/persistence/sequence/guardrails 文档 | single-worker、错误/日志/持久化/业务非目标与 Manager ownership |

本轮只读取 `.env.example`，未读取本地 `.env`；仓库也未发现现成 Docker/Compose/部署脚本可直接复用。

## 2. 设计原则与非目标

### 2.1 原则

- 单一日常写入边界：首发只有 API 能以读写方式挂载生产 data；未来通过门禁后 Memory Worker 才取得相同权限。备份容器只读挂载源 DB、读写挂载 backups。只有在 writers 已停止的恢复窗口，专用一次性 restore profile 才能读写 data。
- 单一封闭测试入口：只发布反向代理的 `80/443`，不发布 API `8000`，不直接暴露 Worker 或 SQLite；本版本不称为公开多用户服务。
- 单 API worker：任何部署文件和验收都必须证明 Uvicorn 是 `--workers 1`。
- 镜像不可变：应用代码、Character、prompt 和依赖位于只读镜像；DB、RAG generations、日志、证书状态和备份均在镜像外。
- 配置与 secret 分离：非敏感配置可进入 `env_file`；secret 只以挂载文件注入，禁止进入镜像、Compose YAML、Git、日志、测试快照或 `docker inspect` 的 environment。
- 健康检查不消费 Provider：liveness/readiness 不调用 DeepSeek、SiliconFlow、embedding、LLM，也不发任何计费请求。
- 内容安全日志：不得记录 raw query、prompt、vector、Provider body、credential、Authorization、raw HTTP body、raw IP 或 raw exception body。
- 先备份、再升级；恢复和回滚必须基于 SQLite backup API 产物，绝不复制正在写入的数据库文件。

### 2.2 非目标

本轮不设计多 ECS、多 Uvicorn worker、共享分布式限流、高可用 SQLite、自动水平扩缩、PostgreSQL 迁移、完整身份系统、流式响应、Web UI、Audio、active vector RAG 或业务层重构。

## 3. 推荐进程与容器拓扑

```text
                            HTTPS :443 / HTTP :80
                                     |
                              Aliyun 安全组
                                     |
                 +-------------------v-------------------+
                 | proxy: Caddy                          |
                 | TLS / 80->443 / 128 KiB body limit    |
                 | 不保存 access body/header/raw IP      |
                 +-------------------+-------------------+
                                     | private compose network
                                     | X-Forwarded-* only from Caddy
                 +-------------------v-------------------+
                 | api: same immutable app image         |
                 | uvicorn app.api.server:create_app      |
                 | --factory --workers 1, :8000 internal  |
                 +-----------+---------------------------+
                             | SQLite + JSONL bind mounts
                 +-----------v---------------------------+
                 | /var/lib/aemeath/data                  |
                 |   aemeath.db                           |
                 | /var/log/aemeath                       |
                 +---------------------------------------+

     api ---------- HTTPS/DNS ----------> DeepSeek

     ops profile: same image, source DB read-only, /var/backups/aemeath rw；
                  /var/lib/aemeath/rag_indexes 由 generation tooling 独占 rw
     restore profile: 仅 maintenance 时启动，source backup ro，production data rw

     [首发禁用]
     memory-worker profile -> python -m app.memory.worker
       启用前必须通过 SQLite 双进程连接门禁 + job lease/stale recovery 门禁；
       通过后才共享 aemeath.db/log volume，并经 HTTPS 调 DeepSeek/SiliconFlow。
```

### 3.1 容器职责

| Service | 副本/进程 | 对外端口 | 职责 | 禁止事项 |
|---|---:|---|---|---|
| `proxy` | 1 | host `80/443` | TLS、HTTP->HTTPS、body 上限、可信转发头、反向代理 | 不做应用鉴权，不记录 body/header/token/raw IP |
| `api` | 1 container / 1 Uvicorn worker | compose-only `8000` | Session/API、业务调用、runtime guardrails、写 DB/JSONL | 不发布 host port，不启用 reload/fallback/mock |
| `memory-worker` | 首发 0；门禁后最多 1 container / 1 process | 无 | 未来的 repair/extraction job 消费 | 首发 profile 不启用；门禁前不得创建，不接 active vector RAG |
| `ops` | 按需、Compose profile | 无 | preflight、online backup、backup verify、generation tooling、只读统计 | 不常驻，不接受公网流量，不直接复制 live DB |
| `restore` | 按需、独立 Compose profile | 无 | writers 停止后执行受保护的原子恢复 | 不常驻、无网络、不得与 API/Worker 同时运行 |

### 3.2 单 worker 是硬约束

Session admission、token bucket、全局并发门禁和 Provider 并发门禁都是进程内状态。`api` 必须显式使用一个 Uvicorn worker；不得使用 Gunicorn 多 worker、Compose replicas、Kubernetes replicas 或 ECS SLB 后的多实例。重启会清空 rate bucket，因此其限流是早期滥用保护，不是持久化计费配额。

首发没有 Memory Worker，也不装配 SiliconFlow-backed Memory runtime。门禁后若启用，Worker 有自己的一组进程内 Provider gate，不会与 API 共享 DeepSeek/SiliconFlow 配额状态；届时需合并计算两个进程的 Provider 配额，embedding concurrency 仍固定为 `1`。

## 4. Docker 镜像设计

### 4.1 基础镜像

推荐多阶段构建，builder 与 runtime 都基于：

```text
python:3.10.20-slim-bookworm@sha256:<implementation-time-approved-digest>
```

理由：项目合同是 Python `>=3.10`；固定 3.10 patch 可以降低语言版本漂移，Debian bookworm slim 比完整镜像更小，且不把宿主 Ubuntu 的 Python 包带入容器。实施时必须解析并审批真实 digest，文档不伪造 digest，也不使用 `latest`。官方 Python 镜像说明 slim 变体只包含运行 Python 所需的最小 Debian 包，因此 builder 中若出现无 wheel 依赖，可临时安装编译包，但不得把编译器带入 runtime。[Python Official Image](https://hub.docker.com/_/python/)

### 4.2 构建与依赖

1. builder 只安装 `.[api]` 所需依赖并构建 wheel/虚拟环境；不安装 `dev`、`local-embedding`、Audio、CUDA 或 GPU 相关 extra。
2. runtime 只复制已构建产物、应用包、受版本控制的 Character/prompt 资源和必要证书。
3. 使用多阶段构建，最终镜像中不保留 pip cache、编译器、Git、测试缓存或源码构建临时目录。[Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
4. 实施阶段新增部署 lock artifact，例如 `requirements.deploy.lock`；其格式由 Manager 审批。lock 必须与 `pyproject.toml` 一致并在 CI 中校验。
5. `.dockerignore` 必须排除：`.env`、`.env.*` 中的本地私有文件、`.git/`、`.aemeath/`、`data/`、`source_doc/`、Audio 资产、test/cache、备份、secret 目录。允许进入镜像的 example 文件不得含 secret 值。

### 4.3 运行用户与只读根文件系统

- 应用镜像创建固定 UID/GID `10001:10001`，工作目录 `/opt/aemeath`，默认 `USER 10001:10001`。
- Compose 设置 `read_only: true`、`security_opt: [no-new-privileges:true]`、`cap_drop: [ALL]`，只给 data/logs bind mount 写权限，并以 tmpfs 提供 `/tmp`。
- Caddy 同样以非 root 用户运行；容器内部监听 `8080/8443`，宿主映射为 `80/443`。如 Caddy 自动 HTTPS 在该方式下不能通过 staging 验证，备选是仅增加 `NET_BIND_SERVICE` capability，仍不得使用完整 root capability 集。
- 官方运行规范中容器默认用户是 root，因此 Dockerfile/Compose 必须显式设置非 root 用户。[Docker container runtime](https://docs.docker.com/engine/containers/run/)

### 4.4 区域、时间与编码

镜像固定：

- `PYTHONUNBUFFERED=1`
- `PYTHONDONTWRITEBYTECODE=1`
- `PYTHONUTF8=1`
- `LANG=C.UTF-8`
- `LC_ALL=C.UTF-8`
- `TZ=UTC`

数据库、JSONL、backup manifest 统一记录 UTC；运维界面可按 Asia/Shanghai 展示。ECS 必须启用 NTP/chrony。镜像不安装完整 timezone 数据，除非实际依赖证明需要。

## 5. Compose 设计

### 5.1 网络与端口

- 独立 bridge network `aemeath_frontend`，预留一个与宿主网络不冲突的 subnet；Caddy 使用固定地址，例如 `172.30.0.10`。
- API 只 `expose: 8000`，没有 `ports`；首发不创建 Memory Worker，ops/restore 都没有监听端口。
- 仅 proxy 发布 `80:8080` 和 `443:8443`。
- Uvicorn 使用 `--proxy-headers --forwarded-allow-ips=172.30.0.10`；禁止 `*`，也不信任整个 RFC1918 网段。
- 若后续在 Caddy 前增加阿里云 SLB/CDN/WAF，必须重新设计可信代理链；当前 v0.1 假设 Caddy 是第一跳代理。

Uvicorn 官方配置允许通过 `--forwarded-allow-ips` 指定可信 IP/IP network；默认只信任 `127.0.0.1`。[Uvicorn Settings](https://www.uvicorn.org/settings/)

### 5.2 启动命令与重启策略

API 的目标命令：

```text
python -m uvicorn app.api.server:create_app \
  --factory --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips=172.30.0.10 \
  --timeout-graceful-shutdown=60 --timeout-keep-alive=5 \
  --no-access-log
```

Memory Worker 门禁通过后的目标命令（首发不执行）：

```text
python -m app.memory.worker
```

Compose 行为：

| Service | restart | init | stop_grace_period | health |
|---|---|---:|---:|---|
| proxy | `unless-stopped` | true | 30s | HTTPS/内部 upstream 状态，不调用 Provider |
| api | `unless-stopped` | true | 75s | 容器内请求 `/ready` |
| memory-worker | 首发不创建；门禁后 `unless-stopped` | true | 90s | 首发不适用；门禁后使用 process-liveness 与结构化 job 状态 |
| ops | `no` | true | 30s | 不适用 |
| restore | `no` | true | 30s | 不适用；运行前必须确认 writers 已停止 |

`init: true` 用于转发信号并回收子进程。API 的 Compose grace 比 Uvicorn graceful timeout 多 15 秒。Worker 的 90 秒只属于门禁后的设计，并须在专项 fault injection 中验证。

### 5.3 依赖与启动顺序

1. `api` 不依赖 proxy，先启动并通过 `/ready`。
2. `proxy` 依赖 API healthy 后开始接受封闭测试流量。
3. `ops` 只通过 `--profile ops run --rm` 调用，不随 `up -d` 常驻。
4. `restore` 只在 maintenance 中通过独立 profile 显式调用；任何 writer 仍运行时必须 fail-closed。不得给 restore 容器挂 Docker socket。
5. `memory-worker` 放入默认关闭的 `memory` profile；首发启动命令不得包含该 profile。门禁通过后，才允许其依赖 API 首次 schema initialize/upgrade 且 healthy 后启动，避免两进程同时首次建表。

Compose `depends_on` 只能协调容器启动，不替代运行期监控。API 宕机会使 Caddy 返回 502/503。首发不存在 Worker backlog；未来启用后，Worker 宕机不得使对话 API liveness 失败，但 extraction backlog 必须可见。

### 5.4 资源约束

staging 初始建议 2 vCPU / 4 GiB ECS，不启用 NVIDIA runtime/device。首发容量按 API+Caddy+宿主开销测量；未来启用 Worker 前重新做 API+Worker 峰值容量预算。至少预留 1 GiB 给内核、Docker、Caddy 和文件缓存，并设置磁盘告警。

## 6. 路径与 volume 对照表

宿主目录由 root/deploy automation 创建；应用可写目录为 `10001:10001`、建议 `0750`。任何改变 UID/GID 的升级都必须先做权限迁移预检。下表 Worker 权限均为**门禁通过后的未来权限**；首发没有 Worker container/mount。

| 逻辑数据 | 当前默认/入口 | 生产容器路径 | ECS 宿主路径/volume | API | Worker | Proxy | Ops | Restore | 备份策略 |
|---|---|---|---|---|---|---|---|---|---|
| 主 SQLite | `data/aemeath.db` / `AEMEATH_DB_PATH` | `/var/lib/aemeath/data/aemeath.db` | `/var/lib/aemeath/data` bind | rw | 首发 -；门禁后 rw | - | source ro | rw，仅 maintenance | nightly + pre-upgrade online backup |
| RAG generations | `data/rag_indexes` / `AEMEATH_RAG_INDEX_ROOT` | `/var/lib/aemeath/rag_indexes` | `/var/lib/aemeath/rag_indexes` 独立 bind | - | - | - | rw（generation tooling） | ro | generation 完成后按 manifest 归档；不把它接入 active provider |
| reserved memory vector DB | `data/aemeath_memory.sqlite` / `AEMEATH_MEMORY_VECTOR_DB_PATH` | `/var/lib/aemeath/data/aemeath_memory.sqlite` | 同一 data bind | 当前未使用 | 当前未使用 | - | ro | rw，仅 maintenance | 仅保留路径，不宣称已持久化 vector memory |
| App Log | `.aemeath/app_logs` / `AEMEATH_APP_LOG_ROOT` | `/var/log/aemeath/app` | `/var/log/aemeath` bind | rw | 首发 -；门禁后 rw | - | ro | - | 固定 30 天，宿主清理 |
| Trace Log | `.aemeath/traces` / `AEMEATH_TRACE_LOG_ROOT` | `/var/log/aemeath/trace` | 同一 log bind | rw | 首发 -；门禁后 rw | - | ro | - | 固定 14 天，宿主清理 |
| Prompt Log | `.aemeath/sessions` / `AEMEATH_PROMPT_RUN_LOG_ROOT` | `/var/log/aemeath/prompt` | 同一 log bind | rw，但 prod disabled | 不使用 | - | ro | - | v0.1 固定关闭，不提供 break-glass 开启路径 |
| SQLite backups | 无 | `/var/backups/aemeath` | `/var/backups/aemeath` bind | - | - | - | rw | ro | 本地保留 + 加密 off-ECS 副本 |
| Caddy cert/state | 无 | `/data`, `/config` | `caddy_data`, `caddy_config` named volumes | - | - | rw | - | - | 由 Caddy 管理；备份策略单独制定；首次启动前解决 non-root ownership |
| 临时文件 | 系统 temp | `/tmp` | tmpfs | rw | rw | rw | rw | rw | 容器退出即丢弃 |

Docker volume/bind mount 的数据生命周期独立于容器可写层，适合把状态放到镜像外。[Docker storage](https://docs.docker.com/engine/storage/)

### 6.1 路径校验

prod preflight 必须：

1. 拒绝相对 DB/log/RAG 路径。
2. 拒绝 DB、RAG、App/Trace/Prompt root 落在 `/opt/aemeath` 或容器 rootfs。
3. 对 data/logs/backups/RAG 目录检查 owner、mode、可用空间和与 service scope 对应的 ro/rw 权限；可写目标使用空 sentinel 创建、`fsync`、删除，不写业务内容。
4. 检查 DB 与 RAG generation 不含 symlink 逃逸；restore/prune 只允许经过解析后仍位于固定根目录的目标。
5. 确认 `.env` 未进入镜像、未挂载到容器，也不存在于 `/opt/aemeath`。

## 7. 配置与 secret 对照表

### 7.1 新增环境分层合同

新增 `AEMEATH_ENVIRONMENT=dev|test|prod`，默认 `dev` 只为保持本地兼容；部署清单必须显式写 `prod`。

| 能力 | dev | test | prod |
|---|---|---|---|
| dotenv | 本地开发可允许 | 禁止，测试显式注入 | 禁止加载；镜像内也不得存在 |
| mock LLM | 可显式启用 | 默认 mock，无外网 | 必须 false |
| API fallback | 可测试 | 可单测 | 必须 false，否则启动失败 |
| local default user | 可显式 opt-in | 测试 fixture 可用 | 必须 false，否则启动失败 |
| bearer token | 本地环境或 secret file | 测试临时值 | secret file 必需；只用于单用户封闭测试 |
| hash embedding | 单元测试可用 | 可用 | 必须 false |
| CORS | localhost 明确列表 | fixture | 固定空列表；任何非空值启动失败 |
| SQLite/logs | 本地路径 | 每测试 temp root | 固定绝对 volume 路径 |
| Prompt Log | 可由开发者显式启用 | 通常关闭 | 固定关闭，非本版本可变项 |
| Provider 网络 | 可 mock/真实 | 禁止真实 | 首发仅业务 DeepSeek；真实 smoke 仍需单独授权 |

### 7.2 非敏感配置

`/etc/aemeath/prod.env` 只保存下列非 secret 值；文件本身不进 Git，仓库只提供无值或安全默认的 example。

| 配置 | 生产建议 | 启动校验/备注 |
|---|---|---|
| `AEMEATH_ENVIRONMENT` | `prod` | 必填 |
| `AEMEATH_DB_BACKEND` | `sqlite_raw` | 拒绝 experimental backend |
| `AEMEATH_DB_PATH` | `/var/lib/aemeath/data/aemeath.db` | 必须绝对且位于 data root |
| `AEMEATH_RAG_INDEX_ROOT` | `/var/lib/aemeath/rag_indexes` | 只给 ops/generation tooling；API/Worker 不挂载且不激活 vector RAG |
| `AEMEATH_MEMORY_VECTOR_DB_PATH` | `/var/lib/aemeath/data/aemeath_memory.sqlite` | reserved，当前 runtime 不使用 |
| `AEMEATH_CHARACTER_CARD_PATH` | 镜像内批准的 manifest 路径 | 必须位于只读镜像且启动可读 |
| `AEMEATH_DEFAULT_CHARACTER_ID` | Manager 批准的稳定 ID | 必须与 manifest/runtime 一致 |
| Uvicorn `--host/--port/--workers` | `0.0.0.0` / `8000` / `1` | 这是 Compose command，不伪造当前不存在的 env 名；端口仅 compose network 可达 |
| `AEMEATH_API_ALLOW_LOCAL_DEFAULT_USER` | `false` | prod 为 true 直接失败 |
| `AEMEATH_DEFAULT_USER_ID`、`AEMEATH_DEV_AUTH_USER_ID` | Manager 批准的同一非默认 user ID | 名称沿用现实现；不得依赖 `user_default` |
| `AEMEATH_ENABLE_MODEL_FALLBACK`、`AEMEATH_API_ENABLE_MODEL_FALLBACK` | `false` | API flag 为硬禁令；通用 flag 也关闭以防误用 CLI 入口 |
| `AEMEATH_USE_MOCK_LLM` | `false` | prod 为 true 直接失败 |
| `AEMEATH_ENABLE_LONG_TERM_MEMORY` | `false` | Manager 已冻结；Compose 不启用 memory profile |
| `AEMEATH_EMBEDDING_PROFILE` | 首发不设置；门禁后 `siliconflow_pro_bge_m3_v1` | approved profile 固定 Provider/model/dimension 合同 |
| `AEMEATH_EMBEDDING_ALLOW_HASH` | `false` | prod 为 true 直接失败 |
| `AEMEATH_ENABLE_CONTEXT_BUDGET` | `true` | Manager 已冻结；保持既有 deterministic contract |
| `AEMEATH_ENABLE_APP_LOG` | `true` | 仅安全结构化字段 |
| `AEMEATH_ENABLE_TRACE_LOG` | `true` | 仅 safe summary，保留 14 天 |
| `AEMEATH_ENABLE_PROMPT_RUN_LOG`、`AEMEATH_API_ENABLE_PROMPT_RUN_LOG` | `false` | v0.1 固定 false；任一为 true 则启动失败 |
| `AEMEATH_APP_LOG_ROOT`、`AEMEATH_TRACE_LOG_ROOT`、`AEMEATH_PROMPT_RUN_LOG_ROOT` | `/var/log/aemeath/{app,trace,prompt}` | 必须位于 log volume |
| DeepSeek base URL；门禁后 SiliconFlow base URL | 现有官方 HTTPS endpoint | prod 拒绝明文 HTTP；首发不配置 SiliconFlow；不记录完整 URL query |
| timeout/concurrency/rate/body 业务限制 | 已批准 guardrail 值 | 部署不静默放宽；变更需独立审批 |
| `AEMEATH_API_CORS_ALLOWED_ORIGINS`（新增） | 空列表 | Manager 已冻结；prod 同时拒绝 wildcard、path、userinfo |

### 7.3 Secret 注入

| Secret | 宿主 source | 容器路径 | 消费者 | 新增 `_FILE` 环境名 |
|---|---|---|---|---|
| DeepSeek API key | `/etc/aemeath/secrets/deepseek_api_key` | `/run/secrets/deepseek_api_key` | 首发 API；门禁后也可给 Worker | `DEEPSEEK_API_KEY_FILE` |
| SiliconFlow API key | `/etc/aemeath/secrets/siliconflow_api_key` | `/run/secrets/siliconflow_api_key` | 首发不创建/不挂载；门禁后 API、Worker | `SILICONFLOW_API_KEY_FILE` |
| API bearer token | `/etc/aemeath/secrets/api_bearer_token` | `/run/secrets/api_bearer_token` | API | `AEMEATH_DEV_AUTH_TOKEN_FILE` |

规则：

1. Compose 负责把获准的 secret 文件按 service 挂载到 `/run/secrets/<name>`；`_FILE` **只是应用/部分镜像采用的约定，Compose 不会自动读取该变量并注入值**。Aemeath 必须自行实现 resolver。[Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
2. Compose 文件只允许写受审查的 secret 名称和固定 source 路径，不允许 `${SECRET_VALUE}`、`secrets.*.environment`、inline `content`、remote `include/extends` 或把 secret 放入普通 `environment/env_file`。
3. Standalone Compose secrets 通过文件挂载提供，不等于 Swarm 的加密 secret store。宿主 source 建议 `root:10001`、`0440`，父目录 `0750`；实施时验证 UID `10001` 可读但其他用户不可读。
4. Compose file-based secret 的 `uid/gid/mode` 在某些实现中不会覆盖 bind source 权限，因此不能只信 YAML，必须检查容器内权限但不得打印内容。
5. 每个 resolver 必须拒绝明文值与对应 `_FILE` 同时存在；prod 还禁止 legacy `OPENAI_API_KEY`。文件按 bytes 限长后用严格 UTF-8 解码，只移除**一个**结尾行终止符：若为 CRLF 移除一个 `\r\n`，否则只移除一个 `\n`；不得调用 `.strip()` 或继续裁剪空格/更多换行。随后拒绝空值、NUL 和残留的 CR/LF。
6. resolver 只把结果保存在进程内存；禁止写入 Settings repr、异常、日志、health/readiness 或 process environment。轮换以原子替换宿主文件并重启唯一消费进程完成，不输出旧值、新值、长度或 hash。
7. Docker Compose 把 Compose 文件视为可信可执行输入；`env_file`、`secrets/configs file:`、include/extends 等可读取宿主文件，内容可能在配置加载或 `docker compose config` 输出中出现。因此默认只运行 `docker compose config -q` 做验证；禁止保存未经脱敏的完整 config 输出，也禁止将其上传到 CI artifact/log。[Docker Compose trust model](https://docs.docker.com/compose/trust-model/)
8. WSL 与 staging 使用唯一的假 secret marker 做泄漏检查：扫描受控的 config 命令输出流、image/layer、`docker inspect` environment、容器 stdout、App/Trace/Prompt JSONL 和测试 artifact。扫描器只报告位置/category 与 pass/fail，不回显 marker；真实 secret 不用于该测试。

### 7.4 生产启动校验

新增独立 preflight，且 `create_app` 自身仍要 fail-fast，不能只依赖运维脚本。未来启用 Worker 时，它也必须执行同一 prod 合同。prod 至少拒绝：

- fallback/mock/hash embedding/local default user/wildcard CORS；
- 缺失的 bearer token、DeepSeek key；首发若出现 SiliconFlow secret mount 或启用 LTM 也应失败；
- 非 HTTPS Provider base URL；
- 相对或逃逸出 volume root 的 state/log 路径；
- Uvicorn worker 数不为 1、reload 开启或未限制可信代理；
- 任一 Prompt Log 开关为 true；
- DB backend 不是 `sqlite_raw`；
- image 中存在 `.env` 或部署挂载 `.env`。

preflight 输出只能显示配置名、是否满足和安全枚举值，不显示 secret、原始异常 body、完整网络响应或用户内容。

## 8. `/health`、`/ready` 与启动状态

### 8.1 `/health`：liveness

保留当前轻量语义：只证明 ASGI event loop 能响应，返回固定 `200` 与服务名。它不访问 DB、文件系统、DeepSeek、SiliconFlow、embedding 或任何付费 Provider。容器的 liveness 不应因上游 Provider 故障而重启 API。

### 8.2 `/ready`：readiness

新增 `GET /ready`，成功返回固定最小 JSON，例如 `status=ready`；失败返回 `503` 与固定 machine-readable reason enum，不包含路径、SQL、异常文本或 secret。FastAPI route 只依赖窄的 `ReadinessPort`，不得读取 `RuntimeResources._state`、其他私有字段，也不得直接 import/use raw SQLite 实现。

建议的窄合同：

```text
ReadinessPort.check(deadline) -> ReadinessResult
ReadinessResult = {ready: bool, reason: stable enum}

内部组合：
  LifecycleReadinessState: starting | ready | closing
  DatabaseReadinessPort.check_readable(deadline)
  CachedFilesystemReadinessProbe.check(deadline)
```

生命周期由 lifespan 显式维护：

1. 构造 app 时状态为 `starting`；Settings、Character/Prompt 和 startup preflight 失败时应用启动失败，不进入 ready。
2. lifespan 的 startup 阶段完成 runtime 装配和 adapters 初始化后，公开方法 `mark_ready()` 将状态切为 `ready`。
3. lifespan 退出路径先调用 `mark_closing()`，使新 `/ready` 立即返回 `503/closing`，再调用同步 `application_runtime.close()`。
4. 状态机是 readiness 自己的公开合同，不复用或窥探 `RuntimeResources` 私有关闭状态，也不新增 `ApplicationRuntime.aclose()`。

数据库与文件系统检查：

1. `DatabaseReadinessPort` 由 store/runtime 组装层注入；首发 `sqlite_raw` adapter 使用短超时、只读连接验证 DB 可读及关键 schema/table 存在，不迁移、不写数据。FastAPI route 不知道 backend 类型；未来 SQLAlchemy adapter 也实现同一 port。
2. 每次 `/ready` 的总 deadline 建议 `500 ms`，DB 子检查建议不超过 `200 ms`；超时统一为 `503/check_timeout`，不得让 healthcheck 堆积。
3. owner、mode、symlink、sentinel write/`fsync`/delete 全部放在 startup preflight，只执行一次；失败则应用不 ready/启动失败。
4. 运行期磁盘只做无写入的 `statvfs`/路径存在性检查，并以例如 `5 s` TTL 缓存和 single-flight 防抖，避免每个 probe 都访问磁盘；阈值结果只返回固定 `filesystem_unavailable`。
5. readiness 本身不写盘，不做 schema initialize，不返回表名、路径、空间数字或业务计数。

明确禁止 readiness：

- 调 DeepSeek/SiliconFlow；
- 生成 embedding 或执行 semantic query；
- 发起真实对话/记忆抽取；
- 执行 SQLite schema 修改、repair 或 backup；
- 返回详细异常、磁盘路径、版本 secret 或业务计数。

`/health` 与 `/ready` 可作为无需 Bearer 的低敏探针，但只返回固定无敏感字段并设置 `Cache-Control: no-store`；这不改变服务仅用于封闭测试的定位。如只让 proxy/healthcheck 访问 `/ready`，应在 Caddy 侧按来源限制，不改变其无 Provider 语义。

### 8.3 Worker 健康边界（首发禁用）

首发不创建 Worker，因此 API readiness 不包含 Worker、SiliconFlow、memory job 或 backlog 检查。未来通过门禁后也不为 Worker 增加 HTTP server：Docker 负责 process-liveness；结构化 `worker_started`、`worker_stopping`、`worker_stopped`、job success/failure 和 repair failure 提供可见性。不能用“进程还活着”替代 lease/backlog 健康。

## 9. 反向代理、CORS 与 HTTP 安全

### 9.1 HTTPS

- Caddy 为唯一公网入口，自动申请/续期证书；HTTP 永久跳转 HTTPS。
- 阿里云安全组只开放 `80/443`；`22` 只允许批准的管理网段。不得开放 `8000`、SQLite、Docker daemon 或 Worker 端口。
- 必须先有 Manager 提供的域名/DNS A/AAAA 策略。没有域名时不得把“自动 HTTPS 已完成”作为验收通过。
- Caddy 镜像按 version + digest 固定；证书 state 存在独立 volume。

### 9.2 原始 HTTP body 上限

在 Caddy 转发到 ASGI **之前**限制 raw request body，建议初值 `128 KiB`，超限立即 `413`。该值高于当前 message/metadata 的解析后业务上限，给 JSON Unicode escape、字段名和 envelope 留有余量，同时阻止任意大 body 进入 FastAPI parser。必须用边界测试证明：最大合法请求的 escaped/unescaped 形式不会被误拒，超限 chunked 与 Content-Length 请求都会被拒绝。

Caddy 的 `request_body max_size` 可在超限时返回 `413`，该 directive 在当前文档中标记为实验性。Manager 已明确接受这一风险；实施必须固定已验证的 Caddy version+digest，并通过 128 KiB 边界、chunked、Content-Length 和重启回归，不再把取消 body limit 作为备选。[Caddy request_body](https://caddyserver.com/docs/caddyfile/directives/request_body)

API 端口不发布是必要的旁路防护。应用层仍保留已实现的 message char/UTF-8 byte/metadata byte 限制；proxy raw limit 不能替代业务校验。

### 9.3 可信代理与 `X-Forwarded-For`

1. Caddy 是第一跳时，默认忽略客户端伪造的 forwarded 值并生成自己的 `X-Forwarded-*`；不得在前面悄悄加入其他 proxy。
2. Uvicorn 只信任固定 Caddy IP；其他来源的 `X-Forwarded-For` 不得影响 `request.client.host` 和 rate-limit key。
3. staging 必测“客户端伪造 XFF”与“多个 XFF 值”，确认不会绕过 IP bucket。
4. 若未来接入 SLB/CDN/WAF，只能信任明确的 proxy CIDR，并需要 Caddy `trusted_proxies`/`trusted_proxies_strict` 重新评审。[Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)

### 9.4 CORS

当前没有 Web UI，Manager 已冻结 prod CORS allowlist 为空；prod preflight 必须拒绝非空值。未来若单独批准前端 origin，才可按以下合同新增/开启 FastAPI CORS middleware：

- 精确 HTTPS `scheme://host[:port]` 列表；拒绝 `*`、path、query、userinfo 和模糊 suffix；
- methods 仅 `GET, POST, OPTIONS`；headers 仅 `Authorization, Content-Type, X-Request-ID`；
- expose `X-Request-ID, Retry-After`；
- `allow_credentials=false`，因为当前是显式 Bearer header 而不是 cookie auth；
- localhost 只可出现在 dev profile。

带 Authorization 的跨域请求应使用明确 origin，而不是 wildcard。[FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/)

### 9.5 Proxy 日志

v0.1 默认不启用 Caddy access log，避免记录 raw IP、query string 或意外 header。Caddy operational log 只送 stdout，级别与字段在 staging 用敏感标记验证；不得记录 Authorization、Cookie、body、完整 query 或 Provider secret。Docker stdout 使用 `local` logging driver 轮转。

## 10. 优雅关闭与进程生命周期

### 10.1 API

1. Compose/Docker 发送 `SIGTERM`。
2. Uvicorn 停止接受新连接，在最多 60 秒内等待 in-flight 请求。
3. FastAPI lifespan 先将 readiness 状态切为 `closing`，再调用当前已存在的同步 `application_runtime.close()`，关闭 Provider client/资源。
4. 75 秒后仍未退出才允许 Docker `SIGKILL`；该事件必须被监控为异常。

`ApplicationRuntime` 当前只公开同步 `close()`，本任务不得引入 `aclose()` 或第二套异步生命周期。业务 timeout 应小于 graceful window。部署不更改 `DialogueController` 或 Provider fallback；外层 timeout 仍负责稳定错误映射。

### 10.2 Memory Worker（门禁后的未来合同）

首发 `AEMEATH_ENABLE_LONG_TERM_MEMORY=false` 且不创建 Worker。以下只作为两个专项门禁全部通过后的未来生命周期合同，不属于 API-only 首发实施：

1. 为 `SIGTERM`/`SIGINT` 设置 stop event；收到信号后不再 claim 新 job。
2. 若当前没有 job，立即关闭资源并退出 0。
3. 若正在执行 job，允许当前有界 Provider/DB 操作结束，然后写安全状态、关闭资源并退出；不得把原始 response/exception 写日志。
4. 90 秒超过后 Docker 可能强杀；lease/stale recovery 必须保证 job 可被安全回收，不能依赖人工 SQL 或单纯重启。
5. memory profile 未显式启用时不创建 service；不能让一个“正常退出 0”的 disabled worker 被 `restart: unless-stopped` 反复拉起。

## 11. SQLite 备份、升级、恢复与回滚

### 11.1 一致性要求

禁止对正在写入的 `aemeath.db` 执行 `cp`、`tar`、`rsync`、宿主磁盘文件级复制或只复制主文件而忽略潜在 journal/WAL sidecar。统一使用 Python `sqlite3.Connection.backup()` 从 live source 生成新数据库；SQLite backup API 可在其他客户端访问源数据库时工作。[Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)

ECS 磁盘 snapshot 只作为整机灾备层，不能替代应用一致性 backup。建议顺序是：先生成并验证 SQLite backup，再让 offsite/snapshot 系统复制该静态产物。

### 11.2 WAL-aware 恢复合同

首发继续 `sqlite_raw`，当前实现也没有显式启用 WAL；但 backup/restore 工具不得假设永远只有一个主文件。未来启用 WAL 或 Memory Worker 前，恢复合同必须同时覆盖 `aemeath.db`、`aemeath.db-wal` 和 `aemeath.db-shm`：

1. 进入 maintenance 并停止所有 writer；首发是 API，未来是先 Worker 后 API。确认容器退出且没有已知进程持有 DB 文件。
2. 读取实际 journal mode，不以文件是否存在推断。如果是 WAL，使用受控短命 RW 连接执行 `PRAGMA wal_checkpoint(TRUNCATE)`；要求 checkpoint 结果 `busy=0`，然后关闭连接。busy/nonzero 或 I/O 错误必须中止恢复。
3. restore journal 记录 `prepared -> old_quarantined -> candidate_installed -> verified` 阶段，使用 `fsync` 保证可恢复；记录路径 basename、存在性、大小和 hash，不含 DB 内容。
4. 将旧主库和任何现存 `-wal`/`-shm` **作为同一 quarantine set** 移出 active basename 并生成 manifest。文件系统没有多文件原子 rename，因而 restore 必须在无 writer 的 maintenance 中按状态机可重入执行，不能声称三文件原子切换。
5. 安装由 `Connection.backup()` 产生并已验证的单一候选主库前，确认 active `aemeath.db-wal`/`aemeath.db-shm` 不存在；绝不把旧 sidecar 与新主库组合，也不从 backup 目录复制 sidecar。
6. 候选主库原子 rename 为 `aemeath.db`、设置权限并 `fsync` 父目录。应用启动后由已批准的连接策略创建全新的 sidecar；启动前后都要验证旧 sidecar 未被复用。
7. 回滚时停止所有 writer，把失败候选及其新 sidecar 作为新的 quarantine set 移走，再恢复原 quarantine set 或更推荐的配对 pre-upgrade backup；仍按相同 checkpoint/sidecar 规则执行。

故障注入至少覆盖：非空 WAL、残留旧 `-wal/-shm`、主库与 WAL 不匹配、checkpoint busy、sidecar 权限错误，以及在每个 restore journal 阶段模拟进程崩溃后安全重入。任何测试都只在 staging 数据副本上执行。

### 11.3 Online backup 产物

计划新增 `ops` profile 中的 `backup_sqlite.py`：

1. source DB 以只读 URI 打开；目标先写入 `/var/backups/aemeath/.partial/<unique>.sqlite`，目标必须不存在。
2. 使用 `Connection.backup()` 分页复制，并设置有限重试/进度；进度日志只包含页数、耗时和结果，不含业务数据。
3. 对目标运行 `PRAGMA quick_check`，定期或 pre-upgrade 运行完整 `integrity_check`。
4. 验证预期 schema/table 与 migration 版本；记录内容安全的 row-count 摘要。
5. `fsync` 文件和父目录，生成 SHA-256 与 JSON manifest，再在同一文件系统原子 rename 到最终名字。
6. manifest 至少包含 UTC 时间、source DB size、backup hash、schema/migration version、应用 Git SHA、镜像 digest、校验结果和安全计数；不含行内容、用户名、message、memory、token 或路径外 secret。
7. 失败时保留有后缀的 diagnostic partial 或安全删除，由实现测试决定；绝不覆盖既有成功 backup。

频率固定为 nightly + 每次升级前；暂定 RPO 24 小时、RTO 2 小时。本地初值保留 7 daily/4 weekly，另复制加密副本到 ECS 之外。OSS bucket、KMS 和责任人仍需在实施前落实。

### 11.4 RAG generation 与日志

- RAG generation 是目录树，不是 live 主 DB。只有 generation 已 `ready`、无构建进程持锁并有 manifest/hash 时才归档；恢复后重新校验 active pointer 与 hash。它仍不接入在线 Evidence。
- JSONL 日志默认不进入业务备份；如为审计保留，必须加密、限期、受权限控制，Prompt Log 仍默认禁用。

### 11.5 首次部署

1. 验证 ECS OS/arch、磁盘、NTP、Docker/Compose 版本、域名/DNS、安全组和出站 DNS/443。
2. 创建 `/var/lib/aemeath/data`、`/var/lib/aemeath/rag_indexes`、`/var/log/aemeath`、`/var/backups/aemeath`、`/etc/aemeath/secrets`，设置 owner/mode。
3. 写入 non-secret prod env 和 secret source files；运行权限检查，不输出内容。
4. 按批准 digest 拉取 app/Caddy 镜像；默认用 `docker compose config -q` 检查结构。若为人工审计生成完整 resolved config，必须在受控输出流内先做 unique-marker 脱敏检查，禁止保存未经脱敏的结果。
5. 运行 prod preflight；确认 WorldInfo active、API worker count 为 1、LTM=false、Context Budget=true、CORS empty、危险开关关闭且 memory profile 未启用。
6. 启动 API，等待 `/ready`，再启动 proxy；首发不创建 Memory Worker。
7. 执行不计费 health/auth/permission smoke；真实 Provider smoke 需 Manager 单独授权。

### 11.6 升级

1. 在 WSL/CI 构建测试后发布 immutable image digest，记录 Git SHA、lock hash 和 Compose/Caddy config hash。
2. 在现版本仍运行时执行并验证 pre-upgrade online backup；把 manifest 复制到 off-ECS 目标。
3. 进入 maintenance：proxy 停止接收新请求或返回维护响应；等待 API in-flight 完成。
4. 首发只需优雅停止 API；未来 Worker 通过门禁后，顺序固定为先 Worker、后 API。
5. 对 pre-upgrade backup 的临时副本运行候选镜像 preflight/schema initialize，验证不触碰生产 DB、不调用 Provider。
6. 启动候选 API，确认 `/ready`，最后恢复 proxy；首发不创建 Worker。
7. 执行 auth、Session、持久化、body/CORS/forwarded header smoke。真实 chat/embedding 只在授权时执行最小调用。
8. 保存 deployment record：时间、操作者、旧/新 image digest、backup manifest、测试结果和回滚截止条件；不保存 secret/业务内容。

### 11.7 恢复

1. 宣布 maintenance，停止 proxy 流量；首发优雅停止 API。未来启用 Worker 后先停 Worker 再停 API，确认没有 writer。
2. 在只读位置验证所选 backup 的 manifest/hash、`integrity_check`、schema/migration 和基本安全计数。
3. 确认所有 writer 已停止后才启动无网络的 restore profile；按第 11.2 节完成 checkpoint、restore journal、旧主库与 sidecar quarantine。
4. 把 backup 恢复到同一 data 文件系统的临时候选路径，设置 owner/mode，重新校验；确认 active sidecar 不存在后，把候选主库原子 rename 为 `aemeath.db`。不得就地覆盖或删除旧 quarantine set。
5. 只启动 API，确认 `/ready`、schema、预期 session/account/job counts；这一步不调用 Provider。
6. 首发直接启动 proxy；未来 Worker 门禁通过后，才在 API ready 之后启动 Worker 并确认 lease/claim 状态。
7. 执行批准的人工 smoke。若失败，停止 writers，原子换回 quarantine DB，恢复原 image/config。

恢复验证至少每季度在 staging 做一次；“存在 backup 文件”不等于可恢复。

### 11.8 回滚

- app-only 且 DB schema 完全兼容：停止新版本，启动上一批准 image digest，复用当前 DB。
- 任何 schema/data migration：停止所有 writer，恢复与旧镜像配对的 pre-upgrade backup，再启动旧 image digest；不尝试未设计的 down migration。
- 配置回滚使用已存档的 non-secret config hash/version；secret 只按轮换 runbook 恢复或重新注入，不进入 deployment archive。
- 若恢复/回滚仍失败，保持 proxy maintenance，保留当前 DB、quarantine DB、backup 和安全诊断，不反复自动启动 Worker。

## 12. Memory Worker 启用门禁与未来运维

首发硬配置为 `AEMEATH_ENABLE_LONG_TERM_MEMORY=false`，Compose 默认不启用 `memory` profile。Recent conversation、Session、Character、Context Budget 和 `WorldInfoEvidenceProvider` 继续正常工作；没有 Memory extraction/recall、SiliconFlow 请求或 Worker backlog。以下两个门禁必须**同时完成专项设计、实现、自动化测试、双进程 staging 测试并经 Manager acceptance**，之后才可启用 Worker。

### 12.1 门禁 A：SQLite 双 backend 连接合同

专项范围必须同时覆盖 `sqlite_raw` 与 SQLAlchemy connection layer，即使首发生产仍固定 `sqlite_raw`：

1. 明确 DB 级 journal mode 的 owner 和初始化时序，避免 API/Worker/不同 backend 在启动时互相切换；若批准 WAL，两种 backend 必须观察到同一模式。
2. raw `sqlite3` 与 SQLAlchemy 每条连接都要有一致、可配置且有上限的 `busy_timeout` 合同；不得无限阻塞，也不得只在某一 backend 设置。
3. 验证 API 与 Worker 同时写、claim/complete/fail、backup、checkpoint、优雅停止与异常中断；要求无 silent loss、无重复完成、事务状态一致，锁等待/失败映射稳定且内容安全。
4. WAL 模式必须通过第 11.2 节 sidecar backup/restore/rollback 故障注入；checkpoint busy 或 sidecar 不匹配必须 fail-closed。
5. 该专项不得顺手切换生产 backend、引入 PostgreSQL/Alembic 或改变 Memory/Dialogue 业务策略。

### 12.2 门禁 B：job lease/stale recovery

不能以人工 SQL、重启容器或“进程还活着”作为 `running` job 恢复机制。专项合同至少需要：

1. 原子 claim 同时生成不可猜测的 lease/fencing token 和过期时间；旧 worker 在 lease 转移后不得完成或失败新 owner 的 job。
2. 长任务有受限续租；只有 lease 明确过期的 `running` job 可被原子 reclaim，未过期 job 不可抢占。
3. success/failure/reclaim 操作幂等，并有 attempt 上限与稳定终态；所有日志/状态只使用 safe ID、attempt、age bucket、canonical error，不含任务 body。
4. 启动 repair 与周期 repair 使用同一 lease 规则，不另设绕过路径；恢复动作可审计，不允许操作员直接改表。
5. 自动化与 staging 必须覆盖 SIGTERM、SIGKILL、OOM/host reboot、Provider timeout、DB lock、续租竞争、旧 owner 迟到写入和重复恢复。

在两个门禁通过前，preflight 必须拒绝 `AEMEATH_ENABLE_LONG_TERM_MEMORY=true`、拒绝启用 memory profile，并确认 API 容器没有 SiliconFlow secret mount。

### 12.3 结构化可见性（门禁后）

复用 `SafeAppLogger`，新增内容安全事件：

- `memory_worker_started`：版本、poll interval、安全枚举配置；
- `memory_worker_stopping` / `memory_worker_stopped`：signal enum、是否有 in-flight job、耗时；
- 现有 job claimed/succeeded/failed 事件继续只含 job/session/request ID 与 safe error code；
- repair 成功/失败只含数量、状态和 canonical error category；
- 不写 prompt、memory body、query、embedding、Provider body、credential、raw exception 或 raw IP。

stdout 使用同样的 JSON 事件或极少生命周期消息，交给 Docker `local` driver；不得把 JSONL body 再完整复制到 stdout。

### 12.4 最小运维动作（门禁后）

允许的日常动作：

```text
docker compose ps
docker compose logs --since <duration> memory-worker
docker compose restart memory-worker
docker compose --profile ops run --rm ops <safe-job-status-command>
```

safe job status 只显示 pending/running/failed 数量、最老 job age、最后成功时间和 canonical error category，不显示输入/输出 body。重启 Worker 前先判断是否存在 running job；不得通过手工 SQL 随意改状态，也不得把重启当作恢复。stale job 只能经第 12.2 节的 lease-aware、fenced、可审计应用路径恢复，不提供独立 SQL 修复脚本。

### 12.5 失败语义（门禁后）

- Provider timeout/429/5xx：Worker 进程应继续存活，job 记录安全失败状态，日志可见 canonical error；不得切 fallback。
- DB locked：job 内错误按现有安全失败路径记录；若 repair/claim 外层抛出存储错误，则进程应记录稳定 category 后非零退出，由 Compose 拉起。不得静默无限重试；进一步 retry 合同取决于 SQLite 审批项。
- API down：Worker 不应依赖 API HTTP，仍可消费 DB；但首次 schema 初始化顺序只用于部署启动。
- Worker down：API 继续处理对话，memory extraction backlog 可见；不得把 Worker 不健康误报为 API liveness 失败。
- disk full/read-only：写失败必须可见且不泄露 body；API readiness 应转为 503，运维停止流量并扩容/恢复磁盘。

## 13. JSONL 与容器日志增长策略

### 13.1 应用 JSONL

当前 logger 每次进程启动创建新文件，但没有按大小切分。v0.1 使用宿主机 `systemd timer` 调用受限 prune 脚本：

| 类别 | prod 默认 | 建议保留 | 额外限制 |
|---|---:|---:|---|
| App Log | on | 30 天 | 总量上限待容量测试 |
| Trace Log | on | 14 天 | 总量上限低于 App Log |
| Prompt Log | off | 不保留 | v0.1 禁止开启；新版本如需开启必须重新设计审批 |

prune 脚本必须解析固定根目录、拒绝 symlink/路径逃逸、跳过过新的 active 文件、先输出 dry-run 安全统计，再按 age/总量删除。磁盘使用率建议在 70% warning、85% critical、95% emergency；emergency 不能通过删除 SQLite 或 backup 自救。

### 13.2 Docker stdout/stderr

Compose 使用 Docker `local` logging driver，例如 `max-size=10m`、`max-file=5` 的等价支持配置，并在当前 Docker 版本验证。Docker 推荐 `local` driver，因为它默认执行轮转并使用更高效格式。[Docker local logging driver](https://docs.docker.com/engine/logging/drivers/local/)

不得在 stdout 开启 Uvicorn access log，或需配置成不含 raw IP、query/header/body 的安全格式。Caddy access log 默认关闭。

## 14. Provider 出站网络与无 GPU 要求

### 14.1 网络

首发只允许 API 经 DNS + TCP 443 出站访问配置的 DeepSeek HTTPS endpoint；不向 API 挂载 SiliconFlow secret，也不启动 Worker。两个 Memory 门禁通过后，才允许 API/Worker 访问 SiliconFlow HTTPS endpoint。Provider IP 不固定，因为 CDN 地址可能变化；系统 CA 必须校验证书，生产拒绝关闭 TLS verification。

建议初期安全组/NACL 允许必要 DNS 与 443 出站；如安全团队要求域名级 egress，需增加受管 egress proxy 并单独评审证书、日志与单点故障。入站 Provider webhook 不在范围内。

### 14.2 Timeout 与失败

- 首发使用现有 DeepSeek client timeout，并保留 API 外层 guardrail timeout；不要在 proxy 设置比应用 timeout 更短且导致模糊 502 的超时。
- Worker/SiliconFlow timeout 与两个进程合并 Provider quota 属于门禁后容量验收。
- DNS failure、connect timeout、read timeout、429、5xx、TLS failure 都必须映射到稳定安全错误；不得输出 Provider raw body，不得启用生产 fallback。

### 14.3 无 GPU

镜像不安装 `[local-embedding]`、Torch、Transformers、CUDA、NVIDIA container runtime，不挂载 `/dev/nvidia*`。首发不调用 BGE-M3；门禁后仍只允许通过 SiliconFlow API。active Evidence 始终是 keyword-based WorldInfo。验收要检查镜像包列表和 Compose device 配置，而不只是“运行时未检测到 GPU”。

## 15. WSL2 Ubuntu 22.04 构建与 Compose smoke

所有本地部署验收在 WSL2 Linux 文件系统中的干净 checkout 或 bind 性能可接受的位置执行，目标平台显式为 `linux/amd64`。不复制或读取 Windows 本地 `.env`。

### 15.1 前置核对

1. `uname -m` 为 `x86_64`，Ubuntu 为 22.04。
2. 记录 Docker Engine、Buildx、Compose plugin 版本；使用与 ECS 兼容的批准版本。
3. 核对 branch/commit 和干净 checkout；Manager 已有的其他工作区修改不得进入 build context。
4. 使用 mock/test secret files 和临时 bind roots；不使用真实 Provider key。

### 15.2 自动化流程

1. Python lint/compile/full pytest；Windows 已知 temp 权限问题不适用于 WSL，但仍使用独立 writable `--basetemp`。
2. 生成/校验 deployment lock，构建 `--platform linux/amd64` 镜像。
3. 检查 image config：非 root UID、无 secret env、无 `.env`、无 data/log/backup、无 GPU 依赖、entrypoint/labels 正确。
4. `docker compose config -q` 校验结构，并以受控 unique-marker scanner 检查 resolved output stream；验证 API workers=1、只有 proxy published ports、只读 rootfs、volume/secret scope，且 `memory-worker` 带非默认 `memory` profile。默认 `up -d` 后再验证实际容器中没有 Worker。禁止保存未经脱敏的完整 config 输出。
5. 以 mock profile `up -d`，等待 API `/health` 与 `/ready`；不产生真实网络请求，并证明 LTM=false、Context Budget=true、CORS empty。
6. 创建测试 session/chat，确认 DB/JSONL 只写入宿主临时目录；容器可写层不增长业务状态。
7. `restart`、`down/up` 后确认 session/DB 持久；替换容器后数据仍在。
8. 并发写入期间执行 online backup，验证 backup integrity；在另一临时 data root 恢复并核对安全计数。
9. 执行 oversized Content-Length/chunked body、伪造 XFF、CORS allow/deny、缺失/错误 token、read-only rootfs、路径权限测试。
10. 向 API 发送 SIGTERM，验证 readiness 先进入 closing、同步 `application_runtime.close()` 被调用、drain 时间和 exit code。Worker SIGTERM/SIGKILL/lease 测试只属于第 12 节专项门禁。

### 15.3 本地人工 smoke

- 浏览器/HTTP client 访问 proxy 的 `/health`、`/ready`；API host port 不可访问。
- 未认证 `/v1/*` 得到统一 401；有效测试 bearer 可创建/读取自己的 session；用户隔离不变。
- 请求 body 低于边界可达应用，高于边界在 proxy 返回 413。
- 客户端伪造 XFF 不改变 trusted client identity；`Retry-After` 仍可见。
- CORS 空列表不返回跨域允许头；批准 localhost origin 只在 dev 生效。
- 分别使用 user-content marker 与 fake-secret marker：前者只能关联到安全 request ID/状态而不能出现 body，后者在 config/image/inspect/log/artifact 中必须完全不存在；两者都不得回显 token/IP。
- 默认 Compose service 列表和实际容器中都没有 Memory Worker，API 容器也没有 SiliconFlow secret；Recent conversation、Session、Character、Context Budget、WorldInfo 路径仍正常。

## 16. 阿里云 ECS staging 验收与故障注入

### 16.1 主机前置条件

- Ubuntu 22.04 LTS x86_64，启用安全更新、NTP、最小管理员 SSH；关闭密码登录/直接 root 登录按平台规范执行。
- 通过 Docker 官方 apt repository 安装受支持的 Docker Engine/Compose；官方兼容表包含 Ubuntu Jammy 22.04 x86_64。[Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- 安全组：公网只开 80/443；22 仅批准来源；不开放 Docker daemon/API/Worker/DB。
- 已批准 domain/DNS、磁盘类型/容量、备份目标、监控和告警接收人。
- 注意 Docker published ports 与宿主防火墙规则的交互；验收以阿里云安全组 + 实际外部端口扫描为准，而不是只看 UFW。

### 16.2 staging 验收

1. 部署与 WSL 测试完全相同的 image digest、lock 和配置模板。
2. 执行 prod preflight，确认无 `.env`、无危险开关、secret file 权限正确、WorldInfo active、LTM=false、Context Budget=true、CORS empty、memory profile/Worker/SiliconFlow mount 均不存在。
3. TLS 证书、HTTP redirect、协议/header、body 上限、端口暴露、CORS、XFF 欺骗和 bearer auth 通过。
4. 验证 `docker restart`、Compose `down/up`、应用镜像替换、ECS reboot 后 SQLite、RAG generations、日志和 Caddy state 均持久。
5. 在并发安全测试负载下做 online backup；在独立 staging restore root 执行完整恢复，再切换验证。
6. Manager 单独授权后才执行一次最小 DeepSeek chat；SiliconFlow embedding/Memory job 只能在第 12 节门禁环境中另行授权，不能作为首发默认 smoke。
7. 连续运行观察 DB lock、内存、CPU、磁盘和日志增长；记录 P50/P95/P99 只含耗时/状态，不含内容。首发没有 Worker backlog。

### 16.3 故障注入

每项都在 staging 临时数据副本或可恢复快照上进行：

| 故障 | 注入方式 | 期望 |
|---|---|---|
| DeepSeek timeout | test proxy/防火墙短时阻断批准目标 | 稳定 timeout，API 存活，无 fallback/泄露；真实请求仍需授权 |
| Provider 429/5xx | stub 或批准的故障代理，不向真实 Provider 制造滥用 | canonical safe error、Retry/失败可见 |
| API SIGTERM during chat | `docker stop` | 最长 60s drain，readiness closing、同步 runtime close，随后重启 ready |
| Worker idle/mid-job SIGTERM/SIGKILL（门禁后） | controlled leased job | stop claim、fencing/reclaim/幂等符合第12节，不留下永久 running job |
| SQLite write contention（门禁后） | API + Worker、raw + SQLAlchemy 组合测试 | busy timeout 有界、无 silent loss/重复完成，两 backend 合同一致 |
| WAL restore sidecar（门禁后及 restore 工具） | 非空/残留/错配 WAL、checkpoint busy、分阶段 crash | fail-closed、旧 sidecar 不复用、restore journal 可安全重入 |
| data/log permission revoked | 对 staging bind root 改 mode | readiness 503/安全错误；恢复权限后可恢复 |
| disk near-full/full | 限额 loop device/独立测试卷 | 失败可见、不损坏唯一 DB、告警触发 |
| backup corrupt/truncated | 修改 backup 副本 | hash/integrity 阶段拒绝，绝不覆盖 prod DB |
| proxy bypass/XFF/body | 从外部和 compose 网络测试 | host 8000 关闭、伪造头无效、超限 413 |
| Compose secret trust | unique fake marker + 受控 config/image/log 扫描 | marker 不进入已保存 config、image、inspect env、日志或 artifact |
| ECS reboot | 控制台重启 | 服务按策略恢复，state 持久，单 worker 不变 |

禁止故意损坏唯一生产/唯一 staging 数据。故障注入前必须有已验证 backup 和回滚点。

## 17. 安全边界、已知风险与 Deferred

### 17.1 安全边界

- 当前鉴权是静态 bearer -> 固定 user 映射，不是完整账号系统；Manager 仅为单用户封闭测试临时接受，必须 HTTPS、secret file 注入、最小权限和定期轮换，不能描述为公开多用户服务。
- Caddy 是唯一外部信任边界；API 只信任固定 Caddy IP。
- 首发 API 使用非 root、只读 rootfs、drop capabilities；只有指定 state/log volume 可写。未来 Worker 也必须满足同一合同。
- 首发只有 API 获得 DeepSeek key 与 bearer token；不挂载 SiliconFlow key。proxy/ops/restore 不获得 Provider key；未来 Worker 不获得 API bearer token。
- health/readiness/backup verification 均不调用付费 Provider。
- 日志不记录 raw content、credential、raw body、raw IP 或 raw exception body。

### 17.2 已知风险

| 风险 | 影响 | v0.1 缓解 | 是否需 Manager 决策 |
|---|---|---|---|
| 单 ECS/单 API 进程 | 主机/进程故障即中断 | restart、备份、RPO24h/RTO2h、监控、恢复 runbook | Manager 已接受封闭测试无 HA |
| SQLite API+Worker 双进程写竞争 | `database is locked`、延迟/失败 | 首发关闭 LTM/Worker；第12.1节为启用硬门禁 | 不阻塞 API；阻塞 Worker |
| Worker 强杀留下 running job | backlog 永久卡住/重复处理 | 首发不启动 Worker；第12.2节 lease/fencing/recovery 为硬门禁 | 不允许人工 SQL/仅重启替代 |
| 静态 bearer | 轮换和撤销能力弱 | secret file、TLS、单 user、轮换重启 | Manager 仅接受封闭测试 |
| 进程内 rate limit 重启清零 | 不能充当持久配额 | proxy/body limit/监控；明确边界 | 接受或增加外部网关（Deferred） |
| 两进程 Provider 配额不共享 | 门禁后可能超过供应商并发/速率 | 首发无 Worker/SiliconFlow；门禁后合并测算 | Worker 启用前决定配额 |
| 无 lockfile | 日后镜像不可复现 | 新增 deploy lock、image digest | 是：lock 格式 |
| Caddy body-limit directive 实验性 | 版本行为变化 | pin version+digest、128 KiB 边界/故障测试 | Manager 已明确接受风险 |
| JSONL 无内建 rotation | 磁盘增长 | host timer；App30天/Trace14天/Prompt关闭；disk alert | 保留期已决，容量阈值待实测 |
| Prompt Log 高敏 | prompt 泄露 | 两个 prod Prompt Log flag 均强制 false | Manager 已冻结关闭 |
| 恢复从未演练 | backup 不可用 | staging restore drill 作为上线门 | 必须通过 |

### 17.3 Deferred

- 多实例 API、分布式 admission/rate limit/concurrency、HA proxy/DB。
- PostgreSQL/Alembic 与在线迁移。
- 完整账号、OAuth/OIDC、权限管理、Web UI。
- active vector RAG、Memory vector DB 启用或业务检索策略变化。
- Audio/GPT-SoVITS、GPU、本地 embedding。
- Streaming/WebSocket/SSE。
- Worker HTTP health、自动扩容、通用 retry engine、dead-letter queue。
- 外部集中日志/APM/SIEM；若将来接入，仍必须遵守 content-safe 字段合同。
- CDN/SLB/WAF 与多级 trusted proxy；若启用需独立设计。

## 18. Manager 已冻结决策与剩余输入

### 18.1 已冻结，不再重开

| 项目 | Manager 结论 |
|---|---|
| 上线类型 | 单用户、封闭测试；不得称为公开多用户服务 |
| OS/平台 | Ubuntu 22.04 LTS x86_64 / 阿里云 ECS |
| 拓扑 | Docker Compose、单 API worker |
| 代理 | Caddy，实施时固定 version + digest |
| raw body | `128 KiB`，必须测试；明确接受 Caddy experimental 风险 |
| 数据库 | 首发 `sqlite_raw` |
| Long-term Memory | `false`；Compose 不启动 Worker，双门禁通过后另行审批启用 |
| Context Budget | `AEMEATH_ENABLE_CONTEXT_BUDGET=true` |
| CORS | 空列表 |
| Auth | 静态 Bearer，只限单用户封闭测试 |
| 日志 | App 30 天、Trace 14 天、Prompt Log 关闭 |
| backup | nightly + 每次升级前；暂定 RPO 24 小时、RTO 2 小时 |
| 真实 Provider smoke | DeepSeek/SiliconFlow 均需单独授权；SiliconFlow 还受 Memory 门禁约束 |

### 18.2 实施前仍需提供或审批

1. Manager re-review 已通过；可以按第 21 节分阶段实施，但 D5 仍需两个专项设计分别获批。
2. 封闭测试域名、DNS、证书责任人及 Caddy 是否确实为第一跳；若存在 SLB/WAF，必须重画 trusted proxy 链。
3. Caddy、Python base image 和 app image 的确切 version/digest，以及 deployment lock 格式与升级责任人。
4. ECS 规格、系统盘/数据盘容量、off-ECS/OSS/KMS backup 目标、磁盘/服务告警接收人和恢复操作者；RPO/RTO 已冻结但实现责任仍需落实。
5. 静态 bearer 对应的非默认 user ID、轮换周期与紧急吊销责任人。
6. DeepSeek 最小真实 smoke 的单次授权；SiliconFlow 授权只能在第 12 节门禁完成后请求。
7. ECS 使用 Docker 官方 apt repository 还是阿里云批准包源，以及宿主加固基线。
8. Memory Worker 的两个专项门禁需分别提交设计与验收证据；它们是强制条件，不再提供“接受人工 SQL 风险”的选项。

## 19. 拟新增或修改的文件

以下是**审批后的实施计划**，本轮未创建或修改这些文件。

### 19.1 新增

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 多阶段、non-root、只含 `.[api]` runtime |
| `.dockerignore` | 排除 secret、state、logs、RAG/source/audio/cache |
| `compose.yaml` | proxy/API、默认关闭的 memory profile、ops/restore profiles、single worker、volumes/secrets/health |
| `deploy/Caddyfile` | TLS、redirect、body limit、reverse proxy、安全日志 |
| `deploy/env/dev.env.example` | 无 secret 的 dev Compose 示例 |
| `deploy/env/prod.env.example` | 无 secret 的 prod 配置名称/安全默认示例 |
| `requirements.deploy.lock`（名称待批） | 可复现 runtime 依赖 |
| `app/runtime/readiness.py`（名称待批） | 公共 lifecycle 状态机、`ReadinessPort`/result，不读取 runtime 私有字段 |
| `app/db/readiness.py`（名称待批） | backend-specific、只读、短超时 DB readiness adapters |
| `scripts/deployment/preflight.py` | prod config/image/path/permission 校验 |
| `scripts/deployment/backup_sqlite.py` | SQLite online backup + manifest |
| `scripts/deployment/verify_sqlite_backup.py` | hash/integrity/schema/安全计数校验 |
| `scripts/deployment/restore_sqlite.py` | writer 停止后的 journaled restore、主库安装与 sidecar quarantine |
| `scripts/deployment/prune_jsonl.py` | dry-run、固定根、age/size retention |
| `scripts/deployment/check_secret_marker.py` | 只输出 category/pass/fail 的受控 unique-marker 泄漏扫描 |
| `deploy/systemd/aemeath-log-prune.service` | 宿主受限日志清理任务 |
| `deploy/systemd/aemeath-log-prune.timer` | 周期调度 |
| `docs/deployment.md` | 操作者部署/升级/备份/恢复/回滚 runbook |
| `tests/api/test_readiness.py` | `/health`/`/ready` 边界与无 Provider 调用 |
| `tests/runtime/test_readiness_lifecycle.py` | starting/ready/closing、总 deadline、缓存/single-flight、同步 close 顺序 |
| `tests/api/test_cors_config.py` | prod origin 校验与 header 行为 |
| `tests/test_production_config.py` | prod fail-fast、`_FILE` 精确换行语义、双来源拒绝、路径校验 |
| `tests/memory/test_worker_shutdown.py`（门禁后） | SIGTERM stop-claim/drain/lease/reclaim/fencing |
| `tests/deployment/test_sqlite_backup.py` | live backup/integrity/no-overwrite/WAL-sidecar restore/crash-reentry |
| `tests/deployment/test_secret_marker.py` | unique fake marker 跨 config/image/inspect/log/artifact 泄漏检查 |
| `tests/deployment/test_log_prune.py` | dry-run/path escape/retention |

### 19.2 修改

| 文件 | 计划改动 | 边界 |
|---|---|---|
| `app/config.py` | environment mode、`_FILE` secret resolver、prod validation、绝对路径/CORS | 不改变业务默认，prod 才加 fail-fast |
| `app/embeddings/factory.py`（门禁后） | SiliconFlow key 支持 `_FILE` | 首发不挂 SiliconFlow；不改变 provider/profile/fallback 语义 |
| `app/api/server.py` | CORS 与 readiness wiring；closing 后调用现有同步 `application_runtime.close()` | 不新增 `aclose()`，不改 controller/chat policy |
| `app/api/routes/health.py` | 增加 `/ready`，保留 `/health` liveness | 不调用 Provider |
| `app/api/schemas.py` | 最小 readiness response enum | 不暴露内部细节 |
| `app/memory/worker.py`（门禁后） | SIGTERM、结构化生命周期及经批准的 lease/stale recovery | 首发不改/不启动；业务策略变化需独立接口审批 |
| `.env.example` | 只补配置名和安全说明 | 绝不写 secret 值；不读取本地 `.env` |
| `docs/fastapi_service.md` | 部署命令、single-worker、health/ready/proxy/CORS | 文档同步 |
| `docs/observability.md` | 宿主 retention 与 proxy/stdout 安全边界 | 不放宽内容安全合同 |
| `docs/persistence.md` | online backup/restore 与 SQLite 两进程验收合同 | 不引入 PostgreSQL/Alembic |

### 19.3 明确不修改

- `DialogueController`、Character、Memory、RAG 业务策略和 active provider wiring；
- `docs/task_log.md`：由 Manager 在验收后更新，本 Worker 不标 done；
- `docs/codex_cross_session_messaging.md`：存在 Manager 既有修改，实施时也不得修改、回滚或提交；
- 本地 `.env`：不读取、不修改、不复制、不纳入 build context。

## 20. 自动化测试与人工 smoke 验收矩阵

| 层级 | 自动化 | 人工/环境 | 通过标准 |
|---|---|---|---|
| 配置 | prod dangerous flags、missing/ambiguous secret、精确 `_FILE` newline、路径/CORS | `config -q` + 受控 marker scan | 不安全组合 fail-fast；LTM=false/context=true/CORS empty；无 secret 输出 |
| 镜像 | build/inspect/package scan | WSL image shell 只读检查 | UID非 root、无 `.env`/state/GPU/build tools、digest 固定 |
| Compose | `compose config -q` contract + marker stream scan | 默认 `up` 后 inspection | 只有 proxy 发布80/443；API workers=1；memory profile未启用、无Worker/SiliconFlow mount；rootfs ro |
| API health | route/unit adapter integration | proxy/容器 probes | 显式 starting/ready/closing；不读私有字段/raw SQLite；有界缓存；Provider call count=0 |
| HTTP ingress | body/CORS/XFF/auth tests | 外部 curl/browser | 超限413、XFF不可伪造、CORS精确、401稳定 |
| Guardrails | 现有完整回归 | staging 并发 smoke | 既有 506 基线不回退；限流/timeout/Retry-After 不变 |
| Worker（门禁后） | raw/SQLAlchemy contention + lease/fencing tests | SIGKILL/reboot/backlog observation | 两门禁全部通过前 profile 不可启用；无永久 running job |
| Persistence | concurrent backup/verify/restore/WAL-sidecar tests | staging restore/crash-reentry drill | live backup一致、旧sidecar不复用、损坏/忙状态拒绝、恢复计数一致 |
| Restart | Compose integration | container/host reboot | DB/RAG/log/cert state 均持久；rate bucket重置有记录 |
| Logging | unique marker leak scan | JSONL/stdout/Caddy inspection | marker body/token/IP不存在，safe ID/error code存在 |
| Provider | mock failure matrix | 经批准最小 DeepSeek；SiliconFlow 门禁后另授权 | TLS/timeout/model正确，无fallback，无GPU |
| Fault injection | scripted controlled faults | ECS staging | 无 silent data loss；失败可见；rollback 实际成功 |

实施完成时必须重新运行完整测试；不能把本设计阶段记录的 `506 passed` 当作实施后回归证据。

## 21. 实施阶段任务拆分与验收标准

### D0 — Manager 设计审批

- 输入：本设计、第 18.2 节剩余输入、ECS/domain/backup 前置条件。
- 当前：`Approved`。D1–D4、D6–D8 可按阶段实施；D5 仍受第 12 节两个专项设计审批约束。

### D1 — 可复现 non-root 镜像

- 实施：Dockerfile、dockerignore、deploy lock、image metadata。
- 验收：WSL `linux/amd64` build；non-root/read-only；无 secret/state/GPU/build tools；依赖/full tests 通过。

### D2 — Prod 配置与 secret 合同

- 实施：environment mode、应用 `_FILE` resolver、fail-fast、example/preflight、unique-marker scanner。
- 验收：精确单换行、明文+file 双来源、Compose trust model 测试通过；未脱敏 config 不保存；日志/异常不泄露。

### D3 — API ingress readiness

- 实施：窄 readiness port/adapters/lifecycle、空 CORS、Uvicorn/proxy trust、128 KiB body limit。
- 验收：starting/ready/closing 明确；不读 private state/raw SQLite；500ms 有界/磁盘缓存；同步 close；零 Provider 调用；ingress 矩阵通过。

### D4 — Compose/Caddy/volume

- 实施：两个常驻 service（proxy/API）、默认关闭的 memory profile、ops/restore one-shot profiles、固定 network、TLS、只读 rootfs、volume/secret scope、logging driver。
- 验收：默认 `up` 不创建 Worker；无 SiliconFlow mount；restart/down-up/state persistence；Caddy fixed digest/128KiB 测试通过。

### D5 — Memory Worker 双门禁（不在首发 API critical path）

- 实施前置：第 12.1/12.2 节分别完成专项设计并另获 Manager 批准。
- 验收：raw/SQLAlchemy WAL+busy_timeout 双进程测试和 lease/fencing/stale recovery 全通过；不接受人工 SQL/仅重启；之后才可启用 memory profile。

### D6 — 备份、验证、恢复、回滚

- 实施：backup/verify/restore scripts、manifest、runbook、权限和 offsite hook。
- 验收：并发写时 online backup；损坏/checkpoint busy 拒绝；主库+sidecar quarantine；旧 sidecar 不复用；crash-reentry 与配对回滚成功。

### D7 — 日志保留与宿主运维

- 实施：Docker local driver、prune script/systemd timer、disk alerts 文档。
- 验收：dry-run、安全路径、active file、age/size、低磁盘故障测试；Prompt Log prod false。

### D8 — WSL full gate 与 ECS staging gate

- 实施：第 15、16、20 节完整矩阵和 evidence bundle。
- 验收：完整 pytest（不得少于新基线）、WSL smoke、ECS reboot persistence、backup/restore、API-only fault injection 全部通过；真实 Provider smoke 无授权可明确 skip，不得伪装为通过；Manager 做最终 acceptance。

每个任务的交付报告必须列：变更文件、实际命令/测试、pass/fail/skip、剩余风险和是否需要 Manager 决策。Focused validation 不等于最终 acceptance。

## 22. 上线门槛与回报格式

只有以下条件全部满足才可称为 **API-only、单用户封闭测试** Deployment Ready v0.1：

1. 本设计保持 Manager Approved，并在对应实施阶段补齐第 18.2 节所需输入；
2. 相同 immutable image digest 通过 WSL 和 ECS staging；
3. Uvicorn 单 worker、WorldInfo active、LTM=false、Context Budget=true、CORS empty、无 Worker/SiliconFlow mount、prod fallback/mock/default-local/hash 禁用有自动化证据；
4. TLS、trusted proxy、body limit、CORS、auth、只读/non-root/secret scope 通过；
5. API starting/ready/closing、同步 `application_runtime.close()` 和安全日志通过 fault injection；
6. concurrent online backup、WAL-aware sidecar restore、crash-reentry、paired rollback 在 staging 实际成功；
7. ECS reboot 后持久化通过，App30/Trace14/Prompt关闭、RPO24h/RTO2h 的运维责任明确；
8. 完整回归通过，Manager 执行最终 acceptance 并更新任务状态。

SQLite 双进程与 stale running job 不阻塞 API-only 首发，因为 Worker 被硬禁用；它们继续阻塞任何 Memory Worker/SiliconFlow-backed Memory 上线。

本 Worker 完成本设计后只应回报：文档路径、核对基线、设计结论、blocking decisions、实际验证和“等待审批”。不得宣称 Docker/部署已实施，不得修改 `docs/task_log.md` 或自行标记 done。

## 23. 参考资料

- [Uvicorn Settings](https://www.uvicorn.org/settings/)
- [Docker Compose services reference](https://docs.docker.com/reference/compose-file/services/)
- [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [Docker Compose trust model](https://docs.docker.com/compose/trust-model/)
- [Docker Compose variable interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
- [docker compose config](https://docs.docker.com/reference/cli/docker/compose/config/)
- [Docker storage](https://docs.docker.com/engine/storage/)
- [Docker local logging driver](https://docs.docker.com/engine/logging/drivers/local/)
- [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Python Official Image](https://hub.docker.com/_/python/)
- [Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
- [Caddy request_body](https://caddyserver.com/docs/caddyfile/directives/request_body)
- [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- [FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/)

---

**当前状态：Approved / Implementation Authorized。尚未实施，未标记 done。**
