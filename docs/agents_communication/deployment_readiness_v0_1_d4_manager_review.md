# T-0140 Deployment Readiness v0.1 D4 Manager Review

日期：2026-07-30；最终复验：2026-07-31

状态：**Accepted；D4 gate closed；T-0140 remains pending**

## 1. 复验范围

Manager 基于当前未提交 D4 工作树独立复验：

- `compose.yaml` 默认 service/profile、网络、端口、mount、Secret 和安全参数；
- API/Caddy 两个 `linux/amd64` 镜像的无缓存构建；
- Ubuntu 22.04 WSL2 上的真实 Compose 启动、HTTPS、鉴权、body limit、profile guard、
  restart/down-up persistence 和资源清理；
- 完整 pytest、Ruff、compileall、deployment lock 和 `git diff --check`。

未读取项目 `.env`，未调用真实 DeepSeek/SiliconFlow；只使用隔离的随机假 Secret 和一个
不触发 Provider 的测试 Session。复验产生的容器、镜像、network、volume、假 Secret、测试
SQLite/JSONL 和固定 WSL 测试目录均已清理。

## 2. Findings

### Medium / Blocking：Caddy 宣告了不可达的 HTTP/3 端点

当前 Caddy 在容器内使用 HTTPS 端口 8443，Compose 只发布 TCP `443 -> 8443`，没有发布
UDP，也没有发布宿主 8443。真实 HTTPS 响应却包含：

```text
Alt-Svc: h3=":8443"; ma=2592000
```

这会让支持 HTTP/3 的客户端尝试不可达的外部 UDP/8443，随后才回退到 HTTP/2 或 HTTP/1.1。
首发合同没有批准 HTTP/3/UDP ingress，因此推荐在 Caddy 中显式限制为 `h1 h2`，而不是扩大
Compose 端口和安全组范围。

修复验收条件：

- Caddy 只启用 HTTP/1.1 和 HTTP/2；
- HTTPS `/health` 响应不再含 `Alt-Svc: h3=...`；
- 增加静态配置测试和真实容器响应头回归证据；
- 保持外部只发布 TCP 80/443。

### Medium / Blocking：Proxy healthcheck 不覆盖 upstream

`proxy` 当前只探测 Caddy admin API `http://127.0.0.1:2019/config/`。Manager 停止 API 并等待
超过一个 proxy health interval 后，观测到：

```text
proxy_health=healthy
https_status=502
```

这不符合批准设计中 proxy health 应覆盖 provider-free HTTPS/internal-upstream 状态的合同。
Admin API 存活只能证明 Caddy 进程存在，不能证明 ingress 可以服务请求。

修复验收条件：

- healthcheck 同时验证 Caddy 存活和 `api:8000/health`；不得调用 `/v1/chat` 或任何 Provider；
- 停止 API 后，proxy 必须在有界时间内变为 unhealthy；
- API 恢复 healthy 后，proxy 必须恢复 healthy；
- 增加相应 Compose fault-injection smoke 或可重复脚本证据。

### Low / Required：Operator runbook 的权限与命令不一致

文档按 `root:root 0640` 安装 `/etc/aemeath/prod.env`，Secret 目录也是 root-only traversal，
随后却要求普通用户直接执行 `docker compose --env-file /etc/aemeath/prod.env ...`。按文档原样
执行会得到：

```text
stat /etc/aemeath/prod.env: permission denied
```

修复验收条件：

- 明确首发部署命令由受控 root/sudo operator 执行，并统一修订 Compose 命令；或设计独立
  operator group 和完整目录/文件权限合同；
- 不得通过放宽 Secret 到 world-readable 解决；
- 增加一次按文档权限和身份执行 `config -q` 的 smoke。

## 3. 已通过部分

- 全量测试：592 passed，1 skipped；D4 定向测试 16 passed；Ruff、compileall、lock checker、
  `git diff --check` 均通过。
- 当前工作树无缓存构建 API 与固定 digest Caddy 派生镜像成功。
- 默认实际容器只有 API/Caddy；仅 Caddy 发布 TCP 80/443，API 8000 未发布。
- 两个容器均为 non-root、read-only rootfs、`cap_drop: ALL`、`no-new-privileges`，有效
  capability 全为 0；Caddy `/data`、`/config` 可写。
- Secret 不在 container environment、inspect、JSONL 或 stdout/stderr 中；无 SiliconFlow
  mount，Long-term Memory 保持关闭。
- HTTP redirect 的 `Location` 正确指向外部标准 HTTPS 地址，没有错误附带 `:8443`。
- Content-Length 和 chunked 均满足 128 KiB 到达应用、128 KiB+1 返回 413。
- Memory profile 默认不创建；显式 guard 容器无 network/mount，稳定退出 78。
- `docker compose restart` 和不带 `-v` 的 down/up 后测试 Session 仍可查询，Caddy named
  volume 保留。

## 4. Manager 初次结论

D4 的总体拓扑和安全边界成立，不需要重构。完成上述三项窄修订并补齐对应测试后重新提交
Manager 复验。D4 未通过前不进入 D6-D8；D5 继续受独立双门禁约束。

## 5. 最终复验与结论

2026-07-31，Manager 对三项 Changes Requested 做了独立复验：

- 当前工作树重新执行 linux/amd64 无缓存 Compose build，API 与 Caddy 镜像均成功构建；
- Caddy 仅启用 h1/h2，HTTPS `/health` 返回 200，`Alt-Svc` 响应头数量为 0，外部仍只发布
  TCP 80/443；
- 停止 API 后，proxy 在约 67.6 秒内变为 unhealthy，HTTPS 返回 502；恢复 API 后约 16.7 秒
  内 API/proxy 均恢复 healthy，HTTPS 返回 200；
- 在 env `root:root 0640`、Secret `root:10001 0440` 的真实权限下，普通用户直接读取 env
  会被拒绝，受控 root/sudo 路径执行 `docker compose config -q` 和部署命令成功；
- D4 定向测试为 `18 passed, 1 skipped`，完整回归为 `594 passed, 1 skipped`；Ruff、
  compileall、deploy lock checker 和 `git diff --check` 均通过。

故障窗口文档已按 `15s interval + 3s timeout + 4 retries` 修正为最多约 75 秒，避免把正常的
探针 timeout 错判成门禁失败。本轮未读取项目 `.env`、未调用真实 Provider，临时假 Secret、
容器、镜像、network、volume 和测试宿主目录均应在验收结束后清理。

**最终结论：D4 accepted。** T-0140 继续保持 `pending`；可按已批准设计进入 D6，D5 仍受
WAL/busy-timeout 与 lease/fencing/stale-recovery 两项独立门禁约束。
