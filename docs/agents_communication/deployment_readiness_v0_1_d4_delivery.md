# T-0140 Deployment Readiness v0.1 — D4 阶段交付

日期：2026-07-30

基线：`checkpoint/semantic-query-20260728` / `9294364`

状态：**D4 accepted by Manager；T-0140 remains pending**

## 1. 本阶段边界与结果

本阶段只实施已授权的 D4 Compose/Caddy/volume。默认部署现在由一个 API container 和一个
Caddy container 组成；API 仍只有一个 Uvicorn application worker，Long-term Memory 仍关闭。
没有实施 D5 Memory Worker 的 WAL/busy-timeout 或 lease/stale recovery 门禁，没有实施 D6
backup/restore 功能、D7 retention/monitoring 或 D8 ECS staging 自动化，也没有改变
DialogueController、Memory/RAG/Character 业务逻辑和 active `WorldInfoEvidenceProvider`。

## 2. D4 实现

### 2.1 默认拓扑与网络

- 新增 `compose.yaml`，默认 service 精确为 `api`、`proxy`。
- `api` 只 `expose: 8000`，不发布宿主端口；`proxy` 是唯一 ingress，发布
  `80:8080`、`443:8443`。
- 独立 bridge `aemeath_frontend` 固定 `172.30.0.0/24`；Caddy 固定
  `172.30.0.10`，API 只信任该单一代理 IP。
- 两个 service 均使用 `read_only: true`、`init: true`、`cap_drop: ALL`、
  `no-new-privileges`、bounded `/tmp` tmpfs、Docker `local` log driver（10 MiB x 5）和显式
  graceful-stop 时间。API 保持 `10001:10001`、75 秒 stop grace。
- Caddyfile 内部 HTTP/HTTPS 端口固定为 8080/8443，只启用 h1/h2；128 KiB body limit 和
  first-hop XFF 合同保持在 reverse proxy 前，不发布 UDP 或宣告 HTTP/3。

### 2.2 Caddy non-root 镜像

实机 smoke 发现官方固定 Caddy 镜像没有名为 `caddy` 的 passwd 用户，而且
`/usr/bin/caddy` 带 `cap_net_bind_service=ep`。直接配置 `user: caddy` 无法启动；即使改成数值
UID，带 file capability 的 binary 在 `cap_drop: ALL` + `no-new-privileges` 下也会被内核拒绝
执行。

因此新增最小 `deploy/Caddy.Dockerfile`：

- `FROM` 仍是 Manager 批准的 Caddy 2.10.2 linux/amd64 version + digest；
- 创建固定 UID/GID `10002:10002`；
- 移除高端口运行不需要的 low-port file capability；
- 只预置 `/data`、`/config` 为该 UID 所有，并固定最终 `USER 10002:10002`。
- Dockerfile-specific ignore 排除全部 build context；派生镜像不复制仓库文件。

Compose 构建 `aemeath-caddy:2.10.2-nonroot` 后运行该数值身份。新 named volume 首次创建会继承
镜像内 ownership；实机已验证 non-root Caddy 能写 `/data` 与 `/config`，同时只读 rootfs 和
drop-all-capabilities 成立。

### 2.3 Volume 与 Secret scope

- API 仅以 rw bind mount 获取 `/var/lib/aemeath/data`、`/var/log/aemeath`；没有 RAG、backup
  或 SiliconFlow mount。
- Caddyfile 是只读 bind；TLS/data/config 使用 `aemeath_caddy_data`、
  `aemeath_caddy_config` named volumes。
- DeepSeek key 与 closed-test Bearer token 只从固定宿主文件挂入
  `/run/secrets/{deepseek_api_key,api_bearer_token}`；容器环境只有 `_FILE` 路径，没有 secret
  明文。
- `ops`、`restore` profile 只预留已批准的最小 mount shape。D6 尚未实施，所以两者运行
  `profile_guard.py` 并以 78 fail-closed，不提供 backup/restore 命令。
- `memory` profile 同样只运行无网络、无 mount、无 secret 的 guard 并以 78 拒绝。默认
  `up` 不创建 Worker；这不是 D5 实现或启用。

## 3. 自动化测试

新增 `tests/deployment/test_compose_contract.py`，覆盖：

- 默认/显式 profile service 集合、端口与依赖顺序；
- fixed network/IP、trusted proxy、non-root/read-only/capability/logging 合同；
- API/Caddy/ops/restore volume 的 rw/ro 最小权限；
- Secret source/target 与无 SiliconFlow；
- memory/ops/restore guard 的稳定原因和退出码 78；
- Caddy 派生 Dockerfile 的固定 base digest、数值 UID、file-capability removal 和 volume
  ownership；
- `docker compose config -q`、default services 和 profile 列表的安全 CLI 检查。

`tests/deployment/test_ingress_contract.py` 追加 Caddy 内部 8080/8443 端口锁定。

## 4. Changes Requested 窄修订

| Finding | 修订 | 复验证据 |
|---|---|---|
| HTTP/3 地址不可达 | Caddy `servers` 固定 `protocols h1 h2`，不扩大 UDP/8443 ingress | 固定镜像 `caddy validate` passed；HTTPS `/health` 的 `Alt-Svc` 数量为 0；外部仍仅 TCP 80/443 |
| Proxy health 不覆盖 upstream | healthcheck 以 `&&` 同时检查 Caddy admin 和 `http://api:8000/health`，不调用 readiness/chat/Provider | API 停止后约 56.9 秒 proxy 变为 unhealthy 且 HTTPS 502；API 恢复后约 13.3 秒 API/proxy 自动恢复 healthy 且 HTTPS 200 |
| Runbook 权限/身份不一致 | 保持 env root:root 0640、Secret root:10001 0440，所有生产 Compose 命令统一为受控 `sudo docker compose` | 在上述实际权限下，以 root/sudo 执行 `config -q` 和 `up -d` passed；没有放宽目录或 Secret 权限 |

测试新增静态 h1/h2/no-h3、HTTPS 无 `Alt-Svc`、双端 health command 以及 runbook sudo
合同。故障注入只调用 `/health`，没有触发 Provider。

## 5. 验证证据

所有 Python 测试显式使用 `AEMEATH_ENVIRONMENT=test`，没有读取仓库本地 `.env`。

| 检查 | 结果 |
|---|---|
| 完整 pytest | **595 collected；594 passed，1 skipped**；唯一 skip 为未设置本机 Caddy binary 的 integration fixture；固定 Caddy 容器响应头已另行实测 |
| D4 targeted pytest | **18 passed，1 skipped** |
| `ruff check .` | passed |
| `compileall app scripts tests` | passed |
| deployment lock checker | passed；无 missing/unpinned/unhashed/constraint mismatch |
| Compose static config | Windows Compose 5.1.3 与 WSL Ubuntu 22.04 均 `config -q` passed；默认 services 仅 API/proxy |
| linux/amd64 application image | current worktree build passed；production preflight 后 API healthy |
| pinned-base Caddy image | derived-image build passed；proxy healthy；UID/GID 10002；rootfs ro；capabilities empty |
| actual ports/mounts | 仅 proxy 发布 80/443；API 只 expose 8000；Secret mounts ro；data/log/Caddy state scope 符合合同 |
| HTTPS probes | `/health` 200，`/ready` 200；HTTP 自动 308 到 HTTPS |
| HTTP protocols | Caddy 固定 h1/h2；HTTPS response 无 `Alt-Svc`；Compose 仅发布 TCP 80/443 |
| proxy fault injection | API stop 后约 56.9 秒 proxy unhealthy/HTTPS 502；API start 后约 13.3 秒双服务 healthy/HTTPS 200 |
| root operator contract | env root:root 0640、Secret root:10001 0440 下，`sudo docker compose config -q/up -d` passed |
| raw body boundary | Content-Length 与 chunked：128 KiB 到达应用并返回 400 invalid JSON；128 KiB+1 均由 Caddy 返回 413 |
| restart persistence | Compose restart 后同一测试 session 可查询 |
| recreate persistence | 不带 `-v` 的 down/up 后同一测试 session 可查询；Caddy named volumes 保留 |
| Memory fail-closed | 默认实际容器只有 API/proxy；显式 guard exit 78，原因 `memory_worker_gates_not_approved` |

实机环境为 WSL2 Ubuntu 22.04.5 LTS x86_64、Docker Desktop Linux Engine 29.4.3。所有 secret
均为临时随机假值；没有发送 DeepSeek 或 SiliconFlow 请求。验收中只创建一个无 Provider 调用
的 session，用于 SQLite persistence drill。

## 6. 文件交付

新增：

- `compose.yaml`
- `deploy/Caddy.Dockerfile`
- `deploy/Caddy.Dockerfile.dockerignore`
- `scripts/deployment/profile_guard.py`
- `tests/deployment/test_compose_contract.py`
- `docs/agents_communication/deployment_readiness_v0_1_d4_delivery.md`

修改：

- `deploy/Caddyfile`
- `deploy/env/prod.env.example`
- `tests/deployment/test_caddy_body_limit_integration.py`
- `tests/deployment/test_ingress_contract.py`
- `docs/deployment.md`

没有修改 `docs/task_log.md`、本地 `.env` 或 Manager 既存修改
`docs/codex_cross_session_messaging.md`。本 Worker 没有把 T-0140 标记为 done。

## 7. 风险与 Deferred

- Caddy request body directive 仍是已接受的 experimental 风险；任何 Caddy 升级都必须重跑
  Content-Length/chunked 边界。
- 派生 Caddy 镜像固定了 base digest 与 Dockerfile，但发布到 staging/ECS 时仍应记录派生镜像
  自身的 immutable registry digest，禁止只依赖可变 tag。
- 当前静态 Bearer 只适用于批准的单用户封闭测试；不是公开多用户 auth。
- D6 前不得运行实际 ops/restore；D5 两个专项门禁通过并另行批准前不得替换
  memory-worker guard。
- DeepSeek 真实 smoke 仍需单独授权；ECS DNS/TLS、安全组、重启、host reboot 与镜像替换属于
  D8 staging 验收。

## 8. 请求 Manager 复验

请重点复验：

1. 固定 base digest 的最小 Caddy 派生镜像是否接受为 non-root 运行合同；
2. 默认 service、固定 network/IP、端口和 Secret/volume 最小权限；
3. disabled profile 的 fail-closed 边界没有越权实施 D5/D6；
4. HTTPS 不含 h3 `Alt-Svc`，以及 API stop/start 时 proxy unhealthy/healthy 的有界迁移；
5. root-only 配置权限与受控 sudo operator 命令一致；
6. 复验后决定 D4 是否 accepted。T-0140 继续保持 `pending`，不得因本阶段交付标记为 done。
