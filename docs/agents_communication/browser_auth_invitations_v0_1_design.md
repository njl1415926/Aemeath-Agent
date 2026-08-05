# Worker Session 18：Browser Auth And Invitations v0.1 设计方案

Date: 2026-08-04

Status: **Accepted by Manager; T-0142 is `done`; A1–A6 and the required narrow repairs accepted**

Scope source: `docs/web_mvp_contract.md` and the Worker Session 18 assignment.

本文最初的设计审查轮只新增本文档，没有修改运行时代码、数据库 schema、依赖、`.env`、
Manager 管理文档或 `frontend/`。实施结果与 fresh verification evidence 单独记录在 A7 delivery
report，不以本文的设计示例替代交付证据。

Revision 2026-08-04：已按 Manager 首轮审查冻结可执行的 SQLite sentinel migration、
credential-version 并发校验、invitation/reset cheap preflight、Web Session 日志相关摘要，以及
invitation conflict/reissue 语义；第 13 节记录的 Manager 决策已从待定改为批准。Manager 随后已
正式接受本设计并授权 A1–A6，冻结合同不再重开。

## 0. 结论摘要

现有边界可以承接 T-0142，不需要把账号、Cookie、密码、CSRF 或 HTTP 策略放进
`DialogueController`：

```text
Browser / operator CLI
  -> FastAPI Auth adapter / Auth admin adapter
  -> InvitationService / PasswordCredentialService / BrowserSessionAuthService
  -> narrow Auth persistence ports and atomic unit-of-work methods
  -> UserContext
  -> AgentService / SessionService
  -> existing Controller and domain ports
```

核心建议如下：

1. 生产 API Auth 使用显式 `web_session` 模式；dev Bearer 使用显式 `dev_bearer` 模式，不设
   自动探测或 hybrid 模式。任何同时出现 Cookie 与 Bearer 的请求都 fail closed。
2. 邀请、密码重置和 Web Session token 均使用 256-bit CSPRNG material；数据库仅保存带
   domain separation 的 SHA-256 digest。原始 token 只在 operator CLI 成功创建时输出一次。
3. 密码使用 `argon2-cffi` 的 Argon2id；仓库冻结一个版本化 password profile，初始参数不低于
   当前 OWASP 基线 `m=19456 KiB, t=2, p=1`。单 CPU 机器必须先 qualification，不能因性能
   不足静默降低安全参数。
4. 邀请/重置接受先经限流和 token-digest cheap preflight，只有可用 candidate 才进入 Argon2；
   所有 candidate 都必须在 `BEGIN IMMEDIATE` transaction 内重新验证并消费。密码 hash 在事务前
   计算，避免长事务；login 对未知账号仍执行 dummy Argon2，防止账号枚举。
5. 每个 Web Session 使用独立 synchronizer token。为同时满足 `GET /v1/auth/csrf` 可重复读取、
   GET 不改变状态以及数据库不保存客户端原始 CSRF token，客户端 token 由持久化随机 verifier、
   Web Session internal id 和生产 HMAC key 确定性派生。
6. Origin/Referer、`Sec-Fetch-Site`、JSON content type 和双凭据检查属于 HTTP adapter；Web
   Session 解析和 CSRF token 比较属于 Auth service/dependency。所有业务 route 在这些检查成功
   前不得运行。
7. `PermissionService` 和 `AgentService` 不需要改变语义。前者继续 owner-only，后者继续只信任
   已构造的 `UserContext`。
8. raw SQLite 与 SQLAlchemy-on-SQLite 都需要显式增量 migration；当前 SQLAlchemy 的
   `create_all` 不足以迁移旧 `accounts` 表。两个 backend 必须共享 Auth store contract tests。

没有发现必须推翻 `docs/web_mvp_contract.md` 的安全或架构 blocker。第 13 节的 Manager 决策和
本设计已经冻结并获接受。已接受的 D4 Bearer baseline 不在 T-0142 中被替换，Web profile 只在
后续 frontend/E2E 阶段单独启用。

## 1. 当前实现现状

### 1.1 已有能力

- `AccountRecord` 已包含稳定 `id/user_id`、`username`、display name、`active|disabled`、roles、
  dev token hash 和 login timestamps。
- `AccountService` 可 bootstrap 账号、更新 dev token hash、读取账号并记录登录。
- `AuthService.resolve_bearer_token(...)` 对 dev token 做 domain-separated SHA-256，并输出
  `UserContext`；`LocalDefaultAccountResolver` 支持 CLI/显式本地模式。
- `PermissionService.require_owner(...)` 已提供最小 owner-only 边界。
- `AgentService` 接受 `UserContext`，不解析 credential；`DialogueController` 不依赖 Auth、HTTP、
  Cookie 或 Account。
- FastAPI 已有 request-id middleware、统一异常响应、Auth dependency、App Log、Session/Chat
  routes 和 auth 后的 chat rate limit。
- API validation 只公开稳定 `INVALID_INPUT`，不返回 FastAPI 原始 `detail`。
- `AppLoggerPort`、`SafeAppLogger`、`ErrorInfo`、`map_exception(...)`、
  `http_status_for_error(...)` 和 sanitizer 可复用。
- raw `SQLiteStore` 与 `SqlAlchemyStore` 已通过共同 fixture 运行 store contract tests。
- Session、Message、Memory 和 relationship 均按 `user_id` 保持 owner isolation。
- Runtime Guardrails 是单进程门禁；当前 production 必须保持一个 Uvicorn worker。

### 1.2 当前缺口与实现约束

- `accounts` 没有 `username_normalized`；现有 `username UNIQUE` 在 SQLite 中默认区分大小写。
- 没有 credential、邀请、reset、Web Auth Session 或 CSRF persistence。
- `pyproject.toml` 没有 Argon2 dependency。
- FastAPI 只识别 Bearer/local default；当前 `get_user_context` 不读取 Web Cookie。
- 当前 production validation 固定要求 Bearer secret，与最终 Web Auth production profile 不同。
- 当前 API CORS middleware 未允许 credential，符合生产同源方向；Web Auth 模式应继续保持空
  CORS，不为本地开发开启跨源 Cookie。
- raw `SQLiteStore._connect()` 没有逐连接执行 `PRAGMA foreign_keys=ON`。
- SQLAlchemy 只对 fresh database 执行 `Base.metadata.create_all(...)`；它不会为 existing
  `accounts` 加列。
- `SQLiteStore` 的账号写入是单方法单 transaction，尚无跨 Account/Credential/Invitation/
  Web Session 的 unit-of-work。
- 当前 `InProcessApiRateLimiter` 只接受 authenticated `user_id`，不能直接用于 pre-auth
  username/token 流量；Auth 限流还需要硬容量上限，防止攻击者用高基数 token 撑大 bucket map。
- 当前对话 CLI 对用户输入调用 `.strip()`，不适合作为密码或 Auth operator workflow。
- 任务中指定的 `tests/test_api_auth.py` 当前不存在；现有 API Auth coverage 位于
  `tests/test_api.py`。

### 1.3 必须保持不变的边界

- `DialogueController` 不增加账号、密码、Cookie、CSRF、HTTP 或 permission policy。
- `UserContext.user_id` 继续等于 `AccountRecord.id`；Web Auth Session id 不得替代它。
- Web Auth Session 与 dialogue `session_id` 完全分离。
- `AgentService` 不二次查询账号或解析 Auth material。
- `PermissionService` 不升级为 RBAC；v0.1 roles 仍只携带 `['user']`。
- Character、Memory、RAG、Embedding、Prompt、State 和 active
  `WorldInfoEvidenceProvider` 不变。
- 不读取或依赖用户维护的 `frontend/`。

## 2. 建议架构和模块职责

### 2.1 `AccountService`

负责：

- 规范化并验证新 Browser username；新 username 必须已经是 lowercase ASCII 且匹配
  `^[a-z0-9][a-z0-9_.-]{2,31}$`。
- 按 account id 或 `username_normalized` 读取 Account。
- 创建 invitation-bound Account；账号 id 使用独立 opaque id，不由 username 推导。
- 读取/变更 `active|disabled` 状态。
- 保留当前 default/dev account bootstrap 兼容入口。

不负责：

- hash/verify password；
- 生成或解析邀请、reset、Session、CSRF token；
- 设置 HTTP Cookie；
- 判断 dialogue Session ownership。

`AccountService.ensure_account(...)` 不能继续被新 Browser flow 当作通用 upsert。实施时应新增
明确的 `create_invited_account(...)`，并使 username 对已存在账号不可变；旧 bootstrap 路径只允许
返回已有账号或创建相同 identity。

### 2.2 `InvitationService`

负责：

- operator 创建/list/revoke invitation；
- 校验预绑定 username、display name、roles 和 TTL；
- 生成 invitation token、digest 和 internal id；
- 接受邀请时验证 password policy、准备 credential 和新的 Web Session material；
- 调用一个原子 persistence unit-of-work 消费 invitation 并创建 Account/Credential/Session；
- 只向 operator/接受成功的 HTTP adapter 返回必要结果，不记录原 token。

它不发送邮件、不读取前端、不创建 dialogue Session。

### 2.3 `PasswordCredentialService`

负责：

- password exact-input policy；
- Argon2id hash/verify；
- 维护 repository-owned password profile；
- `check_needs_rehash(...)` 和成功登录后的 representation-only rehash；
- dummy verification，降低 username/account/disabled enumeration timing 差异；
- 为 change/reset 准备新的 encoded hash。

它不直接写 Account status、Web Session 或 HTTP response。所有 password field/DTO 的 `repr`
必须隐藏；异常不得包含 password、hash 或 password-derived diagnostic。

### 2.4 `BrowserSessionAuthService`

负责：

- username/password login；
- session token 生成、digest lookup、account status/expiry/revocation 校验；
- 输出内部 `AuthenticatedBrowserSession` 以及下游 `UserContext`；
- `last_seen_at` write coalescing；
- logout、revoke current、revoke all 和 bounded cleanup；
- change/reset 后的 session rotation/revocation；
- 调用 `CsrfTokenService` 派生/验证 synchronizer token。

原始 Cookie 只在 API adapter 被提取一次并传入本 service。下游 `AgentService`、
`SessionService` 和 Controller 只看到 `UserContext`。

### 2.5 CSRF 与同源职责拆分

建议拆分为两层：

- `SameOriginRequestPolicy` 位于 `app/api/`：根据 HTTP method/path/header 校验 JSON content
  type、Origin/Referer 和 `Sec-Fetch-Site`，并拒绝 Cookie+Bearer 混用。
- `CsrfTokenService` 位于 `app/auth/`：根据 authenticated Web Session verifier 派生 token，
  使用 `hmac.compare_digest` 验证 `X-CSRF-Token`。
- FastAPI dependency 负责组合两者：先取得 authenticated Web Session，再对 unsafe protected
  request 执行 CSRF token 校验，最后返回 `UserContext`。

HTTP header、status 和 Cookie helper 不进入 Auth domain service；CSRF token algorithm 不进入
middleware。

### 2.6 `PermissionService`、`AgentService` 与 observability

- `PermissionService` 无需变化。既有 Session ownership 仍由 Session store/service 执行；未来非
  Session owner-only 资源继续使用 `PERMISSION_DENIED`。
- `AgentService` 无需改变 Auth 语义。它继续信任 `UserContext` 并把 request id 放入 turn
  metadata。
- Auth services 接受 `AppLoggerPort`，默认 `NoopAppLogger`，记录 content-safe terminal event。
- 所有错误继续使用 `AemeathError/ErrorInfo`；API 继续使用统一 envelope 和 request id。
- sanitizer 需要增加 CSRF/Set-Cookie/session digest/password hash 等精确敏感 key 覆盖，但不能
  依赖 sanitizer 作为“可以先记录 secret”的理由。

## 3. Port、DTO 与 Service 草案

以下是责任草案，不冻结最终 import path 或每个字段名。

### 3.1 Domain records

```python
@dataclass(frozen=True)
class AccountCredentialRecord:
    account_id: str
    password_hash: str = field(repr=False)
    credential_version: int
    created_at: str
    updated_at: str
    password_changed_at: str


InvitationStatus = Literal['pending', 'used', 'revoked']

@dataclass(frozen=True)
class AccountInvitationRecord:
    id: str
    username: str
    username_normalized: str
    display_name: str | None
    roles: list[str]
    status: InvitationStatus
    created_at: str
    updated_at: str
    expires_at: str
    used_at: str | None
    revoked_at: str | None
    accepted_account_id: str | None


PasswordResetStatus = Literal['pending', 'used', 'revoked']

@dataclass(frozen=True)
class AccountPasswordResetRecord:
    id: str
    account_id: str
    issued_credential_version: int
    status: PasswordResetStatus
    created_at: str
    updated_at: str
    expires_at: str
    used_at: str | None
    revoked_at: str | None


@dataclass(frozen=True)
class WebAuthSessionRecord:
    id: str
    account_id: str
    authenticated_credential_version: int
    created_at: str
    updated_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str
    revoked_at: str | None
    revoke_reason: str | None
```

Token digest、password hash、CSRF verifier 不放入普通 read DTO；只在 persistence write/verify
对象中以 `repr=False` 字段出现。

### 3.2 Service DTOs

```python
@dataclass(frozen=True)
class IssueInvitationRequest:
    username: str
    display_name: str | None
    roles: list[str]
    lifetime_seconds: int


@dataclass(frozen=True)
class IssuedInvitation:
    record: AccountInvitationRecord
    raw_token: str = field(repr=False)
    invitation_url: str = field(repr=False)


@dataclass(frozen=True)
class AcceptInvitationRequest:
    raw_token: str = field(repr=False)
    password: str = field(repr=False)
    request_id: str | None = None


@dataclass(frozen=True)
class LoginRequest:
    username: str
    password: str = field(repr=False)
    request_id: str | None = None


@dataclass(frozen=True)
class BrowserSessionMaterial:
    record: WebAuthSessionRecord
    raw_session_token: str = field(repr=False)


@dataclass(frozen=True)
class AuthenticatedBrowserSession:
    user_context: UserContext
    web_session_id: str
    csrf_verifier: bytes = field(repr=False)
```

`AuthenticatedBrowserSession` 仅是 Auth adapter/dependency 的内部结果。`web_session_id` 只进入
dependency-owned `request.state`，供 CSRF 验证和生成日志相关摘要；它不得进入
`UserContext.metadata`、业务 service、response 或 logger payload。

### 3.3 Narrow repository ports

```python
class AccountCredentialStorePort(Protocol):
    def get_login_identity(
        self, *, username_normalized: str
    ) -> LoginIdentityRecord | None: ...

    def rehash_credential_if_unchanged(
        self, *, account_id: str, expected_hash: str, replacement_hash: str, now: str
    ) -> bool: ...


class InvitationStorePort(Protocol):
    def preflight_acceptance_by_digest(
        self, *, token_digest: str, now: str
    ) -> InvitationPreflight | None: ...

    def create_invitation(self, *, write: InvitationWrite) -> AccountInvitationRecord: ...
    def list_invitations(self, *, query: InvitationListQuery) -> InvitationPage: ...
    def revoke_invitation(self, *, invitation_id: str, now: str) -> RevokeOutcome: ...


class PasswordResetStorePort(Protocol):
    def preflight_acceptance_by_digest(
        self, *, token_digest: str, now: str
    ) -> PasswordResetPreflight | None: ...

    def issue_password_reset(self, *, write: PasswordResetWrite) -> ResetIssueOutcome: ...


class WebAuthSessionStorePort(Protocol):
    def get_session_by_digest(self, *, token_digest: str) -> BrowserSessionIdentity | None: ...
    def touch_session_if_due(self, *, request: SessionTouchRequest) -> bool: ...
    def revoke_session(self, *, request: SessionRevokeRequest) -> bool: ...
    def revoke_account_sessions(self, *, account_id: str, reason: str, now: str) -> int: ...
    def cleanup_sessions(self, *, query: CleanupQuery) -> CleanupPage: ...
```

### 3.4 Atomic unit-of-work port

跨表 invariants 必须由 persistence 实现，不由 service 连续调用多个 auto-commit repository：

```python
class BrowserAuthUnitOfWorkPort(Protocol):
    def accept_invitation(
        self, *, request: AcceptInvitationWrite
    ) -> InvitationAcceptanceOutcome: ...

    def create_login_session(
        self, *, request: CreateLoginSessionWrite
    ) -> WebAuthSessionRecord: ...

    def change_password_and_rotate_session(
        self, *, request: ChangePasswordWrite
    ) -> PasswordChangeOutcome: ...

    def accept_password_reset(
        self, *, request: AcceptPasswordResetWrite
    ) -> PasswordResetAcceptanceOutcome: ...

    def disable_account_and_revoke_auth(
        self, *, request: DisableAccountWrite
    ) -> AccountDisableOutcome: ...
```

两个 preflight DTO 只携带 `repr=False` 的 opaque candidate id/version snapshot，不携带 raw token，
也不是授权依据。它们只用于在 Argon2 前淘汰 unknown/expired/revoked/used candidate；UoW 必须忽略
preflight 的“已验证”含义并在 transaction 内从 durable state 重新判断。

`accept_invitation` 的 transaction 内必须：重新验证 digest/status/expiry、claim invitation、检查
normalized username、创建 Account、创建 Credential、创建 Web Session、标记 invitation used。
一般 storage/fault-injection failure 全部 rollback。唯一特例是确定性的 Account/username conflict：
UoW 不创建任何 Account/Credential/Session，而是 CAS 将该 invitation 标为 `revoked` 后 commit，并
返回 allowlisted `identity_conflict` outcome；公开 API 仍映射为 generic invitation failure。

`create_login_session` 的 write 必须包含 transaction 前读取的 expected account id、active status、
credential hash 和 credential version，以及可选 replacement rehash。UoW 在 `BEGIN IMMEDIATE` 内
重新读取 Account+Credential，只有 Account 仍 active 且 hash/version 都与 expected 值完全相同，
才可进行 representation-only rehash、写 login timestamp 并创建 Session；否则 rollback 且不返回
Session material。

### 3.5 Frozen port 的 adapter 形状

Manager contract 中的：

```python
class BrowserSessionAuthPort(Protocol):
    def resolve_session_token(
        self, *, token: str | None, request_id: str
    ) -> UserContext: ...
```

可以由 `BrowserSessionAuthService` 实现。FastAPI 内部需要 Web Session id/CSRF verifier 时，使用
更窄的 `authenticate_session(...) -> AuthenticatedBrowserSession`；dependency 最终仍只向业务
route 提供 `UserContext`，不得把 principal 传入 `AgentService`。

## 4. Persistence schema 和迁移方案

### 4.1 `accounts.username_normalized`

新增：

```text
accounts.username_normalized TEXT NOT NULL DEFAULT ''
UNIQUE INDEX uq_accounts_username_normalized(username_normalized)
```

`DEFAULT ''` 是永久 schema contract，不是临时 migration aid，也不计划通过后续 ALTER 移除。
SQLite 对 `ADD COLUMN ... NOT NULL` 要求非 NULL default，而当前本机/部署基线 SQLite 3.50.4
不能原地 drop 该 default；因此空字符串只作为 migration sentinel，并由数据库 trigger 禁止在
完成的 row 中留存。参见
[SQLite ALTER TABLE](https://www.sqlite.org/lang_altertable.html)。

新 Browser username 的 normalization 是：

1. 不 trim、不做 Unicode normalization；
2. 必须先匹配 lowercase ASCII regex；
3. `username_normalized = username.lower()`；由于新值只允许 lowercase ASCII，二者相同；
4. username 在 v0.1 创建后不可修改。

旧 Account backfill：

- 对数据库内原 `username` 使用 Python 的 `str.lower()`，不调用 SQLite `LOWER()`，避免 backend
  对非 ASCII 的差异；
- 不静默 trim、重命名或 suffix；
- 在任何 durable migration write 前计算完整 collision set；若两个旧 username 得到相同值，
  整个 migration fail closed 为 `STORAGE_MIGRATION_REQUIRED`，safe reason 为
  `account_username_normalized_collision`；
- 错误响应/普通日志只记录 collision count，不记录 username；operator audit 可显示 account id，
  但仍不显示 credential/token；
- 不符合新 ASCII regex 的 legacy username 可以完成 backfill，但按已批准决策保持 CLI/dev-only，
  禁止 Browser login 和 version=0 reset enrollment；后续只能经受控 rename/enrollment tool 处理。
  不得因此删除或改写原账号历史。

raw SQLite migration 必须在一个 transaction 中按以下顺序执行：

1. `ADD COLUMN username_normalized TEXT NOT NULL DEFAULT ''`；
2. 用预计算、无 collision 的值 backfill 所有 legacy row，并确认
   `SELECT COUNT(*) ... WHERE username_normalized = ''` 为 0；
3. 创建 unique index；
4. 创建严格 guard：`BEFORE INSERT` 在 `NEW.username_normalized = ''` 时 `RAISE(ABORT, ...)`，
   `BEFORE UPDATE OF username_normalized` 在新值为 `''` 时同样 abort；另一个 update trigger 禁止
   `username` 或 `username_normalized` 在创建后改变；
5. 在写 marker/commit 前运行全库 `PRAGMA foreign_key_check`，任何 result row 都 rollback 并
   fail closed。

三个 trigger 名称冻结为：

```text
trg_accounts_username_normalized_insert_nonempty
trg_accounts_username_normalized_update_nonempty
trg_accounts_username_identity_immutable
```

trigger 在 backfill 完成后、同一 transaction 内创建，因此 migration 自身可短暂持有 sentinel，
但其他连接永远看不到已 commit 的 sentinel。正常 INSERT 必须显式提供非空 normalized value；
遗漏字段会先得到永久 default，随后被 INSERT trigger 拒绝。不能只依赖应用层检查，也不得通过
`PRAGMA writable_schema` 改写 default。

每次 startup（包括 marker 已存在）必须验证：column 为 `TEXT NOT NULL`、default 语义精确为
空字符串、sentinel row count 为 0、三个命名 trigger 的 SQL/fingerprint、unique index 的唯一性和
列顺序。之后执行全库 `PRAGMA foreign_key_check`；只要返回一行，就以
`STORAGE_MIGRATION_REQUIRED` fail closed。普通日志只记录 safe reason
`foreign_key_violation` 和总数，不记录 table/rowid/parent 或 row content。SQLite 明确规定该
PRAGMA 每个 FK violation 返回一行，参见
[SQLite PRAGMA foreign_key_check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)。

### 4.2 `account_credentials`

```sql
CREATE TABLE account_credentials (
    account_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    credential_version INTEGER NOT NULL CHECK (credential_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);
```

- 每个 Account 最多一个 current credential。
- `password_hash` 保存 Argon2 encoded string；salt、algorithm version 和 work factors 已包含其中。
- `credential_version` 只在真实 password change/reset 时递增；representation-only rehash 不递增，
  避免无意义地撤销 sessions。
- 不保存 plaintext、reversible ciphertext、password hint 或 derived diagnostic。

### 4.3 `account_invitations`

```sql
CREATE TABLE account_invitations (
    id TEXT PRIMARY KEY,
    token_digest TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL,
    display_name TEXT,
    roles_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'used', 'revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    accepted_account_id TEXT,
    FOREIGN KEY (accepted_account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK (
      (status = 'pending' AND used_at IS NULL AND revoked_at IS NULL) OR
      (status = 'used' AND used_at IS NOT NULL AND revoked_at IS NULL) OR
      (status = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL)
    )
);
```

索引：

- unique `token_digest`；
- partial unique `username_normalized WHERE status='pending'`，同一 username 同时最多一个待用邀请；
- `(status, expires_at, created_at, id)` 用于 bounded list/cleanup；
- `(username_normalized, status)` 用于 create/accept conflict check。

`expired` 是由 `status='pending' AND expires_at <= now` 派生的 read status，不持久化为第四种
状态，避免 wall-clock 状态漂移。

### 4.4 `account_password_resets`

```sql
CREATE TABLE account_password_resets (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    issued_credential_version INTEGER NOT NULL CHECK (issued_credential_version >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'used', 'revoked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK (/* 与 invitation 相同的 timestamp/status coherence */)
);
```

索引：

- unique `token_digest`；
- partial unique `account_id WHERE status='pending'`；
- `(status, expires_at, created_at, id)`；
- `(account_id, status)`。

签发新 reset 时在同一 transaction 撤销该账号以前未使用的 reset。accept 时要求 current
`credential_version == issued_credential_version`；中间发生 change/reset 后旧 token 自动失效。
Manager 已批准 `issued_credential_version=0` 仅用于 active、credentialless 且 username 符合
Browser 规则的 legacy Account；accept 后首个 credential 必须为 version=1。

### 4.5 `web_auth_sessions`

```sql
CREATE TABLE web_auth_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    csrf_verifier BLOB NOT NULL,
    authenticated_credential_version INTEGER NOT NULL
        CHECK (authenticated_credential_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT,
    CHECK (length(csrf_verifier) = 32),
    CHECK ((revoked_at IS NULL AND revoke_reason IS NULL) OR
           (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL))
);
```

索引：

- unique `token_digest` 负责 hot lookup；
- `(account_id, revoked_at, absolute_expires_at)` 负责 revoke-all/account queries；
- `(absolute_expires_at, id)` 和 `(revoked_at, id)` 负责 bounded cleanup；
- 不建立 raw IP、User-Agent、Cookie 或 dialogue session 字段。

`csrf_verifier` 是 server-side 32-byte random input，不是返回给客户端的 CSRF token。它和 token
digest/password hash 一样不得进入普通 DTO、日志或异常。

### 4.6 Token generation、digest 和比较

所有原始 token：

- 使用 `secrets.token_bytes(32)`，保证 256-bit entropy；
- base64url 编码且去掉 padding；
- 使用类型前缀，例如 `inv_`、`rst_`、`wsa_`，用于语法拒绝和防止跨类型误用；
- 设置严格最小/最大长度，超长输入在 hash/DB lookup 前拒绝。

digest：

```text
sha256(b'aemeath:invitation:v1\0' + raw_token_ascii)
sha256(b'aemeath:password-reset:v1\0' + raw_token_ascii)
sha256(b'aemeath:web-session:v1\0' + raw_token_ascii)
```

数据库保存 `sha256:<64 lowercase hex>`。Lookup 使用 indexed equality；取到 candidate 后仍用
`hmac.compare_digest(stored_digest, computed_digest)` 验证。256-bit random token 不需要 password
hash，也不需要数据库 pepper。任何 digest 不进入 public response；日志也不记录 digest，以免它
变成可关联 identifier。

### 4.7 Timestamp contract

- 所有 Auth timestamps 使用 UTC、带 `+00:00`、固定 microsecond timespec 的 ISO-8601 字符串；
- expiry comparison 使用同一 formatter，或解析为 aware datetime 后比较；
- 业务判断统一使用 injected clock，测试不调用 wall clock；
- expiry 边界统一为 `now >= expires_at` 即失效。

### 4.8 raw SQLite migration

建议 migration id 为 `browser_auth_v0_1`：

1. 打开连接后先执行 `PRAGMA foreign_keys=ON`，验证结果为 1；
2. 读取 schema contract；若 marker 已存在，仍验证列、index、FK、CHECK/trigger，不盲信 marker；
3. `BEGIN IMMEDIATE`；
4. 对所有账号预计算 normalized username 和 collision；冲突则 rollback；
5. 以永久 `DEFAULT ''` 增加/backfill `username_normalized`，确认 sentinel count 为 0，再创建三个
   guards 和 unique index；
6. 创建四张 Auth 表及全部 indexes/guards；
7. 在当前 transaction state 上执行全库 `PRAGMA foreign_key_check`，存在任何 result row 则
   rollback/fail closed；
8. 写 migration marker并 commit；
9. commit 后重新 introspect，并再次执行 startup `foreign_key_check`；不完整则启动失败，不做自动
   drop/rebuild。

Migration 不删除或重建 Account、Session、Message、Memory、relationship rows。任一步失败保持旧
业务数据原样；错误不得包含 username/token/hash。

### 4.9 SQLAlchemy-on-SQLite migration

不能只更新 models 后调用 `create_all`。建议：

- `AccountModel` 和四个新 model 表达 fresh schema；
- 在 `initialize_database(engine)` 中增加 explicit Auth migrator；
- existing database 使用同样的 collision algorithm 和 SQLite DDL，在 explicit
  `BEGIN IMMEDIATE` transaction 中增量迁移；
- 使用 SQLAlchemy event 在每个 DBAPI connection 上设置并检查 `PRAGMA foreign_keys=ON`；
- 增加 SQLAlchemy 专用 migration marker，不把未来 Alembic `alembic_version` 与 raw
  `schema_migrations` 混用；
- startup 每次验证 Auth schema；marker/schema 不一致时返回
  `STORAGE_MIGRATION_REQUIRED`，不自动删除 experimental database；
- migration transaction 和每次 startup 都通过底层 DBAPI connection 执行全库
  `PRAGMA foreign_key_check`；任何结果都 fail closed，不能只检查新增 Auth tables。

### 4.10 Backend parity 与 D6 read contract

新增 `tests/store_contract/test_browser_auth_contract.py`，同一 fixture 对两个 backend 运行：

- record/constraint/list/CAS behavior；
- invitation/reset single-use；
- cross-table rollback；
- password/session rotation；
- disable/revoke；
- concurrent accept；
- bounded cleanup/keyset behavior。

legacy migration fault tests可以 backend-specific，但最终可观察结果必须一致。

T-0142 不修改 D6 backup/restore。它应冻结一个 read-only `StoreSchemaInfo` 输出供 D6 后续记录：

```text
db_backend
backend migration ids/revisions
browser_auth_schema_revision = browser_auth_v0_1
schema contract fingerprint
```

D6 manifest 只记录这些 revision/fingerprint 和 SQLite backup artifact metadata；不读取 table row、
token digest、password hash 或 CSRF verifier。Manager 已批准 `StoreSchemaInfo`，且本轮明确不启用
SQLite `PRAGMA user_version` 或 `PRAGMA application_id`。

## 5. Auth、Invitation、Password、Session 与 CSRF 生命周期

### 5.1 Operator 创建 invitation

1. narrow Auth admin CLI 读取 DB backend/path、Web public origin 和 TTL；不启动 Controller、模型、
   Embedding 或 Memory runtime。
2. 校验 username、display name、roles=`['user']` 和 TTL（默认 24h，最大 7d）。
3. cheap preflight 确认 Account 不存在；生成 raw token/digest/internal id。
4. `BEGIN IMMEDIATE` 内重新确认 Account 不存在，将同 normalized username 的所有旧
   `status='pending'` invitation（包括 derived expired）原子改为 `revoked`，然后插入新 invitation。
5. commit 成功后才向 stdout 显示一次 fragment URL；commit 失败不显示 token。

create-invite 的再次执行就是 reissue，不读取旧 token。旧 pending/expired 的撤销与新 token row 的
创建必须在同一 transaction；纯 storage failure 使整笔 transaction rollback，旧 invitation 状态
保持原样，可以安全重试，不存在“旧 token 已撤销但新 token 未创建”的部分完成。

建议 URL：

```text
https://configured-origin/invite#token=inv_<base64url>
```

fragment 不会被 Caddy/API request line 接收。T-0145 frontend 必须在读取后立即
`history.replaceState()`；在此之前不得发出第三方请求。T-0142 不读取或实现 frontend。

### 5.2 接受 invitation

1. API adapter 先做 auth mode、content type、Origin/Referer、Fetch Metadata 和 pre-auth rate limit。
2. 对 request/token 做 bounded syntax validation，并计算 invitation token digest。
3. 使用 digest indexed lookup 做 cheap preflight，constant-compare 后只接受 `pending` 且未过期的
   candidate。unknown/expired/revoked/used 立即返回同一个 generic failure，不进入 password work
   gate，不产生 DB mutation；外部响应、长度和日志字段保持一致。token 为 256-bit 随机且受限流
   保护，preflight 不能被用作公开 invitation oracle。
4. 仅对 preflight candidate 执行 password exact-input policy；policy failure 不进入 Argon2。通过后
   才在 password work gate 内计算 Argon2id hash，并生成 Account id、Web Session token/digest、
   CSRF verifier 和 expiry。
5. `accept_invitation` UoW 进入 `BEGIN IMMEDIATE`，按 digest 重新读取并 constant-compare，再次验证
   status/expiry；preflight snapshot 不是 authority。
6. 若 invitation 的 normalized username 已对应现有 Account，或该 identity 的 username unique
   invariant 已确定性冲突，不创建 Account/Credential/Session；CAS 将该 invitation 从 pending
   改为 `revoked`、写 `revoked_at/updated_at` 并 commit。随机新 Account id 的偶发 PK collision
   属于 storage failure，必须 rollback，不能错误地消费邀请。API 将内部 `identity_conflict`
   映射为同一 generic invitation failure。
7. 无冲突时创建 Account、version=1 Credential、Web Session；CAS 将 invitation 标为 used 并写
   `used_at/accepted_account_id`。
8. success commit 后 API 设置 Cookie 并返回 safe Account summary。

同一邀请的两个并发 accept 都可能在事务前计算 hash，但 transaction linearization 保证最多一个
commit。第二个请求得到 generic invalid/expired response，不创建第二 Account/Session。

除第 6 步明确的 deterministic conflict terminal commit 外，任何 transaction/storage failure 都
rollback：不设置 Cookie、不返回 raw Session token，invitation 仍保持原状态并可重试。若 conflict
revocation 的 commit 本身失败，也整体 rollback，不能报告 invitation 已撤销或产生部分完成。

### 5.3 Argon2id profile、qualification 与 future rehash

依赖建议：`argon2-cffi>=25.1,<26`，放入 runtime dependencies 和 deploy lock；它支持 Python
3.10，并提供 `PasswordHasher.verify()` 和 `check_needs_rehash()`。

Manager 已批准的初始 repository profile：

```text
profile_id = argon2id_owasp_2026_v1
type = Argon2id
version = 19
memory_cost = 19456 KiB
time_cost = 2
parallelism = 1
hash_len = 32
salt_len = 16
```

这是当前 OWASP 最低建议之一，不是无需测试的性能假设。Manager 同时批准单 CPU p95 `<1s` 门禁；
实施前新增 provider-free qualification：

- 在目标 Python 3.10 / single CPU 容器上运行 warmup 后多轮 hash/verify；
- 报告 profile id、参数、样本数、p50/p95/p99 duration、峰值 RSS、并发数和 pass/fail；
- 测试 concurrency=1 以及建议 password gate 上限；
- 目标单次 p95 小于 1 秒，并验证并发内存不会越过容器预算；
- 若最低 profile 失败，implementation/production activation fail closed 并回 Manager，不降低参数；
- qualification 不打印 password/hash/salt，只用固定测试 material 并报告 timing/count。

生产配置只选择 approved `profile_id`，不暴露可任意调低的 m/t/p 环境变量。成功 login 后执行
`check_needs_rehash()`；需要时生成 replacement hash，并在 `create_login_session` 同一 transaction
通过 expected hash/version CAS 更新。representation-only rehash 不递增 credential version；失败
login 不 rehash。

### 5.4 Password exact-input policy

- 12–128 Unicode code points；
- UTF-8 编码最多 1024 bytes；invalid surrogate/encoding 拒绝；
- 不 trim、不 lower、不 normalize、不静默截断；空格和 printable Unicode 保留；
- invite/reset/change 的 policy failure 返回 `PASSWORD_POLICY_REJECTED`；
- login 中超限/非法 password 返回 generic `AUTH_INVALID_CREDENTIALS`，且不执行高成本 hash；
- Pydantic 使用 `SecretStr` 或等价 repr-hidden field；validation error 只记录 count/reason enum。

Password hashing/verification 由独立 bounded semaphore gate 保护，默认 concurrency=1。Pre-auth
limiter 必须在 Argon work 前运行，防止单 CPU 被无限 hash request 占满。

### 5.5 Login 与 enumeration 防护

1. 精确验证 username syntax，生成 normalized username。
2. pre-auth limiter 使用 HMAC-pseudonymized client IP 和 username key，不保存原值。
3. 读取 Account+Credential identity。
4. 不存在、credentialless、disabled 均对固定 dummy Argon2 hash 执行一次 verify；存在 active
   credential 则 verify real hash。
5. missing/wrong/disabled 对外全部返回相同 `AUTH_INVALID_CREDENTIALS`、HTTP 401、message 和
   retryable=false；内部 App Log 可记录 allowlisted reason enum。
6. real verify 成功后，必要则在事务前计算 replacement rehash，并准备全新 Web Session material。
7. `create_login_session` 进入 `BEGIN IMMEDIATE`，重新读取 Account+Credential；要求 Account 仍为
   active，且 current `password_hash` 和 `credential_version` 与 verify 所用 snapshot 完全相同。
8. 只有第 7 步通过，才可选地更新 representation-only hash（version 不变）、touch Account login
   timestamp，并创建 `authenticated_credential_version=current version` 的 Session。任一值变化均
   rollback、丢弃 Session material并返回 generic `AUTH_INVALID_CREDENTIALS`，不设置 Cookie。
9. commit 后才返回 `UserContext` 并设置 Cookie。

不返回“username 不存在”“账号 disabled”或“无 credential”。成功与失败的 DB timing 仍不可能
绝对相同，因此还必须依赖 pre-auth rate limit，而不是声称完全 constant time。

### 5.6 Password change

1. 需要 active Web Session、exact origin 和 valid CSRF。
2. 验证 current password；失败使用 generic `AUTH_INVALID_CREDENTIALS`。
3. 验证/hash new password，要求它不等于 current password（通过 verify，不保存明文比较结果）。
4. 一个 transaction 中：CAS 更新 Credential、`credential_version += 1`，撤销该 Account 的全部
   pre-change Web Sessions（包括当前 token），创建一个 replacement Session 给当前浏览器。
5. commit 后覆盖 Cookie；其他浏览器全部失效。

这样“当前浏览器继续登录”和“所有旧 token 都被撤销”可以同时成立。transaction 失败时旧
password/session 仍有效，不能产生半完成状态。

### 5.7 Operator-assisted password reset

签发：

- operator 以 account id 或 normalized username 指定账号；不接收 password；
- active Account 才可签发；
- 同一 transaction 撤销以前 pending reset，写入新 digest/version/expiry；
- Manager 批准对 active、无 credential 且现有 username 符合 Browser username 规则的 legacy
  Account 签发 `issued_credential_version=0` enrollment reset；保留原 account/user id 和全部历史。
  不符合 Browser username 规则的 legacy Account 不得走此路径；
- 默认 1h，建议 production 最大 24h；
- commit 后仅向 operator 显示一次 fragment URL；签发本身不撤销 Web Sessions，避免未接受 token
  造成无谓登出。

接受：

- 与 invite accept 相同，顺序冻结为：Origin/content/rate gate → bounded request/token validation →
  digest cheap preflight → password policy → Argon2 → transaction revalidation/consume；
- cheap preflight 只对 pending、未过期、active Account 且 current credential version 与 issued
  version 相等的 candidate 放行；version=0 只在上述获批的 credentialless enrollment 条件成立时
  放行。其他情况 generic failure 且不执行 Argon2；
- transaction 必须重新验证 digest/status/expiry/Account active/current version；preflight 不是
  authority。普通 reset 标记 used、替换 credential并递增 version；version=0 enrollment 创建
  version=1 Credential；两者都撤销该 Account 的全部旧 Web Sessions；
- Manager 已批准自动登录：同一 transaction 创建绑定新 credential version 的 fresh Session，
  commit 后设置 Cookie 并返回 `MeResponse`。transaction failure 不撤销旧 Session、不返回新 token。

### 5.8 Browser Session resolve、timeout 与 touch coalescing

每次 Cookie auth：

1. 严格提取一个目标 Cookie；duplicate cookie name、malformed 或超长 token fail closed。
2. 计算 digest并 indexed lookup；constant-compare digest。
3. lookup 必须同时读取 current Account+Credential；验证 account active、credential 存在、
   `authenticated_credential_version >= 1` 且等于 current `credential_version`，再验证
   `revoked_at IS NULL`、idle 和 absolute deadline。
4. credential missing/version mismatch、disabled 或 expired Session 用 CAS 写
   `revoked_at/revoke_reason`；当前请求按 revoked/expired generic public message 拒绝并清 Cookie。
   并发重复写为 idempotent。任何 mismatch 都不得构造 `UserContext`。
5. 只有以上全部通过且 `now - last_seen_at >= touch_interval` 时才尝试：

```sql
UPDATE web_auth_sessions
SET last_seen_at = :now,
    idle_expires_at = :new_idle,
    updated_at = :now
WHERE id = :id
  AND revoked_at IS NULL
  AND last_seen_at = :observed_last_seen;
```

并发请求最多一个 touch write；其他请求可以继续使用已验证的 Session。absolute deadline 永不延长。
默认 idle=7d、absolute=30d、touch interval=5m。

### 5.9 Logout、revoke 和 disable

- logout：valid session+CSRF 后 CAS revoke current，返回 204 并清 Cookie。
- duplicate/concurrent revoke：store 返回 `changed=False`，不报 storage error；API 最终仍清 Cookie。
- operator revoke-account-sessions：按 account id 一次 transaction 更新所有 active rows，返回 count。
- account disable：同一 transaction 将 Account 设为 disabled、revoke all Web Sessions、revoke
  pending reset；enable 不恢复任何 Session/token。
- password change/reset：按 5.6/5.7 处理，不影响 dialogue Session/Message/Memory。
- 已经完成 Auth dependency 的 in-flight request 可能在之后发生的 revoke/disable commit 后完成；
  v0.1 的 linearization point 是每个 request 的 Auth resolve。若要求取消 in-flight turn，需要新的
  admission/cancellation contract，不能让 `AgentService` 偷查 Cookie。

### 5.10 Bounded cleanup

正常 request 不扫描 Session 表。新增明确的 repository/admin operation：

```text
cleanup-auth-sessions --limit N --before <retention cutoff>
```

- 通过 indexed keyset `(absolute_expires_at, id)` / `(revoked_at, id)` 每批最多删除 N 行；
- default 100，hard max 1000；
- 只清理已过 absolute expiry 或已 revoked 且超过 retention 的 rows；
- 一批一个短 transaction，返回 count/next cursor；
- 后续 D7 可用受控 cron/ops profile 调用；T-0142 不修改 D7/Compose scheduler。

Invitation/reset 历史也使用单独 bounded retention operation，不能在 login request 中清全表。

### 5.11 Cookie contract

生产设置：

```text
Name=__Host-aemeath_session
Secure
HttpOnly
SameSite=Strict
Path=/
no Domain
```

- 每次 login/invite/reset/change rotation 都覆盖为 fresh token；
- 不设置 `Expires/Max-Age`，使用 browser-session cookie；server-side timeout 是 authority；
- clear 使用相同 name/path/secure/samesite、空值、`Max-Age=0` 和过去 `Expires`，仍禁止 Domain；
- 所有 Auth response 设置 `Cache-Control: no-store`；不把 Cookie 放入 response JSON；
- production/test 不降低 `Secure`。API integration test 使用
  `TestClient(base_url='https://testserver')`；本地人工测试使用 Caddy local HTTPS。

若未来确需纯 HTTP manual dev，必须使用不同 cookie name 和显式 dev-only profile，production
validation 必须拒绝；本设计不建议 T-0142 首轮加入该降级。

### 5.12 CSRF token 与同源校验

CSRF token：

```text
base64url(HMAC-SHA256(
  production_csrf_key,
  b'aemeath:csrf:v1\0' + web_session_id + b'\0' + csrf_verifier
))
```

- `csrf_verifier` 每 Session 随机 32 bytes；
- Manager 已批准固定 `_FILE` HMAC secret：key 至少 32 bytes，production 仅从固定 root-owned file
  读取；数据库不保存返回给客户端的原始 CSRF token；
- `GET /v1/auth/csrf` 只读地重新派生同一 token，不 rotate DB state，因此多 tab 稳定；
- token 只在 JSON response 和 `X-CSRF-Token` request header 出现，前端仅存内存；
- validation 使用 `hmac.compare_digest`；Session rotation 自动 rotation CSRF token。

同源 policy 对 Web Auth 模式下所有 unsafe `/v1/*` request 生效：

1. media type 必须是 `application/json`（允许参数如 charset）；拒绝 form、multipart、text/plain；
2. `Origin` 存在时必须与 configured public origin 的 scheme/host/effective port 完全相同；
3. `Origin` 缺失时必须有 Referer，并比较其 origin component；二者都缺失则拒绝；
4. `Origin: null`、userinfo、混淆 host、unexpected port 拒绝；不使用 suffix string match；
5. `Sec-Fetch-Site: cross-site` 和 `same-site` 拒绝；本项目不信任 sibling subdomain；
   `same-origin` 接受；`none` 只有在 Origin/Referer exact match 时接受；未知值按标准忽略并仍依赖
   Origin/Referer；header 缺失也仍依赖 Origin/Referer；
6. target origin 永远来自 reviewed config，不从 Host、X-Forwarded-Host 或 X-Forwarded-Proto
   动态推导；trusted proxy 只影响 `request.client` 的 rate-limit identity。

Pre-auth login/invite/reset 没有 ambient Session，因此不要求 synchronizer token，但必须通过以上
Origin/content/fetch checks 和 pre-auth limiter。GET/HEAD route 不改变业务状态；`/auth/csrf`
只派生 response value。

### 5.13 FastAPI execution order

推荐逻辑顺序：

```text
request-id / outer error+log middleware
  -> Web request provenance middleware
       auth mode / dual credential / content type / Origin / Referer / Fetch Metadata
  -> FastAPI dependency graph
       pre-auth limiter OR Web Session resolve
       CSRF token validation for protected unsafe request
  -> Pydantic DTO validation
  -> route
  -> Auth Service
  -> persistence unit-of-work
  -> cookie/no-store response helper
```

FastAPI dependency/body validation 的内部先后不应成为安全假设。Middleware 必须在 body parsing 前
完成 cheap header/provenance gates；dependency 必须保证 route function 和 business service 只有在
Session/CSRF 成功后才执行。Caddy 128 KiB raw body limit 继续是解析前 body cost 上限。

## 6. API 与错误合同

### 6.1 Public DTOs

```python
class InvitationAcceptBody(BaseModel):
    token: SecretStr
    password: SecretStr

class LoginBody(BaseModel):
    username: str
    password: SecretStr

class PasswordChangeBody(BaseModel):
    current_password: SecretStr
    new_password: SecretStr

class PasswordResetAcceptBody(BaseModel):
    token: SecretStr
    new_password: SecretStr

class MeResponse(BaseModel):
    account_id: str
    username: str
    display_name: str | None
    roles: list[str]

class CsrfResponse(BaseModel):
    csrf_token: str
```

不返回 Account status（成功 principal 必为 active）、credential version、Web Session id、token
digest、expiry internals 或 dev token fields。

### 6.2 Endpoint matrix

| Method/path | Auth/CSRF | Success | Cookie | Safe response |
|---|---|---:|---|---|
| `POST /v1/auth/invitations/accept` | invite token; origin only | 201 | set fresh | `MeResponse` |
| `POST /v1/auth/login` | username/password; origin only | 200 | set fresh | `MeResponse` |
| `POST /v1/auth/logout` | Web Session + CSRF | 204 | clear | empty |
| `GET /v1/auth/me` | Web Session | 200 | unchanged | `MeResponse` |
| `GET /v1/auth/csrf` | Web Session | 200 | unchanged | `CsrfResponse` |
| `POST /v1/auth/password/change` | Web Session + CSRF | 204 | rotate | empty |
| `POST /v1/auth/password-reset/accept` | reset token; origin only | 200 | set fresh | `MeResponse` |

所有 Auth response（包括 error）带 request id、`Cache-Control: no-store`。成功/失败均不返回
invitation/reset/Web Session raw token。

### 6.3 Auth mode selection

新增显式：

```text
AEMEATH_API_AUTH_MODE=dev_bearer | web_session
```

- `dev_bearer`：保留现有 AuthService/local-default 测试兼容；不接受 Web Cookie。
- `web_session`：只接受 Web Cookie；Authorization header 即使没有 Cookie 也拒绝，不 fallback。
- 两者都出现：`AUTH_CREDENTIAL_CONFLICT`，HTTP 400，不选择任何一方。
- 不提供 auto/hybrid；production Web profile 必须为 `web_session` 且 CORS empty。
- `UserContext.metadata.auth_method` 可以是 `dev_token|local_default|web_session`。在
  `web_session` 模式，Auth metadata 只携带 `auth_method='web_session'`；不得包含 Web Session
  internal id、correlation digest、token 或 token digest。internal id 仅留在 Auth dependency 的
  request state。

### 6.4 Error codes、HTTP 和 masking

建议新增：

| Error code | HTTP | retryable | Public message/usage |
|---|---:|---:|---|
| `INVITATION_INVALID_OR_EXPIRED` | 400 | false | generic invite accept failure |
| `AUTH_CREDENTIAL_CONFLICT` | 400 | false | generic conflicting credentials |
| `AUTH_SESSION_EXPIRED` | 401 | false | sign in again; clear Cookie |
| `AUTH_SESSION_REVOKED` | 401 | false | sign in again; clear Cookie |
| `CSRF_INVALID` | 403 | false | generic request verification failure |
| `USERNAME_UNAVAILABLE` | 409 | false | operator/create conflict; API accept masks it |
| `PASSWORD_POLICY_REJECTED` | 400 | false | safe policy message, no measured password data |
| `PASSWORD_RESET_INVALID_OR_EXPIRED` | 400 | false | generic reset accept failure |
| `CONTENT_TYPE_UNSUPPORTED` | 415 | false | JSON required |

继续使用：`AUTH_REQUIRED`、`AUTH_INVALID_CREDENTIALS`、`AUTH_ACCOUNT_DISABLED`、
`API_RATE_LIMITED`、`RUNTIME_BUSY`、Storage errors。

Masking：

- login 的 missing username、wrong password、credentialless、disabled 全部公开为同一个
  `AUTH_INVALID_CREDENTIALS` 401；
- invitation 的 unknown/expired/revoked/used/token mismatch/accept username race 全部公开为
  `INVITATION_INVALID_OR_EXPIRED` 400；
- reset 的 unknown/expired/revoked/used/version mismatch/disabled 全部公开为
  `PASSWORD_RESET_INVALID_OR_EXPIRED` 400；
- operator CLI 可以显示稳定、无 secret 的具体状态，例如 `invitation already revoked`；
- Cookie 已经代表持有一个 Session credential，故 expired/revoked 可以使用不同 machine code，
  但 public message 相同，客户端行为都是清 Cookie+重新登录；
- 不把 `WWW-Authenticate: Bearer` 放在 Web Session error 上。

Internal reason 只使用 allowlisted enum，例如 `token_not_found`、`expired`、`revoked`、
`version_mismatch`、`origin_mismatch`；不使用 `str(exc)` 或 exception repr。

### 6.5 Request-id 和 Cookie error handling

- 复用当前 `X-Request-ID` 规则；invalid external id 不 echo/log。
- Auth App Log 和 HTTP error envelope 使用同一 request id。
- Web Session expired/revoked/disabled/malformed 时 dependency 设置 safe response flag，统一 error
  helper 清 Cookie；flag 不保存原 Cookie。
- validation handler 继续只记录 error count，不记录 Pydantic input/body。

## 7. 配置项

建议新增非 secret 设置：

| Setting | Default / bound | Meaning |
|---|---|---|
| `AEMEATH_API_AUTH_MODE` | `dev_bearer` compatibility | explicit adapter |
| `AEMEATH_WEB_PUBLIC_ORIGIN` | required in web mode; HTTPS prod | exact CSRF target |
| `AEMEATH_INVITATION_TTL_SECONDS` | 86400; 3600..604800 | default issue TTL |
| `AEMEATH_PASSWORD_RESET_TTL_SECONDS` | 3600; 300..86400 | reset TTL |
| `AEMEATH_WEB_SESSION_IDLE_SECONDS` | 604800; reviewed lower bound | idle timeout |
| `AEMEATH_WEB_SESSION_ABSOLUTE_SECONDS` | 2592000; <=30d and >=idle | absolute timeout |
| `AEMEATH_WEB_SESSION_TOUCH_SECONDS` | 300; 60..900 and <idle | coalescing |
| `AEMEATH_WEB_AUTH_CLEANUP_BATCH_SIZE` | 100; 1..1000 | bounded ops batch |
| `AEMEATH_PASSWORD_PROFILE` | approved fixed id | no raw work-factor knobs |
| Auth pre-rate settings | endpoint-specific reviewed defaults | login/invite/reset |
| `AEMEATH_PASSWORD_WORK_CONCURRENCY` | 1 in v0.1 | CPU/memory gate |

Secret：

```text
AEMEATH_WEB_AUTH_CSRF_KEY_FILE=/run/secrets/web_auth_csrf_key
```

- production 固定 path、root-owned、应用 group-readable、不可写，至少 32 random bytes；
- plaintext env source 在 prod 被拒绝；
- repr/errors/preflight 不显示 value；
- key 必须跨进程重启稳定；rotation 需要接受 previous+current key 的短期方案或明确使所有
  existing CSRF token 失效并要求客户端重新 fetch；Web Session Cookie 本身不应因单纯 CSRF key
  rotation失效。

Web Auth production profile 必须拒绝 local default、dev Bearer secret 和非空 CORS。Manager 已
批准 T-0142 实现两个显式 auth mode，但本轮不修改已接受的 D4 API-only Bearer baseline；独立
Web production profile 只在 T-0145/T-0146 frontend/E2E 阶段启用。T-0142 不自行修改
Compose/Caddy/D6。

## 8. Operator CLI

### 8.1 入口与 runtime 边界

建议独立入口：

```text
python -m app.auth.admin create-invite ...
python -m app.auth.admin list-invites ...
python -m app.auth.admin revoke-invite <invitation_id>
python -m app.auth.admin issue-password-reset --account <id-or-username>
python -m app.auth.admin disable-account <id-or-username>
python -m app.auth.admin enable-account <id-or-username>
python -m app.auth.admin revoke-account-sessions <id-or-username>
```

它只构造 narrow Auth config/store/services，不构造 `ApplicationRuntime`、Controller、模型、
Embedding、Memory Worker 或 prompt logger。这样 Auth ops 不依赖 Provider secret/availability。

### 8.2 Token output

- create/issue commit 成功后向 stdout 显示一次完整 fragment URL；DB 只存 digest；
- App Log terminal event 在输出 token 前后都不能包含 stdout payload；
- `repr()`/dataclass/error 禁止含 raw token；
- 默认不提供 `--token`、`--password` 参数，避免 shell history secret；
- revoke/list 只接受 invitation/reset internal id，不接受原 token；
- token output 明确提示 `This value will not be shown again`；create-invite 重新执行时，在创建新
  token 的同一 transaction 撤销同 username 的旧 pending/expired invitation，不是读取原 token；
  纯 storage failure 全部 rollback，可以安全重试。

### 8.3 List、disable 和 errors

- list-invites 显示 id、username、derived status、created/expires/used/revoked times；不显示 digest、
  accepted Account secret 或 display-name 以外的 PII；默认 bounded 50、hard max 200、支持 cursor；
- revoke 已 used invitation 返回 conflict，不回滚 Account；重复 revoke idempotent；
- disable-account 原子 disable+revoke，enable 只恢复 Account status；
- revoke-account-sessions 返回 revoked count，不列出 token/session digest；
- storage/config error 使用 stable public/safe operator message 和 non-zero exit code；不破坏主对话
  CLI 或数据库；
- 不允许人工 SQL 作为常规操作或修复步骤。

可增加 `cleanup-auth-sessions` 和 `audit-usernames` 作为 ops-only 命令；前者 bounded，后者只用于
migration preflight。它们不在 Web UI 暴露。

## 9. Observability 与 sanitizer

### 9.1 事件建议

```text
auth.invitation_issued
auth.invitation_accept_completed / failed
auth.invitation_revoked
auth.login_completed / failed
auth.logout_completed
auth.session_expired / revoked
auth.password_changed
auth.password_reset_issued / accepted / failed
auth.account_disabled / enabled
auth.csrf_rejected
auth.origin_rejected
auth.pre_rate_limited
auth.cleanup_completed
```

### 9.2 唯一允许字段

- `event_name`
- `status`
- `request_id`
- `account_id`（仅已安全解析时）
- `invitation_id` / `password_reset_id`
- `web_session_correlation`：仅允许不可逆相关摘要
- `error_code`
- allowlisted reason enum
- `duration_ms`
- `count` / `retry_after_seconds`

不要把 normalized username 当默认日志字段。Login failure 在 identity 未确认时不记录 account id。
Web Session internal id 绝不进入 App/Trace/Prompt/HTTP access log。相关摘要固定为完整
`sha256(b'aemeath:web-session-correlation:v1\0' + web_session_id_ascii)` 小写 hex；internal id 必须
至少有 128-bit CSPRNG entropy。该摘要只用于同一 Session 的 content-safe correlation，不放入
`UserContext` 或 public response，也不能用 Session token digest 代替。

### 9.3 禁止字段

- raw Cookie / `Set-Cookie`
- Web Session token 或 digest
- Web Session internal id
- CSRF token、HMAC key、verifier
- invitation/reset token 或 digest
- password、password hash、salt、Argon diagnostic
- Authorization
- raw IP、User-Agent body
- request body、username+password pair
- 用户消息、Prompt/Response、Memory/Evidence
- Python exception repr/trace body/provider body

### 9.4 Sanitizer 扩展和 zero-leak verification

在现有 sensitive key 基础上增加精确覆盖：`set-cookie`、`x-csrf-token`、`csrf`、
`password_hash`、`session_token`、`invitation_token`、`reset_token`、各类 digest/verifier/secret。
Auth code 在创建 `AppLogRecord` 前仍只构造 allowlist payload；sanitizer 是第二层。

测试使用每类唯一 marker，扫描：HTTP response/headers、App Log、Trace Log、Prompt Log、CLI
stderr/stdout（除被明确授权的一次 token stdout）、mapped error 和异常文本。不得用真实 secret。

## 10. 测试矩阵

### 10.1 Unit tests

| Area | Cases |
|---|---|
| Username | regex、lowercase、无 trim、case conflict、legacy non-ASCII policy |
| Token | 256-bit shape、type/domain separation、malformed/oversize、digest compare |
| Password | 12/128 code point、1024 UTF-8 boundary、spaces preserved、invalid UTF-8、no truncation |
| Argon2 | hash/valid/wrong、encoded Argon2id parameters、dummy verify、needs-rehash/CAS |
| CSRF | per-session stable derivation、different session different token、bad/missing compare |
| Origin | exact Origin、Referer fallback、null/missing/suffix/port/userinfo、Fetch Metadata matrix |
| Cookie helper | set/rotate/clear exact attributes、no Domain、no persistent expiry on set |
| Auth mode | bearer only、cookie only、both、wrong mode、local default gate |
| Rate/gate | pre-auth longest Retry-After、hard bucket capacity、bounded sweep、password gate |

### 10.2 Store contract tests（两个 backend）

- invitation create/list/revoke/expiry/accept/replay；
- invitation reissue 在同一 transaction 撤销旧 pending/expired 并创建新 row；fault 时全部 rollback；
- accept 的确定性 Account/username conflict 只 commit invitation `revoked`，不创建其他 Auth row；
- 同 normalized username pending uniqueness；
- barrier + two store instances 并发 accept 同一 invitation，success count 必须为 1；
- acceptance 中 Account/Credential/Session/used marker 任一步 fault injection 后全部 rollback；
- reset issue/reissue/revoke/expiry/version mismatch/version=0 enrollment/accept+fresh Session；
- login session create 在 transaction 内重验 Account active、expected hash/version；Credential rehash
  与 Session create 使用同一 CAS transaction；
- idle/absolute expiry、每次 resolve 的 credential-version equality、touch CAS、revoke current/all、
  duplicate revoke；
- password change/reset session rotation；
- account disable transaction；
- bounded cleanup page/cursor 不跳过、不超限；
- FK、unique、CHECK/trigger contract introspection；
- timestamp boundary；
- raw/SQLAlchemy DTO 和 exception behavior parity。

### 10.3 Migration tests

- fresh raw / fresh SQLAlchemy schema；
- 旧 accounts 正常 backfill，重复 startup idempotent；
- `Alice`/`alice` collision fail-fast，rows/marker/schema 不被半写；
- `username_normalized` 永久 `DEFAULT ''`、insert/update/immutability triggers、unique index 均可
  introspect；任何 post-migration sentinel row fail closed；
- marker 存在但 column/default/index/FK/trigger 缺失或漂移时 fail closed；
- legacy invalid username 保留且不自动 Browser-enable；
- 原 Account、Session、Message、Memory、relationship row count/content 不变；
- SQLAlchemy existing database 真实增列，而不是误以为 `create_all` 已迁移；
- foreign key 每连接开启；
- migration transaction 和每次 startup 的 `PRAGMA foreign_key_check` 都无结果；构造 orphan 时
  fail closed且日志只含 reason/count；
- schema-info 输出包含 Auth revision，且不包含 row secret。

### 10.4 API integration tests

- 七个 Auth endpoints 的 DTO/status/request-id/no-store；
- invite/login/reset/change 的 Set-Cookie 和 rotation；logout/invalid Session clear；
- login generic response/timing work path；
- invitation/reset generic public error masking；invalid digest preflight 不调用 Argon2，valid candidate
  才调用一次并在 transaction 内重验；
- Cookie missing/malformed/expired/revoked/disabled/credential-version mismatch；mismatch 清 Cookie；
- idle/absolute expiry，`last_seen_at` coalescing；
- CSRF missing/wrong/other-session；Origin/Referer/Sec-Fetch-Site/content-type matrix；
- GET/HEAD 无业务 mutation；
- Bearer+Cookie dual rejection；web mode 不接受 Bearer，dev mode 不接受 Cookie；
- TestClient 使用 HTTPS，确认 `Secure` Cookie 实际自动发送；
- current Chat/Session POST 在 web mode 也必须 CSRF；
- FastAPI validation 不泄露 `SecretStr` 或 body；
- request id/log correlation。
- `UserContext.metadata` 不含 Web Session internal id；日志只有不可逆
  `web_session_correlation`，unique-marker 扫描证明 raw id 不泄漏。

### 10.5 Concurrency/fault tests

- concurrent invite accept only one success；
- concurrent reset accept only one success；
- concurrent last_seen touch at most one write；
- concurrent logout/revoke idempotent；
- password change/representation rehash 与 login/reset race 重验 hash+credential version，不创建
  old-version/stale-hash Session；
- account disable 与 session creation transaction race 最终无 active Session；
- SQLite busy/storage exception 映射不含 SQL parameter/token；
- AppLogger failure best-effort，不改变 Auth transaction outcome。

### 10.6 Isolation 与 regression

- Account A Cookie 不能读取/变更 Account B dialogue Session/Message/Memory/relationship；
- invitation/reset/session token 不能解析到其他 Account；
- API dependency/route 不 import 或调用 `DialogueController`；
- `AgentService` 继续只接收 `UserContext`；
- dev Bearer existing tests 保持；
- Session/Memory owner isolation tests 保持；
- sanitizer marker zero-leak；
- full Python 3.10 `pytest`、`ruff check .`、`compileall`。

### 10.7 Deployment smoke（实施后但不在本设计轮执行）

- Python 3.10 linux/amd64 wheel/deploy lock 安装 Argon2；
- one Uvicorn worker；
- HTTPS Caddy 下真实 `__Host-` Cookie；
- same-origin Origin/CSRF；
- restart 后 Web Session/Credential 可读且 CSRF key 稳定；
- production 无 Bearer acceptance、CORS 为空；
- 不发真实 Provider request；
- D8/E2E/CSP/Frontend integration 仍归 T-0145/T-0146/T-0140 后续 gate。

本设计轮不运行 pytest/ruff/compileall，因为没有修改运行时代码；实施交付必须给出 fresh evidence，
不能引用历史 test count 作为验收。

## 11. 预计修改文件

### 11.1 建议新增

```text
app/auth/schemas.py
app/auth/ports.py
app/auth/tokens.py
app/auth/passwords.py
app/auth/invitations.py
app/auth/browser_sessions.py
app/auth/csrf.py
app/auth/admin.py
app/auth/qualify_passwords.py
app/api/routes/auth.py
app/api/web_security.py
app/db/browser_auth_migrations.py
app/db/repositories/browser_auth.py
tests/test_password_credentials.py
tests/test_browser_auth.py
tests/test_api_auth.py
tests/test_auth_admin_cli.py
tests/test_browser_auth_migrations.py
tests/store_contract/test_browser_auth_contract.py
```

实际实现可合并过小文件，但不能把 HTTP/persistence/password/CSRF 职责混回一个巨型 service。

### 11.2 建议修改

```text
app/accounts/schemas.py
app/accounts/ports.py
app/accounts/service.py
app/auth/__init__.py
app/auth/errors.py
app/auth/service.py                 # 保留 dev Bearer compatibility
app/api/dependencies.py
app/api/errors.py
app/api/schemas.py
app/api/server.py
app/api/routes/__init__.py
app/config.py
app/main.py                         # account bootstrap/auth mode wiring only
app/db/schema.sql
app/db/models.py
app/db/session.py
app/db/sqlite_store.py
app/db/sqlalchemy_store.py
app/db/store_factory.py             # typing/facade capability only
app/db/repositories/accounts.py
app/errors/codes.py
app/errors/http.py
app/errors/sanitizer.py
pyproject.toml
requirements.deploy.lock
.env.example
tests/test_accounts.py
tests/test_auth.py
tests/test_api.py
tests/test_errors.py
tests/conftest.py
```

T-0142 只实现显式 auth mode/config validation，不修改已接受的 D4 Bearer deployment baseline。
Web profile 的 deployment env/preflight/Compose Secret mount 在 frontend/E2E 阶段单独启用；不得
顺带修改 D6 backup/restore。

### 11.3 明确不修改

```text
app/agent/dialogue_controller.py
app/character/
app/memory/
app/retrieval/
app/embeddings/
app/prompts/
app/state/
frontend/
docs/architecture.md
docs/interfaces.md
docs/task_log.md
D6 backup/restore implementation
Streaming / Web UI / Session API v0.2
```

## 12. 风险与 Deferred

### 12.1 主要风险与缓解

| Risk | Mitigation |
|---|---|
| Argon2 CPU/RAM DoS | pre-auth limiter、password gate=1、qualification、body/token bounds |
| invalid invitation/reset CPU DoS | rate limit → digest cheap preflight → Argon2 → transaction revalidation |
| invitation/reset replay | unique digest、pending CAS、`BEGIN IMMEDIATE`、one transaction |
| username migration collision/sentinel | full preflight、permanent default、strict triggers、startup introspection |
| SQLAlchemy `create_all` drift | explicit migrator + startup introspection + parity tests |
| SQLite FK/orphan | every-connection enable + migration/startup `foreign_key_check` + fail closed |
| Session fixation | login/accept/reset/change always fresh token；old token revoke |
| stale-credential Session | login transaction recheck + per-resolve credential-version equality |
| CSRF token reload/multi-tab | deterministic per-session HMAC derivation；GET read-only |
| Origin spoof via proxy headers | configured target origin；不从 forwarded/Host 推导 |
| high-cardinality limiter memory | HMAC keys、hard capacity、bounded sweep、global bucket |
| secret leakage | allowlist logs、repr-hidden DTO、sanitizer、unique-marker scans |
| revocation race | Auth resolve linearization documented；atomic future requests denied |
| Auth row growth | indexed bounded cleanup；不在 request 全表扫 |

### 12.2 Deferred

- public registration、email、OAuth、third-party login、Passkey、browser admin；
- JWT/refresh token、多节点/distributed Session、Redis/Postgres；
- MFA、breached-password online lookup、password history；
- user self-service forgot-password delivery；
- in-flight dialogue cancellation on Account disable/revoke；
- Web Session device list、User-Agent/IP binding、geo/risk scoring；
- periodic Session renewal timeout；v0.1 只含 idle+absolute；
- automated Auth cleanup scheduling/retention policy（D7）；
- frontend fragment removal/UI/E2E（T-0145/T-0146）；
- D6 backup/restore implementation；只冻结 schema read contract；
- production multi-worker；当前 process-local limiter/gates 仍要求一个 worker。

## 13. Manager 已批准决策与执行状态

以下决策已批准，本轮 re-review 不再把它们列为开放选择：

1. **旧 credentialless Account 的 Browser enrollment**：允许 operator 对 active、无 credential、
   且现有 username 符合 Browser username 规则的 legacy Account 签发 version=0 reset enrollment。
   accept 原子创建 version=1 Credential，保留原 `account_id/user_id`、Session、Message、Memory、
   relationship 及其他历史。
2. **Password reset accept 自动登录**：撤销该 Account 的旧 Web Sessions 后，在同一 transaction
   创建绑定新 credential version 的 fresh Session；commit 后设置 Cookie并返回 `MeResponse`。
3. **D4 profile transition**：T-0142 实现 `dev_bearer|web_session` 两种显式模式，但不修改或替换
   已接受的 D4 Bearer API-only baseline；Web profile 在 frontend/E2E 阶段单独启用。
4. **CSRF secret**：采用固定 `_FILE` HMAC secret。数据库只保存 per-session random verifier，不保存
   任何已返回给客户端的原始 CSRF token；保留 SameSite Strict、Origin/Referer 和 Fetch Metadata
   组合防护。
5. **D6 schema identification**：批准 read-only `StoreSchemaInfo`。本轮不启用 SQLite
   `PRAGMA user_version` 或 `PRAGMA application_id`。
6. **Argon2 qualification**：批准
   `argon2id_owasp_2026_v1(m=19456,t=2,p=1)` 和 single-CPU p95 `<1s` 门禁。目标环境测量失败时
   fail closed并回 Manager，不降低参数；成功登录按 `check_needs_rehash()` 处理 representation
   upgrade。
7. **Legacy invalid username**：migration 保留原值、稳定 id 和历史，但禁止 Browser login/reset
   enrollment。后续只能通过单独的 Manager-approved 受控 rename/enrollment tool 处理；本轮不实现。

邀请制、Argon2id、opaque server-side Session、Cookie attributes、同源、CSRF、one-time digest
token、双 backend parity，以及本次 Changes Requested 新冻结的 migration/version/preflight/logging/
reissue 语义均不重开。Manager 已明确接受修订后的整份设计，T-0142 已更新为 `in_progress`，
A1–A6 获准执行；完成后以 A7 delivery report 等待最终验收。

## 14. 实施顺序

### A0 — Manager design acceptance

- 已复审本文对 3 个 High、2 个 Medium finding 的修订并给出明确 acceptance；
- Manager 已将 T-0142 更新为 `in_progress`；Worker 不修改 Manager 管理的共享状态文档。

### A1 — Password/token primitives and qualification

- 增加 dependency/lock、password profile、token/digest/CSRF primitives；
- 运行 Python 3.10 single-CPU qualification；
- unit tests 和 zero-repr/zero-log tests。

### A2 — Schema migration and store contracts

- 先实现 raw/SQLAlchemy migrations、models/repositories/UoW；
- 添加 collision、rollback、FK、concurrency、parity tests；
- 冻结 `StoreSchemaInfo`，不实现 D6。

### A3 — Account/Invitation/Credential services and operator CLI

- 实现 issue/list/revoke/accept/reset/disable/enable/revoke sessions；
- fault injection 验证 token 只在 commit 后显示一次；
- 不启动模型/runtime。

### A4 — Browser Session and CSRF services

- login/resolve/touch/timeout/revoke/rotation/cleanup；
- password change/reset transaction；
- dummy verify、pre-auth limiter、password gate。

### A5 — FastAPI adapter

- explicit auth mode、provenance middleware、dependencies、routes/DTOs/error mapping；
- Cookie/no-store/clear helper；
- 将现有 Chat/Session unsafe routes 纳入 Web CSRF policy；
- 保持 dev Bearer tests。

### A6 — Focused and full verification

```text
pytest focused unit/store/API/concurrency suites
pytest full suite with writable local --basetemp and no cache provider if Windows temp denies access
ruff check .
python -m compileall -q app tests
git diff --check
Python 3.10 linux/amd64 Argon2/install smoke
HTTPS Cookie/Origin/CSRF deployment smoke when authorized
```

### A7 — Worker delivery report and Manager re-review

报告 modified files、ports/errors/schema、migration behavior、Argon qualification、focused/full test
evidence、未跑的 real browser/deployment checks 和剩余风险。Worker 不把 T-0142 标记为
`in_progress`/`done`；只有 Manager 可以接受并更新共享文档。

## 15. 参考资料

- [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [argon2-cffi documentation](https://argon2-cffi.readthedocs.io/en/stable/)
- [argon2-cffi PyPI release metadata](https://pypi.org/project/argon2-cffi/)
- [SQLite ALTER TABLE](https://www.sqlite.org/lang_altertable.html)
- [SQLite PRAGMA foreign_key_check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)
