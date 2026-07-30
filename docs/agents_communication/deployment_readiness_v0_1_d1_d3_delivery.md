# T-0140 Deployment Readiness v0.1 — D1–D3 阶段交付

日期：2026-07-29；Changes Requested 修订：2026-07-30

基线：`checkpoint/semantic-query-20260728` / `5b1fe0b`

状态：**D1–D3 accepted by Manager；T-0140 remains pending**

## 1. 本阶段结果

本阶段只实施已批准的 D1、D2、D3。没有实施 D4–D8，没有启动或修改 Memory Worker
双进程门禁，没有修改 DialogueController、Memory/RAG/Character 业务策略，也没有改变 active
`WorldInfoEvidenceProvider`。

### D1 — 可复现 non-root 镜像

- 新增两阶段 `Dockerfile`，两个 stage 均固定 Python `3.10.20-slim-bookworm` 的
  linux/amd64 manifest digest。
- 新增带 artifact hash 的 `requirements.deploy.lock`；覆盖 `uvicorn[standard]` 的
  `httptools`、`uvloop`、`watchfiles`、`websockets`，Docker 强制 only-binary 安装；最终
  venv 移除 pip/setuptools，不引入 GPU 或编译工具。
- 最终进程固定 UID/GID `10001:10001`、UTC、UTF-8、无 bytecode、无 Uvicorn access log，
  容器端口仅声明 8000。
- `.dockerignore` 使用 allowlist，只把 `app/`、部署启动脚本、Dockerfile 和 lock 送入 build
  context；末尾再次 deny `.env*`、DB、JSONL、`.aemeath`、cache 和 key/certificate 类型，
  防止未来 `!app/**` 重新纳入本地状态或凭据。
- `/opt/aemeath` 为 immutable application root；生产 preflight 会拒绝可写 rootfs。DB 和日志
  mount point 位于 `/var/lib/aemeath`、`/var/log/aemeath`。
- `scripts/deployment/start_api.py` 固定 `workers=1`、`reload=False`、60 秒 graceful timeout。

### D2 — Prod 配置、`_FILE` Secret 与 preflight

- `Settings` 新增 `dev|test|prod` 环境模式。仅 dev 可以加载 dotenv；test/prod 不加载，且
  prod 必须由进程环境显式声明。
- DeepSeek 与静态 Bearer 支持 `DEEPSEEK_API_KEY_FILE`、
  `AEMEATH_DEV_AUTH_TOKEN_FILE`。resolver：
  - 拒绝明文和 `_FILE` 双来源；prod 拒绝明文及 legacy `OPENAI_API_KEY`；
  - bytes 上限后 strict UTF-8 解码；只删除一个 LF 或一个 CRLF；
  - 保留空格，拒绝空值、NUL、残余 CR/LF；
  - 不在异常中输出值；两个 Settings secret 字段已从 dataclass repr 隐藏。
  - prod 只接受 `/run/secrets/deepseek_api_key` 与 `/run/secrets/api_bearer_token`；preflight
    拒绝任一父路径 symlink，要求 runtime `10001:10001`、secret `root:10001`/`0440`。
- dotenv 不得把初始 dev 切换为 test 或 prod；test/prod 均不能通过 dotenv 激活。
- prod fail-fast 固定 API-only profile：`sqlite_raw`、真实 DeepSeek HTTPS、fallback/mock/local
  default/hash/embedding/LTM 关闭、Context Budget 开启、CORS 空、Prompt Log 关闭、App/Trace
  开启、同一非默认 user ID、固定 state/log 路径。
- `scripts/deployment/preflight.py` 在 runtime 打开 DB 前检查：non-root、只读 application
  root、resource 可读性、`.env` 不存在、data/log owner/mode/access、sentinel
  write/fsync/delete、DB 非 symlink、secret file 权限、无 SiliconFlow、唯一固定 Caddy IP。
  `create_app()` 自身调用相同 preflight，不能只靠运维脚本。
- 新增 `scripts/deployment/check_secret_marker.py`；扫描受控 artifact 时只输出 category 与
  pass/fail，不输出 marker、长度或 hash。

### D3 — Readiness、CORS、trusted proxy 与 raw body limit

- 新增窄合同：
  - `ReadinessPort.check(deadline) -> ReadinessResult`
  - `DatabaseReadinessPort.check_readable(deadline) -> bool`
  - `FilesystemReadinessPort.check(deadline) -> bool`
- lifespan 显式维护 `starting -> ready -> closing`；退出先标记 closing，再调用现有同步
  `ApplicationRuntime.close()`，没有新增 `aclose()`。
- SQLite adapter 使用短超时、只读连接和关键 schema table 检查；`sqlite_raw` 与既有 dev/test
  SQLAlchemy-on-SQLite 使用各自 schema marker，prod 仍只允许 `sqlite_raw`。FastAPI route
  不 import raw SQLite，也不读取 `RuntimeResources._state` 等私有字段。runtime 构造后的
  wiring 异常会同步关闭已创建资源。
- `/ready` 默认总 budget 500 ms、DB budget 200 ms；filesystem 检查不写盘，默认 5 秒 TTL
  且 single-flight。失败只返回稳定 reason enum。`/health` 保持轻量 liveness；两者均
  `Cache-Control: no-store`，均不调用 Provider。
- prod CORS 固定空列表。dev/test 仅精确 origin；无 credentials，methods 仅
  `GET, POST, OPTIONS`，headers/expose headers 为批准的窄集合。
- Uvicorn 只信任一个固定 Caddy IP，拒绝 wildcard、CIDR 和多个 IP；未受信任来源的 XFF
  不改变 `request.client.host`。
- 固定 Caddy `2.10.2-alpine` linux/amd64 digest；`request_body max_size 128KiB` 位于
  reverse proxy 前；不启用 access log。Caddy 作为第一跳时使用其内建的 forwarded-header
  防伪行为。

## 2. 文件交付

新增：

- `Dockerfile`、`.dockerignore`、`requirements.deploy.lock`
- `deploy/Caddyfile`、`deploy/caddy-image.lock`
- `deploy/env/dev.env.example`、`deploy/env/prod.env.example`
- `app/deployment/{secrets,preflight,marker,uvicorn_config}.py`
- `app/runtime/readiness.py`、`app/db/readiness.py`
- `scripts/deployment/{start_api,preflight,check_secret_marker,check_deploy_lock}.py`
- `docs/deployment.md`
- `tests/api/test_{readiness,cors_config}.py`
- `tests/runtime/test_readiness_lifecycle.py`
- `tests/db/test_sqlite_readiness.py`
- `tests/deployment/test_{production_config,preflight_and_marker,ingress_contract,
  caddy_body_limit_integration,deploy_lock_contract,docker_context_contract}.py`

修改：

- `app/config.py`
- `app/api/server.py`
- `app/api/dependencies.py`
- `app/api/routes/health.py`
- `app/api/schemas.py`
- `.env.example`
- `docs/fastapi_service.md`

没有修改 Manager 既存的 `docs/codex_cross_session_messaging.md`、`docs/task_log.md` 或
Approved 设计文档；它们在开始本阶段前已是现有工作树变更/未跟踪文件。本 Worker 没有把
T-0140 标记为 done，也没有读取或修改本地 `.env`。

## 3. 验证证据

所有 Python 测试命令均显式设置 `AEMEATH_ENVIRONMENT=test`，避免加载本地 dotenv。

| 检查 | 结果 |
|---|---|
| 完整 pytest（本机未配置 Caddy binary） | **584 passed, 1 skipped**，30.4 s；唯一 skip 为 Caddy integration |
| `python -m ruff check .` | passed |
| `python -m compileall -q app scripts tests` | passed |
| `git diff --check` | passed；仅显示 Windows checkout 的既有 LF/CRLF 提示 |
| lock/pyproject 一致性 CLI | passed；direct/API PEP 440 版本约束与 `uvicorn[standard]` 覆盖，全部 pinned/hashed |
| linux/amd64 `requirements.deploy.lock` only-binary hash download | passed，26 artifacts |
| Caddy `fmt`/`validate`（2026-07-29 阶段证据） | passed / valid configuration |
| Caddy raw body integration（2026-07-29 阶段证据） | Content-Length/chunked：128 KiB -> 204，128 KiB+1 -> 413 |
| Caddy spoofed multi-value XFF（2026-07-29 阶段证据） | upstream 只收到实际 first-hop client `127.0.0.1` |
| Uvicorn trusted proxy unit contract | 未受信任 source 的伪造 XFF 不改变 client IP |
| Secret fixed-path/owner/group/mode/newline/dual-source/repr/marker tests | passed |
| readiness lifecycle/SQLAlchemy/cleanup/deadline/cache/single-flight/zero-Provider | passed |

完整 pytest 共收集 585 项，仍有基线已有的 FastAPI TestClient、legacy embedding 配置及
Python 3.13 SQLite datetime adapter deprecation warnings；没有新增 test failure。本次修订主机
未配置 `AEMEATH_TEST_CADDY_BINARY`，所以 Caddy integration 跳过；上表保留并明确标注
2026-07-29 已执行的固定 2.10.2 binary 证据，没有把该旧证据冒充本次执行。

## 4. 当前环境未完成的验收

当前 Windows 主机只有 Python 3.13；`wsl --list --verbose` 只有 stopped
`docker-desktop`，没有用户要求的 Ubuntu 22.04 distro，且 Docker Desktop Linux daemon
socket 不存在。因此本轮不能伪称以下环境验收已完成：

- WSL2 Ubuntu 22.04 `docker build --platform linux/amd64`；
- 构建后 image inspect/package scan；
- 容器内 UID、read-only rootfs、tmpfs、bind-mount permission 和 preflight smoke。

已用 pip target selector 验证 CPython 3.10 linux/amd64 的所有锁定 wheel/hash可下载，并用
官方 Caddy 2.10.2 Windows binary 执行真实 ingress 测试。这不能替代上述 Linux image
验收；应由 Manager 在具备 Ubuntu 22.04/Docker daemon 的下一验收环境执行。

## 5. 安全边界、风险与 Deferred

- Caddy `request_body` 仍是官方标记 experimental 的能力；本轮只在固定 2.10.2 上验证，
  升级必须重复 boundary/chunked/restart 测试。
- D4 尚未实施，因此没有 Compose service/network/volume/secret wiring；当前交付不能作为
  完整 ECS 上线包。固定 Caddy IP 必须由 D4 网络拓扑给出，留空会 fail-fast。
- production readiness 默认最小空闲空间为 64 MiB；这是启动安全阈值，不替代 D7 磁盘告警。
- 静态 Bearer 仅限封闭单用户测试，不宣称公开多用户安全。
- DeepSeek 真实 smoke 未执行，继续需要单独授权；SiliconFlow secret/profile 被 prod
  preflight 明确拒绝。
- D5 Memory Worker、WAL/busy-timeout 和 lease/stale recovery 未修改、未测试、未启用；
  仍需两个专项设计审批。
- D4 Compose、D6 backup/restore、D7 retention/monitoring、D8 WSL/ECS staging 全部 deferred。

## 6. Changes Requested 修复对照

- High / lock：补齐 `uvicorn[standard]` 四个 Linux wheel 与实际 hash，Docker 增加
  `--only-binary=:all:`，新增可独立运行的 lock/pyproject 一致性检查及漏包负向测试。
- Medium / lock constraint：使用 `packaging` PEP 440 parser 验证所有 runtime/API 直接依赖
  的锁定版本；报告新增 `constraint_mismatches`，并覆盖 NumPy 与 FastAPI 不兼容约束负向测试。
- Linux/Python 3.10 阻断修复：lock 新增
  `exceptiongroup==1.3.1 ; python_version < "3.11"` 及已验证 wheel hash，lock checker 将其列为
  Python 3.10 必需包并有缺失负向测试；`app/utils/ids.py` 与 `app/schemas.py` 使用
  Python 3.10 可用的 `timezone.utc`，不再引用 Python 3.11 才提供的 `datetime.UTC`。
- Medium / SQLAlchemy：同一窄 SQLite readiness port 支持既有 dev/test SQLAlchemy backend，
  并按 backend 检查 schema marker；新增 API 启动回归与 post-build wiring 异常资源关闭测试。
- Medium / Secret：Settings 和 preflight 均固定两个批准路径；preflight 检查全部父路径、
  runtime `10001:10001`、secret `root:10001`/`0440`，并拒绝 effective runtime write access。
- Low / build context：allowlist 后增加 terminal deny rules，覆盖 `.env*`、SQLite、JSONL、
  `.aemeath`、cache、`app/data`、`app/rag_indexes` 和 key/certificate；新增静态 context 合同测试。
- 建议回归：dotenv 不得把 dev 切换为 test/prod；独立 preflight CLI 将
  `EmbeddingConfigurationError` 等非 `ValueError` 配置失败映射为固定
  `configuration_invalid`，不打印 traceback 或原异常。
- Linux gate：最初交付时该门禁尚未完成；2026-07-30 Manager 已在 Ubuntu 22.04 WSL2 和
  Docker Desktop Linux/amd64 上完成正式仓库镜像复验，结果见第 8 节。

## 7. 请求 Manager 阶段复验

请重点复验：

1. prod frozen flags、`_FILE` source tracking 与 preflight fail-closed 是否符合批准合同；
2. readiness public port、生命周期和同步 close 顺序；
3. Caddy first-hop XFF 与 128 KiB experimental risk 的实现；
4. 在 Ubuntu 22.04/Docker daemon 可用环境补做 D1 image build/read-only smoke；
5. 验收后决定是否授权进入 D4。D5 继续保持独立审批，不随 D4 自动解锁。

## 8. Manager 最终验收

2026-07-30，Manager 使用未施加临时补丁的当前仓库执行正式复验：

- `pytest -q`：584 passed，1 skipped；Ruff、compileall、lock checker 和
  `git diff --check` 通过；
- `docker build --platform linux/amd64 --no-cache` 成功，Python 3.10 的完整 hash lock 安装
  成功；
- non-root UID/GID、只读 rootfs、`/tmp` tmpfs、数据/日志挂载、Secret `root:10001/0440`、
  production preflight、健康/就绪/鉴权、镜像与日志 Secret 扫描全部通过；
- SIGTERM 在约 620 ms 内完成，退出码 0，未发生 OOM；未调用真实 Provider，未读取项目
  `.env`。

D1–D3 正式接受，D4 可以开始。T-0140 继续为 `pending`；D5 仍受独立门禁约束。
