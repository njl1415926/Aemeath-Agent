# Worker Session 16：Runtime Guardrails v0.1 设计方案

Date: 2026-07-28

Status: Accepted / done

Review state: Manager final acceptance completed on 2026-07-29 / 506 tests passed

Manager 已批准整体架构及第二轮修订，Embedding Foundation 已对 §5.7 的状态机、ownership、
Factory bundle 与完整调用点迁移作出最终 `Approved` 结论，因此实现门禁已解除。实现、完整回归、
故障注入修订与 Manager 最终验收均已完成；总项目状态由 Manager 记录在 `docs/task_log.md`。

## 0. 结论摘要

建议把 Runtime Guardrails 拆为三个边界，而不是把策略放入 `DialogueController`：

1. `AgentService` 外围的进程内 dialogue admission：按 `(user_id, session_id)` 串行同一
   用户的 Session，再用全局 semaphore 限制同时执行的 dialogue turn。CLI 与 FastAPI 已共同调用
   `AgentService.handle_chat(...)`，因此可自然复用。
2. provider-neutral 并发 wrapper：在 `ChatModelClient` 和 `EmbeddingProviderPort` 外层分别
   放置有界等待的 semaphore。模型、Embedding 的具体客户端不决定系统级并发策略。
3. FastAPI chat 边界的内存 token bucket：认证后的 `user_id` 是主限流 key，连接 IP 是
   辅助 key；CLI 不经过该 HTTP limiter。

输入在 Controller 和消息持久化之前由共享的 `ChatInputLimitPolicy` 校验 message 字符数、
UTF-8 字节数和 metadata 确定性 JSON 大小；DeepSeek payload 必须携带配置化
`max_tokens`。这给出 v0.1 的单请求硬上限，但不把精确 token/金额核算塞入公开模型契约。

composition root 返回显式 `ApplicationRuntime`，并用 `RuntimeResources` 保存自己创建的可关闭
资源。FastAPI lifespan、CLI `finally`、独立 Memory Worker `finally` 只调用该生命周期对象，
不得沿 Controller/wrapper 私有字段查找 HTTP client。Embedding Factory 返回公开的
`EmbeddingProviderRuntime(provider, close)` bundle，让外层 wrapper 保持不透明同时仍可登记资源。

CLI normal dialogue 与 CLI `/memory extract` 必须使用两个不同的 model wrapper，但共享同一个
进程内 LLM gate 和同一个 owned DeepSeek transport：只有 dialogue wrapper 带精确白名单
fallback，extraction wrapper 直接调用 DeepSeek。API 和 Memory Worker 的 dialogue/extraction
路径都不安装 fallback。

JSONL App/Trace/Prompt logger 各自以实例写锁保证单进程线程安全；只有 App/Trace 统一包裹
`SafeAppLogger` / `SafeTraceLogger` 作为 best-effort 边界，Prompt Log 不新增 safe wrapper。

DeepSeek transport 建议从每次 `urllib.request.urlopen(...)` 改为一个进程生命周期内复用的
`httpx.Client`，分别配置 connect/read/write/pool timeout，并把默认 retry 从 3 次降为 1 次。
HTTPX 官方文档确认 `Client` 提供连接池和连接复用，并支持 connect/read/write/pool 四类独立
timeout：

- <https://www.python-httpx.org/advanced/clients/>
- <https://www.python-httpx.org/advanced/timeouts/>
- <https://www.python-httpx.org/advanced/resource-limits/>

Semantic Query 的低信息 skip 建议由 `app/semantic/` 内的版本化、确定性 eligibility policy
负责，并在 query builder 构造 canonical query 前执行。规则只命中明确的问候、确认词和
纯符号/emoji 输入；显式回忆、上下文依赖表达、问题和任何不在精确 allowlist 内的短输入
均不得 skip。

v0.1 只提供请求次数、attempt 次数、等待时间、执行时间、拒绝原因和进程内并发水位；
不扩张 `ChatModelClient.chat(...) -> str`、`ModelRequest` 或 `ModelResponse` 来强做精确 token
和金额核算。

## 1. 审查依据与当前事实

本设计已核对：

- `AGENTS.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `docs/error_handling.md`
- `docs/observability.md`
- `docs/fastapi_service.md`
- `docs/agents_communication/siliconflow_bge_m3_profile_approval_delivery.md`
- 当前 `app/config.py`、`app/main.py`、`app/services/`、`app/api/`、`app/model/`、
  `app/semantic/`、`app/embeddings/`、`app/errors/` 与对应测试。

当前获批基线以 T-0138 交付记录为准：`437 passed`。re-review 时工作树另有既存的
`docs/codex_cross_session_messaging.md` 修改，本 Worker 不读取、修改或回滚它。SiliconFlow
`siliconflow_pro_bge_m3_v1` 已获批；当前 active EvidenceProvider 仍是
`WorldInfoEvidenceProvider`；managed vector RAG 未接入对话热路径。

### 1.1 当前调用链

```text
CLI normal chat
  -> AgentService.handle_chat(...)
  -> DialogueController.handle_user_message(...)

FastAPI POST /v1/chat
  -> AuthService -> UserContext
  -> AgentService.handle_chat(...)
  -> DialogueController.handle_user_message(...)

DialogueController
  -> validate Session
  -> save current user message
  -> load recent messages
  -> SemanticQueryCoordinator
       -> shared SynchronizedEmbeddingProvider
  -> Memory / WorldInfo / Prompt
  -> ResponseGenerator
       -> ChatModelClient
       -> DeepSeekClient
  -> save assistant message / State / Memory decision

Memory Worker process
  -> build its own DeepSeekClient + Mock fallback
  -> MemoryExtractionService
  -> no explicit runtime owner/finally close today
```

### 1.2 已确认的问题

1. `AgentService` 当前直接调用 Controller，没有同 Session lock，也没有全局 turn
   admission。两个同 Session 请求可同时保存 user message、读取 recent messages、调用模型，
   从而造成上下文与 assistant message 顺序竞态。
2. FastAPI `POST /v1/chat` 是同步 route。多个请求可以由服务线程并发进入同一个
   `AgentService`；当前没有应用级上限或 API 限流。
3. `DeepSeekClient` 每次 attempt 都调用 `urllib.request.urlopen`，没有显式持久连接 client。
   timeout 固定为 30 秒，默认 `max_retries=3`，退避为 `0.5s, 1s, 2s`，且这些值没有接入
   `Settings`。
4. 当前最坏等待可近似为：

   ```text
   4 attempts * 30s + (0.5s + 1s + 2s) = 123.5s
   ```

   这与观察到的约 123.15 秒极端首轮高度吻合。独立复现首轮约 2.74 秒、第二轮约
   2.08 秒，说明不能把该问题简单归因为稳定冷启动。
5. `NeutralTurnQueryBuilderV01` 当前只对空输入和超出 semantic budget 的输入 skip；
   `早哇`、`好的`、`嗯`、纯 emoji 等仍会调用 Semantic Query/BGE。
6. Embedding 已有 `SynchronizedEmbeddingProvider`，它用进程内 `RLock` 串行完整 provider
   call；但等待无 timeout、无 busy 错误，也不输出 queue wait。该 Foundation 保证应保留，
   不应被新的 semaphore 替代或削弱。
7. `SemanticQueryCoordinator._state_lock` 当前也包住完整 `_resolve_requests(...)` 和 provider
   call。第二个 Semantic turn 会先无界等待 coordinator lock，单纯在 provider 外增加 semaphore
   不能约束这段前置等待。实现必须同时给 coordinator state-lock acquisition 增加有界等待，
   或经 Semantic/Embedding owner 批准后缩小锁范围；v0.1 推荐前者以保留已验收的 unhealthy
   状态串行语义。
8. Error Handling 已有稳定模型/Embedding timeout、rate-limit、unavailable 错误；但没有
   本地 runtime busy 和 API 自身 rate-limit 两个类别。
9. App Log 已存在且适合 admission/provider 边界；Trace 属于已开始的内部 turn；Prompt Log
   是显式敏感的本地 prompt 调试产物。三者不能合并。
10. `ChatRequestBody.message` 当前只有 `min_length=1`，没有字符/UTF-8 字节上限；metadata 也
    没有序列化大小上限。DeepSeek payload 没有 `max_tokens`，请求次数和并发上限不能阻止
    单个超长请求产生高额费用。
11. `build_agent_service()` 只返回 service/store/settings 等业务对象，DeepSeek HTTP client
    隐藏在 Controller/wrapper 内；独立 Memory Worker 还会自行创建模型 client。当前没有由
    composition root 显式登记、幂等逆序关闭的 runtime resource owner。
12. `JsonlAppLogger` 与 `JsonlTraceLogger` 没有实例写锁；并发线程可交错写 JSONL。logger
    写入异常当前也可能影响请求结果，尚未形成 App/Trace 一致的 best-effort 契约。
13. model fallback 尚未按稳定错误码白名单冻结；`X-Request-ID` 当前也缺少长度和安全字符
    校验，不能让任意外部 header 原样进入响应与日志。
14. CLI normal dialogue 与 `/memory extract` 当前共享同一个 `chat_model`；若只在共享实例外
    加入 allowlisted fallback，Memory Extraction 仍会在 DeepSeek 失败时进入 Mock，违反
    “仅 CLI normal dialogue 可 fallback”的用途边界。
15. 当前公开 Embedding Factory 只返回 `ObservableEmbeddingProvider`。concrete SiliconFlow
    provider 及其 lazy owned `httpx.Client` 被 wrapper 隐藏，而 `ObservableEmbeddingProvider` /
    `SynchronizedEmbeddingProvider` 均无公开 close；composition root 无法在不穿透 `_provider`
    或调用私有 `_create_provider()` 的情况下登记资源。
16. `JsonlPromptRunLogger` 与 App/Trace JSONL 一样是共享实例，但当前没有写锁。CLI 默认启用
    Prompt Log，并发 turn 可能交错 JSONL 行；写锁必须覆盖它，但其 failure-safe 行为保持不变。
17. `SiliconFlowEmbeddingProvider._get_client()` 当前没有资源状态锁。bundle close 若发生在首次
    embed 前，后续错误调用仍可创建无人关闭的 client；lazy factory 与 close 并发也可能在关闭
    检查后才发布 client。外层 `SynchronizedEmbeddingProvider` 的锁不覆盖 bundle close，不能
    解决 concrete resource race。
18. Factory 返回类型变化的实际 owners 包括 `app/embeddings/qualify.py` 和
    `app/retrieval/manage_rag_index.py`；`app/embeddings/qualification.py` 只是纯 Port consumer。
    现有迁移表把 ownership 放错层并遗漏公开导出、RAG build/query 和多处测试调用点。

## 2. 范围与非目标

### 2.1 本任务负责

- 单进程内同 Session 串行化。
- 单进程内 dialogue turn、LLM logical request、必要 Embedding request 的全局上限。
- FastAPI `POST /v1/chat` 的 user/IP 双层限流。
- DeepSeek 持久 client、分相 timeout、重试矩阵和安全 transport 观测。
- 请求输入/metadata 硬上限和 DeepSeek 最大输出 token 上限。
- 进程内 runtime resource 的显式所有权、幂等逆序关闭。
- dialogue/extraction 模型用途隔离，同时复用同一 surface 内的 LLM gate/DeepSeek transport。
- Embedding Factory 的公开 provider/resource bundle 与 owned client 关闭。
- Semantic Query 低信息输入的确定性 skip。
- 新 guardrail 错误、HTTP status、`Retry-After`、Request ID 校验和安全 App/Trace Log。
- 请求/attempt/等待/执行/并发水位统计。

### 2.2 明确不负责

- 不修改 `DialogueController` 的业务策略。
- 不修改 Character、Memory scope/provenance、RAG 索引、Persistence schema。
- 不切换 active `WorldInfoEvidenceProvider`。
- 不改变 FastAPI 默认 `AEMEATH_API_ENABLE_MODEL_FALLBACK=false`。
- 不引入 Redis、PostgreSQL、消息队列、独立计费服务、LangGraph、MCP、Web UI 或 Streaming。
- 不增加跨进程、跨 Uvicorn worker 或跨节点强保证。
- 不把精确 token/金额计费塞入本任务。
- 不读取、记录、改写或提交本地 `.env` 密钥。

## 3. 推荐模块边界

```text
app/runtime/guardrails/
  schemas.py        # admission/rate/concurrency DTO 与稳定 reason
  ports.py          # DialogueAdmissionPort / ApiRateLimiterPort
  admission.py      # SessionLockRegistry + global dialogue semaphore
  concurrency.py    # generic gate + model/embedding wrappers
  rate_limit.py     # in-memory token bucket
  errors.py         # RuntimeBusyError / ApiRateLimitedError
  observability.py  # allowlisted App Log helpers

app/runtime/
  resources.py      # ApplicationRuntime / RuntimeResources lifecycle owner

app/observability/
  safe.py           # SafeAppLogger / SafeTraceLogger best-effort wrappers

app/embeddings/
  runtime.py        # EmbeddingProviderRuntime public provider/resource bundle

app/semantic/
  eligibility.py    # deterministic low-information policy v0.1
```

边界原则：

- `AgentService` 只执行输入 hard-limit、应用级 admission 和 DTO 转换；不吸收 Character、
  Memory、RAG、Prompt、Model 或 State 策略。
- `DialogueController` 调用次序、业务判断和现有 trace 不变。
- `LimitedChatModelClient` 仍实现现有 `ChatModelClient`。
- fallback 是按 `surface + purpose` 组合出来的 adapter，不是共享 DeepSeek transport 的属性。
- `LimitedEmbeddingProvider` 仍实现现有 `EmbeddingProviderPort`，不改变 signature、batch、
  vector 或 error contract。
- HTTP rate limiter 只由 `app/api/` adapter 调用；CLI 不依赖 IP、header 或 FastAPI。
- Semantic eligibility 在 `app/semantic/` 内完成；Controller 只收到最终
  `TurnSemanticContext`。

## 4. 推荐流程图

```text
FastAPI POST /v1/chat
  -> validate-or-generate request_id
  -> Auth -> UserContext
  -> API token buckets
       user_id bucket (primary)
       client IP bucket (auxiliary, in-memory only)
       reject -> API_RATE_LIMITED / 429 / Retry-After
  -> AgentService

CLI normal chat
  -> AgentService                    # no HTTP/IP rate limiter

CLI /memory extract
  -> MemoryAdminService              # no dialogue admission, shares CLI LLM gate

API/internal memory extraction
  -> MemoryExtractionService         # no fallback, shares API LLM gate

AgentService
  -> ChatInputLimitPolicy.validate(message, metadata)
       reject -> INVALID_INPUT, before Controller/persistence/provider
  -> SessionLockRegistry.acquire((user_id, session_id), timeout=5s)
       reject -> RUNTIME_BUSY / session_lock_wait_timeout
  -> dialogue semaphore.acquire(timeout=0.25s)
       reject -> RUNTIME_BUSY / dialogue_concurrency
  -> DialogueController              # unchanged
       -> Semantic eligibility
            explicit/context/question guard -> eligible
            exact low-info rule       -> skipped, no BGE
            otherwise                 -> eligible
       -> LimitedEmbeddingProvider
            embedding semaphore -> existing synchronized provider
       -> LimitedChatModelClient
            shared LLM gate -> purpose-specific delegate graph
            -> persistent httpx.Client, payload includes max_tokens
  <- release dialogue semaphore in finally
  <- release Session lock in finally
```

锁顺序固定为 Session lock 后 dialogue semaphore；释放顺序相反。这样，同 Session 的第二个
请求等待时不会占用全局 turn permit。锁顺序不得在其他入口反转。

### 4.1 Model composition：surface 与 purpose 双维度

`build_application_runtime(..., surface=Literal["cli", "api"])` 必须显式接收 surface；禁止根据
“当前是否有 HTTP request”、Controller 类型或 wrapper 私有字段推断。真实 DeepSeek 模式的
冻结组合如下：

```text
CLI runtime
  shared_deepseek = DeepSeekClient(shared_httpx_client)
  shared_llm_gate = ConcurrencyGate(limit=2)

  dialogue_model = LimitedChatModelClient(
      gate=shared_llm_gate,
      delegate=FallbackChatModelClient(
          primary=shared_deepseek,
          fallback=MockChatModelClient(),
          allowed_codes=CLI_DIALOGUE_FALLBACK_CODES,
      ),
  )

  extraction_model = LimitedChatModelClient(
      gate=shared_llm_gate,
      delegate=shared_deepseek,
  )

API runtime
  shared_deepseek = DeepSeekClient(shared_httpx_client)
  shared_llm_gate = ConcurrencyGate(limit=2)
  dialogue_model   = LimitedChatModelClient(shared_llm_gate, shared_deepseek)
  extraction_model = LimitedChatModelClient(shared_llm_gate, shared_deepseek)

Memory Worker process
  worker_deepseek = DeepSeekClient(worker_httpx_client)
  worker_llm_gate = ConcurrencyGate(limit=2)
  extraction_model = LimitedChatModelClient(worker_llm_gate, worker_deepseek)
```

两个 `LimitedChatModelClient` 可以共享同一个 gate 和 DeepSeek delegate，但不得共享 CLI
dialogue 的 fallback wrapper。`ResponseGenerator` 只接收 `dialogue_model`；
`MemoryExtractionService` 只接收 `extraction_model`。`MemoryAdminService` 通过其已有 extraction
service 间接使用后者，不得回退到 `chat_model` 变量。

`AEMEATH_USE_MOCK_LLM=true` 的显式离线/mock 运行模式不是 fallback：该模式可以继续为 dialogue
和 extraction 主动构造各自的 mock client，且不会先调用 DeepSeek。本节限制的是“真实 primary
失败后自动切换 Mock”的 fallback 行为。

`surface="api"` 不得安装 fallback。现有默认
`AEMEATH_API_ENABLE_MODEL_FALLBACK=false` 保持；若显式配置为 `true`，v0.1 应在创建 client 前
fail-fast 为 `CONFIG_INVALID` / `api_model_fallback_not_supported`，不得静默忽略，也不得绕过
surface matrix。CLI 的 `AEMEATH_ENABLE_MODEL_FALLBACK` 只控制 `dialogue_model`。

生命周期与业务调用链正交：

```text
composition root
  -> build_application_runtime(settings, surface="cli" | "api")
  -> RuntimeResources.register(owned DeepSeek/EmbeddingProviderRuntime/...)
  -> ApplicationRuntime(agent_service, resources)
       -> FastAPI lifespan finally -> runtime.close()
       -> CLI outer finally        -> runtime.close()

Memory Worker composition
  -> RuntimeResources.register(owned DeepSeek/EmbeddingProviderRuntime/...)
  -> worker outer finally          -> resources.close()
```

## 5. Port 与 DTO 草案

### 5.1 Dialogue admission

```python
@dataclass(frozen=True)
class DialogueAdmissionRequest:
    request_id: str
    user_id: str
    session_id: str
    character_id: str


@dataclass(frozen=True)
class DialogueAdmissionLease:
    request_id: str
    session_id: str
    session_wait_ms: float
    dialogue_wait_ms: float
    total_wait_ms: float
    active_dialogue_turns: int
    dialogue_limit: int


class DialogueAdmissionPort(Protocol):
    def admit(
        self,
        *,
        request: DialogueAdmissionRequest,
    ) -> ContextManager[DialogueAdmissionLease]:
        ...
```

`AgentService.handle_chat(...)` 的推荐形态：

```python
request_id = user_context.request_id or new_id("req")
with admission.admit(request=admission_request) as lease:
    output = controller.handle_user_message(turn_input)
```

API 继续复用 middleware 生成的 request id；CLI 没有 request id 时由 Service 生成一个
operation-scoped id，并传入现有 turn metadata 和 App Log。

### 5.2 通用 concurrency gate

```python
@dataclass(frozen=True)
class ConcurrencyLease:
    resource: Literal["dialogue", "llm", "embedding"]
    wait_ms: float
    active_count: int
    limit: int


class ConcurrencyGate:
    def acquire(
        self,
        *,
        timeout_seconds: float,
        limit_reason: str,
    ) -> ContextManager[ConcurrencyLease]:
        ...
```

`ConcurrencyGate` 是 runtime 实现细节，不进入 Controller ports。`LimitedChatModelClient`
和 `LimitedEmbeddingProvider` 组合它，并原样转发调用结果/异常。

### 5.3 API rate-limit DTO

```python
@dataclass(frozen=True)
class ApiRateLimitRequest:
    request_id: str
    user_id: str
    client_ip: str | None = field(repr=False)
    operation: Literal["chat"] = "chat"


@dataclass(frozen=True)
class ApiRateLimitDecision:
    allowed: bool
    limit_reason: Literal["none", "user_rate", "ip_rate"]
    retry_after_seconds: int | None
    remaining_user_tokens: float | None
    remaining_ip_tokens: float | None


class ApiRateLimiterPort(Protocol):
    def check(self, *, request: ApiRateLimitRequest) -> ApiRateLimitDecision:
        ...
```

API adapter 在 `allowed=False` 时抛出 `ApiRateLimitedError`。raw IP 只作为进程内 key，
不进入 DTO repr、错误响应或日志 payload。

### 5.4 Semantic eligibility DTO/Port

```python
@dataclass(frozen=True)
class SemanticEligibilityDecision:
    eligible: bool
    rule_version: str
    reason_code: str


class SemanticQueryEligibilityPolicyPort(Protocol):
    def evaluate(
        self,
        *,
        turn_input: SemanticTurnInput,
    ) -> SemanticEligibilityDecision:
        ...
```

默认实现为 `DeterministicSemanticEligibilityPolicyV01`。它由
`NeutralTurnQueryBuilderV01` 调用；`SemanticQueryCoordinatorPort` 的公开调用形态不变。

`TurnSemanticContext.safe_summary()` 可增加 `eligibility_version`，但不得增加输入正文、
canonical query、allowlist 命中的原文或 vector。

### 5.5 输入上限 DTO/Port

```python
@dataclass(frozen=True)
class ChatInputLimitRequest:
    message: str = field(repr=False)
    metadata: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True)
class ChatInputLimitDecision:
    allowed: bool
    reason_code: str
    message_char_count: int
    message_utf8_bytes: int
    metadata_utf8_bytes: int | None


class ChatInputLimitPolicyPort(Protocol):
    def validate(self, *, request: ChatInputLimitRequest) -> ChatInputLimitDecision:
        ...
```

默认实现 `ChatInputLimitPolicyV01` 由 `AgentService` 在 admission 和 Controller 调用之前执行，
因此 CLI 与 FastAPI 共用同一硬上限。HTTP schema 可以添加同值的早期校验改善响应速度，但它
不是权威边界，不能让 CLI、测试入口或未来非 HTTP adapter 绕过 Service 校验。

### 5.6 Runtime 生命周期 DTO

```python
@dataclass
class ApplicationRuntime:
    surface: Literal["cli", "api"]
    agent_service: AgentService
    memory_admin_service: MemoryAdminService
    store: object
    settings: Settings
    resources: RuntimeResources

    def close(self) -> tuple[ResourceCloseFailure, ...]:
        return self.resources.close()


class RuntimeResources:
    def register(self, *, name: str, close: Callable[[], None]) -> None: ...
    def close(self) -> tuple[ResourceCloseFailure, ...]: ...


def build_application_runtime(
    settings: Settings | None = None,
    *,
    surface: Literal["cli", "api"],
) -> ApplicationRuntime: ...
```

`ApplicationRuntime` 是 composition root 的返回对象，不是 Controller port。它显式暴露服务与
生命周期；调用方不得通过 Controller、fallback wrapper 或 provider wrapper 的内部字段找
client。独立 Memory Worker 可使用相同 `RuntimeResources`，但不需要持有完整
`ApplicationRuntime`。

`surface` 是必填 keyword-only 参数，未知值在创建任何外部 client 前以 `CONFIG_INVALID`
fail-fast。CLI 只能请求 `surface="cli"`，FastAPI 只能请求 `surface="api"`；不得把可变的
`settings.enable_model_fallback` 当作 surface 识别手段。

### 5.7 Embedding Factory resource bundle

```python
@dataclass
class EmbeddingProviderRuntime:
    provider: EmbeddingProviderPort
    _close: Callable[[], None] = field(repr=False)

    def close(self) -> None:
        ...  # thread-safe and idempotent


def create_embedding_provider(...) -> EmbeddingProviderRuntime: ...
def create_qualification_provider(...) -> EmbeddingProviderRuntime: ...
def create_legacy_embedding_provider(...) -> EmbeddingProviderRuntime: ...
```

#### 5.7.1 Bundle 与 Factory 原子迁移

Factory 在仍持有 concrete provider 局部引用时构造 `ObservableEmbeddingProvider`，并把
`provider=observable` 与捕获 concrete public `close()` 的幂等 callback 一起返回。bundle
不公开 concrete provider，不要求 Observable/Synchronized/Limited wrapper 增加 close，也不让
composition root 遍历 `_provider`。没有可关闭资源的 local/hash provider 使用 no-op close。

bundle 自身用 lock/state 保证 close at-most-once；状态额外记录 closing thread，同线程从
delegate `close()` 重入 bundle `close()` 时立即返回，其他线程才等待 `CLOSED`。delegate close 失败可以向
`RuntimeResources` 抛出并由其安全聚合，但 bundle 仍进入 closed，不能通过重复 shutdown 重试
未知副作用。若 concrete 已创建而 Observable/bundle 构造失败，Factory 必须在传播原始构造
异常前 best-effort 关闭 owned concrete，且关闭异常不覆盖原异常。

三个既有公开 Factory 在同一提交中原子切换为返回 bundle。不得保留能创建远程 client 却只
返回 `EmbeddingProviderPort` 的兼容路径；若旧函数名保留一个发布周期，它也返回 bundle 并发出
`DeprecationWarning`，不能实现 `embed()` 转发来伪装旧 Provider 类型。`app/embeddings/__init__.py`
公开导出 `EmbeddingProviderRuntime` 及更新后的 Factory。

Memory adapter 同步改为：

```python
def build_memory_embedding_runtime(...) -> EmbeddingProviderRuntime: ...
```

composition root 先取得 bundle，立即 `resources.register(name="embedding_provider",
close=bundle.close)`，再把 `bundle.provider` 组合成
`LimitedEmbeddingProvider(SynchronizedEmbeddingProvider(bundle.provider))`。

正常 runtime Factory 必须继续按现有顺序重新加载 canonical Profile、比较完整 profile 对象并
要求 approved 后才创建 provider；bundle 不能绕过 approval gate。Hash 仍要求显式
`allow_hash=true`，不得成为 fallback。wrapper 顺序保持
`Limited(Synchronized(Observable(concrete)))`，不改变 batch、retry、diagnostics、provider
request ID、单终态事件、signature、vector 或 active EvidenceProvider。

#### 5.7.2 SiliconFlow lazy client 与 close 并发状态机

`SiliconFlowEmbeddingProvider` concrete 自己持有一个以 `threading.Lock` 为底层的
`Condition`、`_resource_state: OPEN | CLOSING | CLOSED` 和 `_active_calls`；bundle lock 只保证
bundle API at-most-once，不能替代 concrete resource state。冻结 ownership：

| 构造方式 | client owner | provider close 行为 |
| --- | --- | --- |
| `client=<instance>` | 注入方 | provider 标记 closed，但不调用 injected client.close |
| `client_factory=<callable>` | provider | 首次 embed 创建，provider close 一次 |
| 两者都未提供 | provider | lazy `httpx.Client()`，provider close 一次 |
| 两者同时提供 | 无效 | 构造时 `EmbeddingConfigurationError("siliconflow_client_ownership_ambiguous")` |

原子规则：

1. `embed()` 在任何 validate、空 batch 快速返回、`_load_httpx()` 或 client 创建前进入 condition。
   只有 `OPEN` 可继续；`CLOSING/CLOSED` 均立即抛现有
   `EmbeddingConfigurationError("siliconflow_provider_closed")`。该错误非 retryable，且不产生
   provider request。
2. `_get_client_locked()` 只允许在 condition 内调用。它再次检查 state，然后在同一临界区内
   调用 `client_factory()` 或创建默认 `httpx.Client()`，最后原子发布 `_client`。创建成功后
   `_active_calls += 1`，embed 才释放 condition 并执行 batch/retry/diagnostics。业务成功或异常均
   在 finally 重新进入 condition、递减 active count 并 `notify_all()`。
3. 首个 `close()` 在 condition 内执行 `OPEN -> CLOSING`，阻止新 embed，然后用 condition wait
   等待 `_active_calls == 0`。它取出 owned `_client` 并从 provider detach；injected client 不进入
   待关闭变量。随后释放 condition，再调用 owned client 的 public `close()`，避免持锁执行未知
   外部代码。
4. client close 成功或抛错后，首个 closer 都在 finally 中重新进入 condition、切换为 `CLOSED`
   并 `notify_all()`；其他看到 `CLOSING` 的 close 调用等待 `CLOSED` 后返回，不重复关闭。
5. close 先于首次 embed 时不创建 client；之后任何 embed（包括空 batch）都稳定拒绝。close 与
   lazy factory 并发时只有两种合法结果：factory/已登记 active call 先完成后 client 被 close
   一次，或 close 先切换 `CLOSING` 且 factory 永不调用；不得出现关闭后发布的新 client。
6. owned client.close 抛错时 concrete 和 bundle 都保持 closed，不重试 close、不恢复 client；
   首个 closer 把错误交给 owner 按 §5.7.4 聚合。所有 condition/lock acquisition 都通过 context
   manager，factory/embed/close 异常不得泄漏 active count 或 waiter。

该 concrete lock 与外层 `SynchronizedEmbeddingProvider` 目的不同：外层维持现有完整 embedding
调用串行语义，concrete lock 额外把生命周期 close 纳入互斥。不得删除或合并任一层保证。

#### 5.7.3 调用点 ownership 迁移

| Caller | 迁移后职责 |
| --- | --- |
| `app/memory/runtime.py` | `build_memory_embedding_runtime()` 返回 bundle，不丢弃 close handle |
| `app/main.py` | 拥有 bundle，登记 `bundle.close`，只向所有 Memory/Semantic consumers 分发同一个最外层 provider |
| `app/memory/worker.py` | 拥有 bundle；extraction 与 repair 共用同一个最外层 wrapper；最外层 finally 关闭 |
| `app/embeddings/qualify.py` | 两个 Factory 调用都取得 bundle；只把 `.provider` 传给 qualification 函数，finally 关闭 |
| `app/embeddings/qualification.py` | 保持纯 `EmbeddingProviderPort` consumer，不导入/关闭 bundle，不列入实现修改 |
| `app/retrieval/manage_rag_index.py` | build/query 各自取得 bundle、使用 `.provider` 并在 finally 关闭；status 不调用 Factory |
| `app/embeddings/__init__.py` | 导出 `EmbeddingProviderRuntime` 和返回 bundle 的公开 Factory |
| tests/fixtures | 每个直接 Factory caller 显式持有 bundle；使用 `.provider`，并验证或执行 close |

`manage_rag_index` 的迁移只增加资源生命周期：build 的 `RagGenerationBuilder` 和 query 的
`DirectQueryEmbeddingSource` 接收 `.provider`，不改变 RAG index/schema/activation/query contract，
也不切换 active EvidenceProvider。旧 `build_wuwa_rag.py`、`query_wuwa_rag.py`、`rag_cli.py`
直接创建 local/hash concrete 且不调用 Foundation Factory，本次不顺带重写。

#### 5.7.4 standalone CLI close 失败优先级

ApplicationRuntime 与 Memory Worker 继续使用 `RuntimeResources`。`qualify.py` 和
`manage_rag_index.py` 可不依赖 application runtime，但必须实现相同的异常优先级：

1. 进入业务调用前保存 bundle；成功、普通异常、`KeyboardInterrupt` / `SystemExit` 都执行
   finally close。
2. 若已有 primary error，close failure 只能产生安全摘要并保留原 error/traceback/退出语义；
   不得在 finally 中以 close exception 覆盖 primary error。
3. 若业务成功但 close 失败，命令返回非零，并映射为现有
   `EmbeddingUnavailableError(reason="provider_close_failed")` 或等价安全 terminal outcome。
4. 安全摘要只含 `resource_name="embedding_provider"`、provider/profile id（已 allowlist 时）、
   `error_type` 和稳定 reason；禁止 exception message/repr、query、candidate/vector、response body
   或 credential。logger 失败仍不得覆盖 primary error。

这是对 Embedding Foundation 公开 Factory 返回契约的破坏性变更，必须在同一提交完成全部内部
调用点迁移。完成本节修订后仍需 Foundation owner 只读确认；确认不授权调用私有 factory 或
wrapper 字段，也不改变 Embedding Port、Profile、signature 或 active provider。

## 6. Session 锁的精确定义

### 6.1 Lock key

冻结 key：认证并规范化后的 `(user_id, session_id)` 二元组；两部分都必须是非空字符串。

理由：合法请求始终从认证后的 `UserContext.user_id` 取第一部分，而不是接受 body/header
自报 user id。同一用户、同一 Session 的所有 turn 仍严格串行；另一个用户即使知道某个
`session_id`，其非法请求也不能占用合法用户的锁。Session/character 所有权仍由 Controller
当前已有的 `ensure_session(...)` 校验，admission 不复制 Session policy。`character_id` 不进入
key，避免同用户对同 Session 携带不同 character 的冲突请求绕过串行化。

该复合 key 是 Manager 已批准的 v0.1 语义，不再保留全局单 `session_id` key 选项。

### 6.2 保护区间

锁覆盖整个 `AgentService -> DialogueController` 调用，包括：

- Session 校验；
- 当前 user message 写入；
- recent history 读取；
- Semantic/Memory/WorldInfo/Prompt/Model；
- assistant message、State 与 Memory decision 写入；
- 异常传播和 terminal App Log。

因此第二个同 Session turn 不会在第一个 turn 未完成时构造历史上下文。

### 6.3 等待、失败和公平性

- 默认最多等待 5 秒。
- 5 秒内获得锁：继续执行并记录 `session_wait_ms`。
- 超时：在 Controller 运行前抛 `RUNTIME_BUSY`，reason 为
  `session_lock_wait_timeout`，不写消息、不写 prompt log、不更新 State/Memory。
- v0.1 只保证 mutual exclusion，不承诺 FIFO 公平性。
- 客户端重试不等于幂等；本任务不新增 idempotency key。

### 6.4 异常安全和释放

锁只通过 context manager 使用，任何成功、provider 异常、日志异常或 Controller 异常都在
`finally` 释放。不得暴露手动 acquire 后由调用方自行 release 的 API。

### 6.5 闲置锁清理

`SessionLockRegistry` entry 至少包含：

```text
lock
reference_count     # holder + 已登记 waiter
last_used_monotonic
```

精确规则：

1. 在 registry mutex 内查找/创建 entry，并在等待实体锁前增加 `reference_count`。
2. acquire 超时或 lease 释放后，在 registry mutex 内减少计数。
3. 只有 `reference_count == 0` 且 dict 中仍是同一 entry 对象时立即删除。
4. 不基于墙钟强制释放一个仍在持有或等待的锁；hung holder 只能靠请求终止/进程重启解决。
5. 可有低频 sweep 作为防御，但 sweep 也只能删除零引用 entry。正常路径应立即清理，
   不依赖后台线程。

该引用计数规则防止“旧 entry 被删除后，新请求创建第二把同 key lock”的双锁竞态。

## 7. 全局并发的精确定义

### 7.1 推荐默认值

| Resource | 进程内上限 | 等待策略 | 超时后的错误 |
| --- | ---: | --- | --- |
| dialogue turn | 2 | 最多等待 0.25s，随后快速失败 | `RUNTIME_BUSY`, `dialogue_concurrency` |
| LLM logical request | 2 | 最多等待 2s | `RUNTIME_BUSY`, `llm_concurrency` |
| Embedding logical request | 1 | 最多等待 2s | `RUNTIME_BUSY`, `embedding_concurrency` |

这些是单实例、小规模、非 streaming 的保守起点，不是 provider SLA。上线后只根据安全的
等待/执行/拒绝统计调整。

### 7.2 dialogue semaphore

- 所在层：`DialogueAdmissionPort`，由 `AgentService` 调用。
- 定义：“active dialogue turn”是已经获得 Session lock 和 dialogue permit、但尚未从
  Controller 返回/抛出的 turn。
- service busy 采用“极短有界等待 + 快速失败”，避免 HTTP 工作线程长时间排队。
- admission 失败发生在 Controller 前，因此不产生 turn 业务副作用。

### 7.3 LLM semaphore

- 所在层：`LimitedChatModelClient` wrapper，不放在 `DialogueController` 或 DeepSeek
  transport 内。
- 定义：一次进入 `ChatModelClient.chat(...)` 到返回/抛出的 logical request。
- 组合顺序冻结为 §4.1 的 surface/purpose matrix。CLI dialogue 使用
  `Limited(shared_gate, Fallback(DeepSeek, Mock))`；CLI extraction 使用
  `Limited(shared_gate, DeepSeek)`。API dialogue/extraction 同样直接使用 DeepSeek；Memory Worker
  使用自己的 worker gate 和 DeepSeek。
- 同一 runtime 的 dialogue/extraction wrapper 必须引用同一个 `ConcurrencyGate` 对象，而不只是
  两个 limit 值相同的 gate；这样两种用途合计的 active logical requests 永不超过 2。
- CLI dialogue 中 gate 在 fallback 外层，因此 primary 到 allowlisted mock fallback 的整个
  logical call 只占一个 permit；extraction graph 根本不包含 fallback delegate。
- DeepSeek 内部 retry attempt 不重复 acquire semaphore，因为 permit 应覆盖整个 logical
  request 的 retry budget。
- `MemoryExtractionService` 禁止复用 `dialogue_model`，只接收独立 `extraction_model`；二者通过
  共享 gate 和 DeepSeek transport 复用容量/连接，而不是通过共享 fallback wrapper 复用。

### 7.4 Embedding semaphore

- 所在层：`LimitedEmbeddingProvider`，包住当前完整同步 provider。
- 推荐组合顺序：

  ```text
  embedding_runtime = create_embedding_provider(...)  # public bundle
  resources.register(embedding_runtime.close)

  LimitedEmbeddingProvider(
    SynchronizedEmbeddingProvider(
      embedding_runtime.provider                       # Observable outer provider
    )
  )
  ```

- v0.1 上限为 1，与现有 Foundation 的完整调用串行保证一致；新 wrapper 增加有界等待、busy
  错误和 queue wait 观测，不删除 `SynchronizedEmbeddingProvider`。
- `SemanticQueryCoordinator` 的现有 `_state_lock` 位于 provider wrapper 之前。v0.1 推荐保留
  它对完整 resolution/unhealthy transition 的保护范围，但把 acquisition 改为最多等待
  `AEMEATH_RUNTIME_EMBEDDING_WAIT_TIMEOUT_SECONDS`。超时产生 `RUNTIME_BUSY` /
  `embedding_concurrency`，并由 coordinator 作为当前 turn 的 transient semantic failure
  处理；它不得把 coordinator 标为 unhealthy，也不得阻断 Memory 的 keyword/显式 recall
  fallback。
- state lock 或 outer gate 在 delegate 调用前超时时，semantic safe summary 必须记录
  `provider_called=false` 和 `reason_code=embedding_local_capacity`，对应 admission App Log
  记录 `attempt_count=0`；只有请求实际进入 provider 后的 upstream timeout 才记录
  `provider_called=true`。
- coordinator state lock wait 和 outer provider gate wait 是两个不同阶段，分别最多 2 秒；若
  state lock 获得后恰好又与其他非 Semantic Embedding 调用竞争，v0.1 最坏 pre-provider
  queue 为约 4 秒。两段等待必须分别记录，不能把表中的 2 秒谎报成整个 Semantic 请求的
  total deadline。
- 所有业务调用方只能收到最外层 `LimitedEmbeddingProvider`，避免绕过有界 gate 后在内层
  `SynchronizedEmbeddingProvider` 无界等待。composition root 额外持有的
  `EmbeddingProviderRuntime` 只作为资源 handle，不传给 Semantic/Memory consumer。
- Semantic low-info skip 在进入 wrapper 前发生，因此 skip 不消耗 permit、不产生 provider
  请求和费用。
- 当前进程内共享的 Semantic、Memory recall、同步 Memory extraction 受同一上限约束。
- 独立 `python -m app.memory.worker` 是另一个进程，不受 API/CLI 进程 semaphore 约束；这是
  v0.1 明确限制，不应宣称为全机器全局上限。

### 7.5 semaphore 异常安全

所有 permit 只能由 context manager 管理；provider 抛错、fallback、解析失败和 AppLogger
失败均必须在 `finally` 释放。日志失败仍保持 best-effort，不得吞掉或改变业务异常。

## 8. API 限流设计

### 8.1 作用范围与顺序

v0.1 只限流昂贵的 `POST /v1/chat`：

```text
request id -> Auth -> user/IP limiter -> AgentService admission
```

`GET /health`、Session 列表/读取本轮不受 chat 额度影响。CLI 不调用 API limiter，但仍共享
AgentService 的 Session/global concurrency guard。

### 8.2 算法、窗口与 burst

采用进程内连续补充 token bucket，使用 `time.monotonic()`：

| Key | Sustained rate | Bucket capacity / burst | 等效窗口 |
| --- | ---: | ---: | ---: |
| authenticated `user_id` | 12 requests/minute | 4 | 60s |
| connection IP | 60 requests/minute | 20 | 60s |

含义：用户最多立即发 4 个请求，之后平均每 5 秒恢复 1 个 token；IP bucket 用于限制同一
来源批量轮换用户，但阈值更宽，避免 NAT 下轻易误伤。

一次请求必须同时通过两个 bucket。检查和扣减在同一个 limiter mutex 内原子完成：若任一
bucket 拒绝，另一个 bucket 不扣 token。

### 8.3 Retry-After

- 对两个 bucket 分别计算恢复 1 token 所需秒数。
- 返回两者中较大的值，向上取整且最小为 1。
- API 返回 `429`、稳定 `API_RATE_LIMITED` 和 `Retry-After: <seconds>`。
- response body 仍只使用 `ErrorInfo.to_public_dict()`；不暴露当前 token、raw IP 或 limiter
  内部 key。

### 8.4 IP key 与代理边界

- v0.1 limiter 使用 ASGI `request.client.host`；应用代码不自行解析 `X-Forwarded-For` 或
  `Forwarded`。
- 但 `request.client.host` 不一定是 socket peer：Uvicorn 在启用 proxy headers 且直接连接方
  命中 `forwarded-allow-ips` 时，可根据 forwarded header 改写 client host。因此 IP limiter 的
  安全性依赖部署层的可信代理配置，不能描述成“永远忽略 X-Forwarded-For”。
- raw IP 只存在内存 bucket key 中，不写入 App Log、Trace、Prompt Log 或错误 metadata。
- 无反向代理时，推荐关闭 proxy headers：`uvicorn ... --no-proxy-headers`。
- 位于反向代理后时，必须显式配置 `--proxy-headers --forwarded-allow-ips=<明确代理
  IP/CIDR/Unix socket>`；禁止 `--forwarded-allow-ips="*"`，也禁止把不受控客户端网段列为
  trusted proxy。代理必须丢弃客户端自带 forwarded headers 后写入自己的可信值。
- 若部署无法满足上述条件，IP bucket 只能视为弱辅助信号；`user_id` bucket 仍是主限流。

Uvicorn 官方 settings/deployment 文档明确说明 proxy headers 的启用和信任列表，以及受信
header 会改写 client address：

- <https://www.uvicorn.org/settings/>
- <https://www.uvicorn.org/deployment/>

### 8.5 bucket 清理

- bucket 记录 `tokens` 与 `last_seen_monotonic`。
- 每 30 秒至多触发一次常规 sweep，每次最多检查并删除 128 个最旧 bucket；若仍有 stale backlog，
  下一请求可继续一个 bounded batch，不做全表扫描。
- 默认 idle TTL 为 600 秒，只删除超过 TTL 的 idle bucket。
- sweep 与 check 共用 mutex，避免同 key 双 bucket 竞态。
- user 与 IP 同时受限时，`Retry-After` 取两者等待时间的最大值；reason 仍稳定采用 user 主限流
  原因。

### 8.6 外部 Request ID 校验

`X-Request-ID` 是不可信输入。API middleware 只接受满足以下完整正则且总长度不超过 128 的
值，不做 `strip()` 或 Unicode normalization：

```text
^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$
```

- header 缺失：生成内部 `req_*`。
- header 非法、为空、超过 128、含空白/控制字符/非 ASCII：丢弃并生成内部 `req_*`。
- 响应 header、错误 body 和日志只使用最终 resolved request id；非法原始值不得回显、记录、
  hash 或进入 exception metadata。
- 可记录安全布尔字段 `external_request_id_accepted`，但不能记录 rejection 原值。
- CLI 始终由 Service 生成 `req_*`，不受 HTTP header 规则影响。

## 9. DeepSeek transport 设计

### 9.1 `httpx.Client` 结论

建议替换 `urllib.request`。一个 `DeepSeekClient` 拥有或注入一个持久化 `httpx.Client`：

- 复用 TCP/TLS 连接，减少重复握手；
- 能明确区分 connect/read/write/pool timeout；
- 可配置连接池上限，与 LLM concurrency cap 对齐；
- 测试可注入 `MockTransport` 或 fake client；
- SSL verification 保持默认开启；不得为了规避延迟而设 `verify=False`。

`httpx` 当前只在 `dev` / `embedding-api` extras 中；若 DeepSeek 正式迁移，建议把
`httpx>=0.27,<1` 提升为基础 runtime dependency，避免 CLI 真实模型模式缺依赖。

### 9.2 推荐 client 生命周期

- composition root 每个进程创建一个 DeepSeek client，并在创建时把该具体引用登记到
  `RuntimeResources`；不得事后遍历 Controller/wrapper 查找。
- 同一 application runtime 的 dialogue/extraction wrapper 可共享该 DeepSeek client；client 只
  保存 immutable 配置/owned HTTP client，每次调用的 payload、attempt count、duration 和错误
  诊断必须是局部变量，不得写入共享 `last_*` 状态。
- `DeepSeekClient.close()` 幂等关闭自己拥有的 HTTP client；注入的测试 client 所有权由
  构造参数明确。
- FastAPI lifespan、CLI `finally` 和 Memory Worker `finally` 调用各自的
  `RuntimeResources.close()`。
- 不在每次 `chat()` 内创建或关闭 client。

### 9.3 timeout 默认值

| Phase | 推荐默认值 | 语义 |
| --- | ---: | --- |
| connect | 3s | 建立 TCP/TLS 连接的最长等待 |
| read | 15s | 等待下一个 response data chunk 的最长静默时间 |
| write | 5s | 写出 request body chunk 的最长等待 |
| pool | 1s | 从 HTTP connection pool 获取连接的最长等待 |

连接池建议：

```text
max_connections = AEMEATH_RUNTIME_MAX_LLM_REQUESTS (default 2)
max_keepalive_connections = same value
keepalive_expiry = 30s
```

HTTPX read timeout 是 data-chunk inactivity timeout，不是严格的整请求 wall-clock deadline。
v0.1 通过 15 秒 read timeout、默认不重试 ambiguous read/write timeout、以及最多 1 次合格
retry，消除当前 123.5 秒的固定最坏路径。严格 total deadline 与 cancellation 留作 Deferred。

### 9.4 retry 配置

```text
DEEPSEEK_MAX_RETRIES=1              # 初始请求后最多再发 1 次
DEEPSEEK_RETRY_BACKOFF_SECONDS=0.25
DEEPSEEK_RETRY_BACKOFF_MAX_SECONDS=2
DEEPSEEK_RETRY_AFTER_MAX_SECONDS=5
DEEPSEEK_MAX_OUTPUT_TOKENS=1024
```

`attempt_count` 包含初始请求；因此默认最大为 2。backoff 使用确定性 exponential，
`0.25s` 起步并受 2 秒 cap。HTTP `Retry-After` v0.1 只接受合法 delta-seconds，并 cap 为 5 秒；
非法值或 HTTP-date 暂忽略，回退本地 backoff。

### 9.5 timeout/retry 矩阵

| 情况 | 默认重试 | 最终错误码 | 说明 |
| --- | --- | --- | --- |
| `ConnectTimeout` / `ConnectError` | 是，最多 1 次 | `MODEL_TIMEOUT` / `MODEL_UNAVAILABLE` | 请求尚未可靠建立 |
| HTTP 408 | 是，最多 1 次 | `MODEL_TIMEOUT` | 受总 attempt budget 限制 |
| HTTP 429 | 是，最多 1 次 | `MODEL_RATE_LIMITED` | 遵守 capped `Retry-After` |
| HTTP 500/502/503/504 | 是，最多 1 次 | `MODEL_UNAVAILABLE` | 短暂 provider 故障 |
| `ReadTimeout` | 否（推荐） | `MODEL_TIMEOUT` | provider 可能已执行，重试可能重复计费 |
| `WriteTimeout` | 否（推荐） | `MODEL_TIMEOUT` | 发送状态可能不明确，避免重复请求 |
| `PoolTimeout` | 否 | `RUNTIME_BUSY` | 本地连接池/并发容量，不伪装成 provider 故障 |
| HTTP 400/404/422 | 否 | `MODEL_BAD_REQUEST` | 请求不可通过 retry 修复 |
| HTTP 401/403 | 否 | `MODEL_AUTH_FAILED` | 不允许 fallback 掩盖 API 默认错误 |
| 其他 4xx | 否 | `MODEL_BAD_REQUEST` | 保守分类 |
| JSON/字段/choices/content 非法 | 否 | `MODEL_INVALID_RESPONSE` | 不记录 provider body |

当前 `FallbackChatModelClient` 需要改为捕获新的 typed model errors，而不是只依赖 urllib
异常。fallback 白名单冻结为：

```text
CLI normal chat only:
  MODEL_TIMEOUT
  MODEL_RATE_LIMITED
  MODEL_UNAVAILABLE
  MODEL_INVALID_RESPONSE
```

只有 `surface="cli"`、purpose=`dialogue` 且 `AEMEATH_ENABLE_MODEL_FALLBACK=true` 的 composition
才能安装 fallback wrapper；该 wrapper 只注入 `ResponseGenerator`。CLI `/memory extract` 使用的
`MemoryExtractionService` 必须注入同 gate 下的 direct-DeepSeek extraction wrapper，不能接收
dialogue wrapper。只有异常属于 typed application/model error 且 code 精确命中上表时才进入
mock fallback。以下情况明确禁止 fallback，并原样传播稳定错误：

```text
MODEL_AUTH_FAILED
MODEL_BAD_REQUEST
CONFIG_INVALID / 其他配置错误
RUNTIME_BUSY
未知内部异常或未知 error code
```

CLI Memory Extraction、FastAPI dialogue/extraction 和独立 Memory Worker 均不得安装 mock
fallback；这比“API flag 默认 false”更强，是 composition invariant。实现只捕获 `Exception`，
不捕获 `KeyboardInterrupt` / `SystemExit`。这份白名单是安全契约，未来新增错误码默认禁止，
必须经过单独审查后显式加入。

### 9.6 交互延迟上界说明

推荐配置下：

- 单次 read stall 默认约 15 秒即失败，且不自动重发；
- connect stall 两次加 backoff 约 `3 + 0.25 + 3 = 6.25s`；
- 最慢可重试 HTTP 场景仍受两个 15 秒 read inactivity phase 和一次短 backoff/capped
  Retry-After 约束，预期不再形成当前超过 120 秒的无反馈路径。

这不是 streaming 或严格 wall-clock SLA；UI/HTTP 客户端仍应显示请求进行中，并按稳定错误
决定是否提示用户重试。

### 9.7 Runtime resource 所有权与关闭

composition root 应新增
`build_application_runtime(settings, *, surface: Literal["cli", "api"]) -> ApplicationRuntime`，
它在构造服务的同时登记自己创建的资源，并按 §4.1 产生用途隔离的 model graph。旧
`build_agent_service()` 不再作为会隐藏资源所有权的公开生命周期入口；若为测试兼容暂时保留，
也只能从一个显式 runtime 参数投影 service，不能再自行创建未登记 client。

`RuntimeResources` 契约：

1. 资源在创建成功后立即按创建顺序 `register(name, close)`；只登记 composition root 自己
   拥有的资源，注入资源由注入方关闭。
2. 内部状态固定为 `OPEN -> CLOSING -> CLOSED`。首个调用在 condition lock 内原子切换到
   `CLOSING` 并记录 closing thread，随后在 lock 外执行 close callbacks；其他线程等待
   `CLOSED` 后取得同一缓存结果，同一 closing thread 的再入调用立即返回空 tuple。不得在
   registry lock 内调用未知 delegate；任何 delegate 最多调用一次，因而幂等且线程安全。
3. 按注册逆序关闭；一个资源关闭失败时记录安全失败摘要并继续关闭其余资源，不泄漏
   exception message/repr。返回 `ResourceCloseFailure(name, error_type)` 供启动器决定退出码。
4. 若未来 logger sink 本身可关闭，则应最先注册、最后关闭，使其他资源关闭失败仍可由
   `SafeAppLogger` 记录。当前 JSONL logger 每次 record 自行 open/close 文件，不作为持久资源
   登记；logger 自己失败时 best-effort 丢弃，禁止递归写日志。
5. composition 中途构造失败时，builder 必须在抛出原始构造异常前关闭已登记资源；关闭失败
   不覆盖原始异常。

入口精确定义：

- FastAPI：lifespan startup 创建/接收唯一 `ApplicationRuntime`，`finally` 调用
  `runtime.close()`；route/dependency 只从 `app.state` 获取 service，不拥有 client。
- CLI：`run_cli()` 在进入交互循环前创建 runtime，在最外层 `finally` 调用
  `runtime.close()`，包括 EOF、用户退出和业务异常路径。
- Memory Worker：`run_worker()` 创建自己的 `RuntimeResources`，在创建 DeepSeek、Embedding
  HTTP client 等资源时立即登记，最外层 `finally` 关闭。它不从 API runtime 借用实例，也不
  通过 Controller 字段寻找 client。
- 所有生产/qualification Embedding 入口通过公开 Factory 得到
  `EmbeddingProviderRuntime(provider, close)`。composition root 立即登记 bundle 的 `close`，
  业务只使用 bundle 的 `provider`；不得调用 Factory 私有 `_create_provider()`、读取 wrapper
  `_provider` 或给 wrapper 增加临时 introspection hook。
- 当前 `SiliconFlowEmbeddingProvider` 会 lazy-create `httpx.Client`，必须实现 §5.7.2 的 concrete
  condition/state/active-call 协调：`client=` 为外部所有，`client_factory=` 与默认 lazy client 为
  provider 所有，关闭后禁止创建或调用。Factory bundle 捕获 concrete close，不能只依赖 bundle
  自己的 at-most-once lock。`DeepSeekClient` 保持其独立 owned/injected 规则。

HTTPX 官方 API 明确说明 `Client` 可在线程间共享，并提供显式 `close()`；持久 client 由上述
runtime owner 统一管理：<https://www.python-httpx.org/api/>。

## 10. 单请求成本上限

并发和 rate limit 只限制“同时/一段时间有多少请求”；v0.1 还必须同时限制“一次请求最多带入
多少不可信数据、最多要求模型生成多少 token”。两者缺一不可。

### 10.1 推荐默认值

| Guard | 推荐默认值 | 校验位置 | 超限结果 |
| --- | ---: | --- | --- |
| message Unicode 字符数 | 4,000 | `AgentService`，Controller 前 | `INVALID_INPUT` / 400 |
| message UTF-8 字节数 | 12,000 | `AgentService`，Controller 前 | `INVALID_INPUT` / 400 |
| metadata 确定性 JSON UTF-8 字节数 | 8,192 | `AgentService`，Controller 前 | `INVALID_INPUT` / 400 |
| DeepSeek 最大输出 | 1,024 tokens | `DeepSeekClient._build_payload` | payload `max_tokens=1024` |

字符和字节限制同时生效；前者给用户稳定可理解的编辑上限，后者约束多字节文本和实际
序列化体积。`max_tokens` 是 provider-side 的最大生成量，不是输出长度承诺，也不用于推断精确
费用。DeepSeek 官方 Chat Completion 契约支持 `max_tokens`：
<https://api-docs.deepseek.com/api/create-chat-completion>。

### 10.2 精确计算与拒绝顺序

`ChatInputLimitPolicyV01` 按以下固定顺序处理最终将传给 Controller 的输入，不 trim、不截断、
不做 Unicode normalization：

1. `message_char_count = len(message)`；超过字符上限即拒绝。
2. `message_utf8_bytes = len(message.encode("utf-8"))`；编码失败或超过字节上限即拒绝。
3. metadata 使用
   `json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`
   序列化后计算 UTF-8 字节数。不可 JSON 序列化、包含 NaN/Infinity 或超过上限均拒绝。
4. 全部通过后才允许进入 Session lock、Controller、message persistence、Semantic、Prompt 和
   Provider。拒绝不得保存 user message，不得写 Prompt Log，不得调用 BGE/DeepSeek。

metadata 计算对象是合并可信内部字段后、最终准备传入 Controller 的 metadata；内部字段也不
享有绕过上限的特权。失败日志只允许记录固定 reason 和测得的 count，不记录 metadata key/value
或 message 正文。

### 10.3 稳定 INVALID_INPUT reason

```text
chat_message_char_limit_exceeded
chat_message_utf8_limit_exceeded
chat_message_utf8_invalid
chat_metadata_not_json_serializable
chat_metadata_utf8_limit_exceeded
```

`INVALID_INPUT` 的 public body 继续使用通用文案；安全 metadata 最多包含 `limit_reason`、
`measured_count`、`configured_limit` 和 `unit`。Pydantic/FastAPI schema 应提供等价的 HTTP 早期
拒绝，但 `AgentService` 校验仍是 CLI 与所有 adapter 的权威契约。

### 10.4 DeepSeek output cap

- `DEEPSEEK_MAX_OUTPUT_TOKENS` 必须是 `1..8192` 的整数；该 8192 是 Aemeath v0.1 本地安全
  ceiling，不声明为 provider 能力上限。
- `_build_payload()` 每次都写入 `"max_tokens": configured_value`；不得用 `None` 或省略字段
  静默回到 provider 默认。
- retry 使用同一个 payload 和同一个 output cap；fallback mock 不产生外部费用，但仍不得以
  fallback 绕过输入校验。
- 独立 Memory Worker 的 DeepSeek request 也使用同一个 output cap；若未来提取任务需要不同
  上限，应新增 purpose-specific 配置并重新审查，不能在运行时任意放大。

现有 Context Budget 继续治理 Character/history/evidence 组成的编译上下文；本节不改变其压缩
策略，也不把字符上限伪装成精确 input token 计量。若后续要给所有编译后 model messages 增加
独立总字节硬上限，应作为 Context Budget/Prompt 契约任务评审，不在本轮修改 Controller 次序。

### 10.5 原始 HTTP body 边界

`ChatInputLimitPolicy` 和 FastAPI/Pydantic 字段校验都发生在 ASGI server 已接收并解析请求体
之后，因此 4,000/12,000/8,192 限制不能防止客户端在 JSON 解析前发送超大 raw HTTP body，
也不能作为网络层 DoS 防护。

单实例生产部署任务必须在直接面向客户端的反向代理配置 request-body 上限，并基于允许的
message、metadata、JSON escaping 和 envelope 开销留出明确 headroom；不能简单把业务层
12,000-byte message 上限原样当作整个 body 上限。无反向代理的开发启动只用于受信环境。
具体代理指令和值属于部署配置，不在本轮运行时代码中硬编码；上线验收必须验证超限 body 在
进入 Uvicorn/FastAPI 前被拒绝，并在 `docs/fastapi_service.md` 记录部署要求。

## 11. Semantic Query 成本控制

### 11.1 放置位置

推荐新增 `DeterministicSemanticEligibilityPolicyV01`，由
`NeutralTurnQueryBuilderV01.build(...)` 在 canonical query 构造和 provider 调用前执行。

不放置在：

- `DialogueController`：会把检索策略带回编排层；
- `MemoryReader`：只能节省 Memory 的消费，未来其他 semantic role 仍可能重复调用；
- DeepSeek/Embedding provider：provider 不应理解自然语言意图；
- API/CLI adapter：会导致两入口行为不一致。

Coordinator 继续负责 role、batch dedup、provider 调用、健康状态和错误分类，不承担不断
膨胀的文本意图规则。

### 11.2 判定优先级

规则对 NFC/LF canonicalized、trim 后的输入执行，优先级不可交换：

1. 空输入沿用现有 `blank_current_input` skip。
2. 若命中显式回忆/历史引用 guard，则强制 eligible。
3. 若命中现有上下文依赖表达（如 `继续`、`然后呢`、`这个呢`、`刚才`、`you said`），
   强制 eligible，并沿用近期消息依赖构造。
4. 若包含明确问句、问号、数字、英文/中文实词组合，或不在精确 allowlist，保持 eligible。
5. 只有剩余输入才能按精确低信息规则 skip。

### 11.3 v0.1 skip 规则

规则集在仓库内版本化为 `semantic-eligibility/v1`，不从环境变量读取任意词表。

建议 reason code：

- `low_information_greeting`
- `low_information_acknowledgement`
- `low_information_symbol_only`

冻结 exact allowlist（已获 Manager 批准）：

```text
Greeting:
嗨, hi, hello, 你好, 您好, 早, 早安, 早上好, 早哇, 晚上好, 晚安

Acknowledgement:
嗯, 嗯嗯, 好, 好的, 好呀, 行, 可以, 收到, 知道了, 明白, 对, 是的,
ok, okay, thanks, 谢谢, 谢啦
```

匹配只允许：casefold、首尾空白清理、有限句末标点清理。不得使用“字符数小于 N 就 skip”
这类宽规则，因为 `爱弥斯呢`、`黑潮？`、`第七章` 等短输入有明确检索价值。

纯标点、空白或全 emoji/symbol 输入可 skip；原始输入仍完整进入 Character/Prompt/Model，
这里只是不生成 Semantic embedding。

### 11.4 必须保持 eligible 的反例

```text
还记得我之前说的吗
上次那件事呢
继续
然后呢
这个为什么会这样
好累
爱弥斯呢
黑潮？
第七章
yes, but why
```

特别说明：

- “显式回忆”不要求必须成功向量召回，但不得因 low-info policy 被提前 skip。
- “上下文依赖问题”先触发依赖消息选择，再构造 shared query。
- 未来 canonical lore/voice/character roles 启用时共享同一保守 eligibility；精确 allowlist
  不含 Memory 专属判断，因此不会把未来消费者绑死在 Memory policy。
- skipped context 仍传给 Memory/Evidence。Memory 保留当前 keyword/显式 recall fallback；
  `WorldInfoEvidenceProvider` 继续按当前关键词行为忽略 semantic vector。

### 11.5 回滚开关

建议增加：

```text
AEMEATH_SEMANTIC_SKIP_LOW_INFORMATION=true
```

关闭时恢复当前“除空/超预算外均构造 query”的行为，不改变其他 semantic contract。

## 12. 错误码与 HTTP 契约

### 12.1 新增稳定错误码

| ErrorCode | HTTP | retryable | 适用场景 |
| --- | ---: | --- | --- |
| `RUNTIME_BUSY` | 503 | true | Session wait、dialogue/LLM/Embedding/pool 本地容量超时 |
| `API_RATE_LIMITED` | 429 | true | Aemeath API user/IP token bucket 拒绝 |

已有错误继续复用：

- `INVALID_INPUT` -> 400（message/metadata 硬上限或无效序列化）
- `MODEL_BAD_REQUEST` -> 502
- `MODEL_AUTH_FAILED` -> 502
- `MODEL_TIMEOUT` -> 504
- `MODEL_RATE_LIMITED` -> 429
- `MODEL_UNAVAILABLE` -> 503
- `MODEL_INVALID_RESPONSE` -> 502
- `EMBEDDING_TIMEOUT` -> 504
- `EMBEDDING_RATE_LIMITED` -> 429
- `EMBEDDING_UNAVAILABLE` -> 503

不要把 API 自身限流映射成 `MODEL_RATE_LIMITED`，也不要把本地 semaphore timeout 映射成
provider unavailable。

### 12.2 稳定 reason code

`RUNTIME_BUSY.metadata.limit_reason` 只能取固定 allowlist：

```text
session_lock_wait_timeout
dialogue_concurrency
llm_concurrency
embedding_concurrency
model_connection_pool
```

`API_RATE_LIMITED.metadata.limit_reason` 只能取：

```text
user_rate
ip_rate
```

`INVALID_INPUT.metadata.limit_reason` 对本任务新增的输入 guard 只能取：

```text
chat_message_char_limit_exceeded
chat_message_utf8_limit_exceeded
chat_message_utf8_invalid
chat_metadata_not_json_serializable
chat_metadata_utf8_limit_exceeded
```

`surface="api"` 且 API fallback 显式开启时使用 `CONFIG_INVALID`，安全 internal reason 固定为
`api_model_fallback_not_supported`；不把配置值或环境变量原文放入 public metadata。

Embedding lifecycle 不新增 ErrorCode：closed concrete 使用现有 `EMBEDDING_CONFIG_INVALID` /
`siliconflow_provider_closed`（non-retryable）；standalone 命令仅 close 失败时使用现有
`EMBEDDING_UNAVAILABLE` / `provider_close_failed`。两者都不得携带 exception message/repr。

metadata 按错误类型可带 `waited_ms`、`limit`、`retry_after_seconds`、`measured_count`、
`configured_limit`、`unit`，不得带 raw key、IP、prompt、query、请求/响应正文或 header。

### 12.3 `Retry-After`

建议在 `app/errors/` 增加统一的安全 response header helper，或由 API error adapter 只读取
`ErrorInfo.metadata["retry_after_seconds"]` 的正整数：

- `API_RATE_LIMITED` 必须返回动态 `Retry-After`。
- `RUNTIME_BUSY` 可返回固定 1 秒，或按 admission timeout 计算的保守值。
- upstream `MODEL_RATE_LIMITED` 只有 transport 已解析到安全、capped delta-seconds 时才返回。
- header 不进入 public body，错误 body 形状保持不变。

## 13. 配置草案

### 13.1 Runtime admission/concurrency

```env
AEMEATH_RUNTIME_MAX_DIALOGUE_TURNS=2
AEMEATH_RUNTIME_SESSION_WAIT_TIMEOUT_SECONDS=5
AEMEATH_RUNTIME_DIALOGUE_WAIT_TIMEOUT_SECONDS=0.25
AEMEATH_RUNTIME_MAX_LLM_REQUESTS=2
AEMEATH_RUNTIME_LLM_WAIT_TIMEOUT_SECONDS=2
AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS=1
AEMEATH_RUNTIME_EMBEDDING_WAIT_TIMEOUT_SECONDS=2
```

### 13.2 API chat rate limit

```env
AEMEATH_API_CHAT_USER_RATE_PER_MINUTE=12
AEMEATH_API_CHAT_USER_BURST=4
AEMEATH_API_CHAT_IP_RATE_PER_MINUTE=60
AEMEATH_API_CHAT_IP_BURST=20
AEMEATH_API_RATE_LIMIT_IDLE_TTL_SECONDS=600
```

### 13.3 DeepSeek

```env
DEEPSEEK_CONNECT_TIMEOUT_SECONDS=3
DEEPSEEK_READ_TIMEOUT_SECONDS=15
DEEPSEEK_WRITE_TIMEOUT_SECONDS=5
DEEPSEEK_POOL_TIMEOUT_SECONDS=1
DEEPSEEK_MAX_RETRIES=1
DEEPSEEK_RETRY_BACKOFF_SECONDS=0.25
DEEPSEEK_RETRY_BACKOFF_MAX_SECONDS=2
DEEPSEEK_RETRY_AFTER_MAX_SECONDS=5
DEEPSEEK_MAX_OUTPUT_TOKENS=1024
```

既有 fallback 开关的用途收窄为：

```env
AEMEATH_ENABLE_MODEL_FALLBACK=true       # CLI dialogue only
AEMEATH_API_ENABLE_MODEL_FALLBACK=false  # true is CONFIG_INVALID in v0.1
```

Memory Extraction 与 Memory Worker 没有 fallback 开关；显式 mock mode 仍由既有
`AEMEATH_USE_MOCK_LLM` 控制，不与 fallback 混用。

### 13.4 Chat input limits

```env
AEMEATH_CHAT_MESSAGE_MAX_CHARS=4000
AEMEATH_CHAT_MESSAGE_MAX_UTF8_BYTES=12000
AEMEATH_CHAT_METADATA_MAX_UTF8_BYTES=8192
```

### 13.5 Semantic skip

```env
AEMEATH_SEMANTIC_SKIP_LOW_INFORMATION=true
```

验证规则：dialogue/LLM concurrency limit、rate、burst、idle TTL、chat input limit 必须为正；
v0.1 的 `AEMEATH_RUNTIME_MAX_EMBEDDING_REQUESTS` 必须精确等于 `1`，避免通过外层 gate 后在
`SynchronizedEmbeddingProvider` 内无界等待；等待
timeout 可为 0 表示立即失败，但不得为负；DeepSeek 四类 timeout 必须为正；`max_retries` 只能
是非负整数；backoff/cap 不得为负；`DEEPSEEK_MAX_OUTPUT_TOKENS` 必须在 `1..8192`。非法配置在
启动时 fail-fast 为 `CONFIG_INVALID`，不能静默 clamp 到一个意外值，也不能在首个真实请求时才
暴露。runtime `surface` 只能是 `cli` 或 `api`；`surface=api` 且 API fallback 开关为 true 也在
任何外部 client 创建前 fail-fast。

## 14. Observability 与脱敏契约

### 14.1 事件落点

| 事件 | Log type | 说明 |
| --- | --- | --- |
| API rate accepted/rejected | App Log | HTTP adapter 边界 |
| input limit rejected | App Log | Service 边界；不含 message/metadata 正文 |
| Session/dialogue admission accepted/rejected/released | App Log | Service/runtime 边界 |
| LLM gate wait、DeepSeek terminal outcome | App Log | provider/runtime 边界 |
| Embedding gate wait/reject | App Log | Foundation 外层；现有 embedding terminal event 保留 |
| semantic low-info skip | App Log + 现有 turn Trace safe summary | 不含输入正文 |
| runtime resource close failure | App Log | 进程 shutdown；只含 resource name/error type |
| standalone Embedding close failure | App Log/安全 stderr | qualification/RAG owner；primary error 优先 |
| turn 内现有步骤 | Trace Log | 继续由 Controller 记录，不新增 guardrail policy |
| full prompt/messages/response debug | Prompt Log | 维持现有敏感本地调试边界 |

pre-admission reject 发生在 Controller 创建 `turn_id` 前，因此 `turn_id` 合法为空；不得伪造一个
与 Controller turn 不一致的 id。成功 turn 的 service completion 和 provider/trace 事件在可用时
带真实 `turn_id`。`request_id` 由 API middleware 或 AgentService 生成并贯穿 metadata。

### 14.2 建议事件名

```text
runtime.dialogue_admitted
runtime.dialogue_rejected
runtime.dialogue_completed
runtime.llm_admitted
runtime.llm_rejected
runtime.embedding_admitted
runtime.embedding_rejected
api.chat_rate_limited
runtime.input_rejected
runtime.resource_close_failed
embedding.resource_close_failed
model.deepseek_completed
model.deepseek_failed
semantic.query_skipped              # 复用当前事件名
```

每个 logical provider call 只写一个 completed/failed terminal event；retry attempt 作为
`attempt_count` 聚合，不为每次 attempt 记录可能膨胀的正文型事件。

### 14.3 固定安全字段

只允许按事件选择下列字段：

```text
request_id
turn_id                 # nullable before turn creation
session_id
user_id
character_id
status
limit_reason
wait_ms
session_wait_ms
dialogue_wait_ms
execution_ms
total_ms
attempt_count
retry_count
active_count
peak_active_count
limit
provider
model
profile_id
purpose
batch_size
http_status
retry_after_seconds
error_code
measured_count
configured_limit
unit
external_request_id_accepted
resource_name
error_type
semantic reason_code / eligibility_version
```

所有 payload 仍经过 `app.errors.redact_sensitive(...)`，但 sanitizer 是最后防线，不是记录
正文后再依赖脱敏的许可。

### 14.4 明确禁止字段

Guardrail、App Log 和新增 Trace payload 禁止记录：

- prompt / model messages；
- user message、canonical query、recent-message body；
- model response 或 provider response body；
- vector、canary text、embedding input；
- `Authorization`、`Cookie`、API key、token、password、secret；
- raw client IP、raw rate-limit key；
- 完整 URL query string 或可能携带凭证的 proxy URL；
- raw exception repr/HTTP body。

### 14.5 Trace / App Log / Prompt Log 边界

- App Log：负责请求 admission、限流、provider、等待、执行和操作失败摘要。
- Trace Log：负责已经开始的一次 agent turn 内部结构化步骤；不记录 API/IP limiter，也不让
  Controller 获知 semaphore。
- Prompt Log：保留现有显式本地 debug 行为，可能包含 full prompt/response，属于敏感产物；
  本任务不往 Prompt Log 新增任何 guardrail、token、header、provider body 或限流字段。
- FastAPI 继续默认 `AEMEATH_API_ENABLE_PROMPT_RUN_LOG=false`。

### 14.6 JSONL 线程安全契约

`JsonlAppLogger`、`JsonlTraceLogger` 和 `JsonlPromptRunLogger` 各自持有一个实例级
`threading.Lock`：

1. 先在 lock 外把 record 序列化为一条完整 JSON 字符串并附加单个 `\n`。App/Trace 继续先走
   allowlist/sanitizer；Prompt 保持现有敏感记录内容，不因加锁而改变 payload。
2. 在 lock 内完成目标目录创建、文件 open、整行单次 `write()` 和 close/flush。
3. 同一 logger 实例的多个线程不得交错字节；并发测试必须能逐行 `json.loads()` 全部记录，且
   行数等于提交次数。
4. v0.1 只承诺同进程、同实例线程安全；不承诺多个进程共同写一个文件。现有按进程/日期的
   文件边界应继续保留。
5. 写锁只能通过 context manager 获取，任何 mkdir/open/write/flush 异常都必须释放锁；Prompt
   failure 虽可继续向上抛出，但不得让后续记录永久死锁。

只依赖“一次 write 通常原子”不构成契约；必须有显式 lock。三个 logger 分别有自己的锁，
彼此之间不承诺全局事件顺序。Prompt logger 的锁只保证 JSONL 行完整性，不改变其异常传播：
序列化、mkdir/open/write/flush 失败仍按现有行为抛出。

### 14.7 best-effort logger wrapper

新增 `SafeAppLogger(delegate)` 和 `SafeTraceLogger(delegate)`，并在 composition root 把所有运行
路径看到的 App/Trace logger 包在安全 wrapper 外层：

- `record(...)` 只捕获 delegate 抛出的 `Exception` 并返回，不改变业务成功值、原始业务异常、
  lock/semaphore 释放或 HTTP status。
- 不捕获 `KeyboardInterrupt` / `SystemExit`，不对 logger failure 递归调用另一个 logger。
- wrapper 可维护线程安全的进程内 `failure_count` 和最后一个安全 `error_type` 供测试/关闭摘要
  使用；不保存 exception message/repr、event payload 或正文。
- `Noop` logger 也可由同一 wrapper 包裹，保持 composition 形状稳定。
- `PromptRunLogger` 不纳入本轮 failure-safe 承诺；不新增 `SafePromptRunLogger`。它只增加实例
  写锁，维持既有显式敏感调试边界和异常行为。

因此本轮明确承诺 Trace 写入失败不影响请求，并保留对应故障注入测试；不会再让 raw
`JsonlTraceLogger` 直接暴露给 Controller。

## 15. v0.1 成本与容量统计

### 15.1 本轮可实现

不改变模型公开响应契约即可可靠统计：

- chat logical request count；
- DeepSeek provider `attempt_count` / `retry_count`；
- Semantic/Embedding logical batch count、purpose、batch size；
- low-information skip count 与 reason；
- input-limit rejection count、reason 和单位（不含正文）；
- resolved request-id 中 external accepted/generated 的次数；
- dialogue/LLM/Embedding active 与 peak active concurrency；
- queue wait、provider execution、total duration；
- busy、API rate-limit、provider timeout/rate-limit/unavailable 次数。

实现可以维护一个线程安全、进程内 `RuntimeStatsSnapshot` 供测试和 terminal App Log 使用，
但本轮不增加 metrics HTTP endpoint，不写 Persistence schema。App Log 关闭时不得为了统计而
创建隐藏文件。

### 15.2 本轮不承诺

- DeepSeek 精确 input/output/cached/reasoning token；
- provider 账单、折扣、汇率和金额；
- fallback 前后拆分的精确金额；
- 多进程/多节点聚合；
- 跨进程 Memory Worker 与 API 的统一 quota。

Embedding provider 当前可能返回 `input_tokens`，可以保留为安全诊断，但不能把它当成
跨 provider 精确计费事实。

### 15.3 未来精确计费

未来若产品确实需要精确 token/金额，应作为独立的 ModelRequest/ModelResponse/Usage
contract 任务，基于 provider usage schema、价格版本和账单对账设计；不能在本轮为了计费
强行把 `ChatModelClient.chat(...) -> str` 扩大。

## 16. 预计修改文件

以下范围已获 Manager 架构批准，但必须等 Embedding Foundation 对 §5.7 定向确认后才实施。

### 16.1 新增

```text
app/runtime/__init__.py
app/runtime/guardrails/__init__.py
app/runtime/guardrails/schemas.py
app/runtime/guardrails/ports.py
app/runtime/guardrails/admission.py
app/runtime/guardrails/concurrency.py
app/runtime/guardrails/rate_limit.py
app/runtime/guardrails/errors.py
app/runtime/guardrails/observability.py
app/runtime/resources.py
app/embeddings/runtime.py             # public EmbeddingProviderRuntime bundle
app/observability/safe.py
app/semantic/eligibility.py
app/model/errors.py
tests/test_runtime_admission.py
tests/test_runtime_concurrency.py
tests/test_runtime_resources.py
tests/test_runtime_composition.py
tests/test_embedding_runtime.py
tests/test_chat_input_limits.py
tests/test_api_rate_limit.py
tests/test_observability_safe.py
tests/test_semantic_eligibility.py
```

### 16.2 修改

```text
app/config.py
app/main.py                          # surface/purpose model graphs + resource registration
app/services/agent_service.py
app/api/schemas.py
app/api/dependencies.py
app/api/routes/chat.py
app/api/errors.py
app/api/server.py                    # limiter/runtime lifecycle
app/model/deepseek.py
app/model/__init__.py
app/memory/worker.py                 # lifecycle + 禁止 worker mock fallback；不改 Memory policy
app/memory/runtime.py                # return EmbeddingProviderRuntime bundle
app/embeddings/factory.py            # public Factory returns provider/resource bundle
app/embeddings/qualify.py            # owns bundle; pass .provider; close in finally
app/embeddings/providers/siliconflow.py  # resource condition + owned lazy close
app/embeddings/__init__.py           # export EmbeddingProviderRuntime / updated Factory
app/retrieval/manage_rag_index.py    # build/query own+close bundle; status no Factory
app/observability/app_logger.py      # JSONL instance write lock
app/observability/trace_logger.py    # JSONL instance write lock
app/observability/prompt_run_logger.py  # JSONL instance write lock only; no Safe wrapper
app/observability/runtime.py         # return SafeAppLogger / SafeTraceLogger
app/semantic/ports.py
app/semantic/query_builder.py
app/semantic/coordinator.py          # state lock bounded wait / RUNTIME_BUSY transient handling
app/semantic/schemas.py              # 只增加已批准的 safe eligibility metadata
app/errors/codes.py
app/errors/http.py
app/errors/__init__.py
app/cli.py                           # runtime close/finally only
pyproject.toml
.env.example
tests/test_agent_service.py
tests/test_api.py
tests/test_deepseek_client.py
tests/test_errors.py
tests/test_observability.py
tests/test_prompt_run_logger.py
tests/test_cli_errors.py              # runtime lifecycle + /memory extract fallback isolation
tests/test_memory_worker.py
tests/test_semantic_query.py
tests/test_semantic_query_integration.py
tests/test_context_budget.py          # migrate Factory monkeypatch to bundle
tests/test_rag_generation_runtime.py
tests/embeddings/test_synchronized.py 或新的 wrapper test
tests/embeddings/test_siliconflow.py
tests/embeddings/test_factory_config.py
tests/embeddings/test_hash_approved_profile.py
tests/embeddings/test_qualification_cli.py
tests/embeddings/test_qualification.py  # pure Port consumer regression only
```

### 16.3 文档

实现通过后应由 Manager 或经 Manager 授权更新：

```text
docs/architecture.md
docs/interfaces.md
docs/task_log.md
docs/error_handling.md
docs/observability.md
docs/fastapi_service.md
```

不预计修改：

```text
app/agent/dialogue_controller.py
app/character/**
app/memory/** business policy
app/embeddings/qualification.py       # remains pure EmbeddingProviderPort consumer
app/retrieval/** index/provider policy  # except manage_rag_index.py lifecycle adapter
app/retrieval/build_wuwa_rag.py
app/retrieval/query_wuwa_rag.py
app/retrieval/rag_cli.py
app/db/schema.sql
app/db/** persistence schema
```

## 17. 测试与故障注入计划

### 17.1 Session lock

- 同 `(user_id, session_id)` 两线程严格不重叠执行 Controller。
- 同 user 的不同 Session 可在全局上限内并行。
- 不同 user 使用同一个 `session_id` 时落到不同锁；非法 user 的请求在 Controller ownership
  校验失败，但不会占用合法 user 的 Session 锁。
- Session wait 超时抛 `RUNTIME_BUSY`，Controller 未被调用、无 message/State/Memory side effect。
- Controller 成功、普通异常、Base provider 异常时均释放锁。
- waiter acquire 超时后引用计数归零并删除 entry。
- holder release 与新 waiter 竞争时不会创建第二把同 key lock。
- 大量唯一 Session 完成后 registry 回到零或预期小值。
- 不写 FIFO 假设测试。

### 17.2 Global concurrency

- dialogue active count 永不超过 2。
- 超过上限的请求在 0.25 秒附近失败，不长时间占线程。
- LLM/Embedding wrapper active count 不越界。
- Semantic coordinator state lock 被占用时在 Embedding wait timeout 内降级为 failed context，
  不无界等待、不标记 unhealthy，随后 Memory fallback 仍执行。
- provider 抛错和 logger 抛错均释放 permit。
- retry attempt 保持一个 LLM permit，不重复 acquire。
- CLI 和 FastAPI 构造出的 service 使用同一 admission 实现。
- `build_application_runtime(surface="cli")` 的 dialogue/extraction wrapper 是不同对象但引用同一
  gate；并发混合调用时 combined active count 永不超过 2。
- CLI dialogue 安装 allowlisted fallback（开关开启时），CLI `/memory extract` graph 中没有
  fallback；对四个 allowlisted primary error 分别断言 dialogue 返回 mock、extraction 原样失败。
- `surface="api"` 的 dialogue/extraction 和 Memory Worker extraction graph 均没有 fallback；
  未知 surface 以及 API fallback 显式为 true 均在创建 client 前 `CONFIG_INVALID`。

### 17.3 API limiter

- fake monotonic clock 验证连续 refill、burst 和 60 秒 rate。
- user bucket 拒绝、IP bucket 拒绝分别得到稳定 reason。
- 任一 bucket 拒绝时另一个不扣 token。
- `Retry-After` 向上取整且至少为 1。
- user 与 IP 同时受限时，`Retry-After` 使用两者计算结果的最大值。
- 多线程同 key 竞争不会超发 token。
- idle TTL 清理不删除活跃 bucket；常规请求不会全表扫描，单次 sweep 删除数有固定上限。
- raw IP、Authorization、Cookie、body 不出现在 App Log。
- Uvicorn 未信任 peer/关闭 proxy headers 时，伪造 `X-Forwarded-For` 不改变 IP key；受信代理
  场景用集成测试确认 `request.client.host` 的改写语义并验证部署参数不使用 `*`。
- `X-Request-ID` 有效边界值（1/128 字符）原样采用；空值、129 字符、Unicode、空白、控制字符
  和非法标点全部生成新 `req_*`，响应/日志不含原始值。
- CLI 调用完全不经过 API limiter。

### 17.4 Input cost limits

- message 分别在 4,000/4,001 字符和 12,000/12,001 UTF-8 字节边界测试；包含汉字和 emoji。
- metadata 用确定性 JSON 测 8,192/8,193 字节边界；key 顺序不同得到同一尺寸。
- NaN、Infinity、不可序列化对象稳定映射 `INVALID_INPUT`。
- 每种 reject 均断言 Controller、message store、Semantic/Embedding、DeepSeek call count 为 0。
- FastAPI schema 与 AgentService 得到相同错误码；CLI 直接 Service 调用无法绕过。
- DeepSeek payload 始终含 `max_tokens=1024`，配置 0、负数和 8193 在启动时
  `CONFIG_INVALID`；retry payload cap 不变。
- 日志只含 reason/count/limit/unit，不含 message、metadata key/value。

### 17.5 DeepSeek transport

- 一个 client 跨多次 chat 复用，close 幂等。
- 精确断言 connect/read/write/pool timeout 和 connection limits。
- 200 成功解析与 whitespace trim。
- connect timeout/error、408、429、500/502/503/504 的 retry 矩阵。
- read/write/pool timeout 默认不 retry。
- 400/401/403/404/422 不 retry。
- `Retry-After` 合法、非法、超 cap 场景。
- `attempt_count`、`retry_count`、duration 和 error code 正确。
- JSON、choices、message、content 缺失/非法映射 `MODEL_INVALID_RESPONSE`。
- injected response body、prompt、API key、Authorization 不进入日志或异常 metadata。
- AppLogger 抛错不改变 transport 结果。
- fallback 白名单四个 allowed code 逐项进入 CLI mock；`MODEL_AUTH_FAILED`、
  `MODEL_BAD_REQUEST`、配置错误、`RUNTIME_BUSY`、未知 code 和普通内部异常逐项不得 fallback。
- CLI `/memory extract`、FastAPI dialogue/extraction 与 Memory Worker 即使 primary 抛出白名单
  错误也不得进入 mock fallback。

### 17.6 Runtime resources and logging

- `RuntimeResources.close()` 按逆注册顺序调用、重复/并发 close 不重复 delegate；delegate 在同一
  closing thread 重入 `close()` 时立即返回，其他等待线程全部在 `CLOSED` 后退出。
- 中间 close 抛错时继续关闭其余资源并只返回安全 `name/error_type`；构造中途失败关闭已登记
  资源且不覆盖原始异常。
- FastAPI lifespan、CLI `finally`、Memory Worker `finally` 分别在成功、业务异常、EOF/退出路径
  调用 runtime close。
- 注入 client 不被 runtime/provider 关闭；owned DeepSeek 与 lazy SiliconFlow client 只关闭一次；
  `client_factory=` 创建物归 provider 所有；`client` 与 `client_factory` 同时提供稳定拒绝。
- Factory 对 SiliconFlow/local/hash/qualification 返回公开 `EmbeddingProviderRuntime`；业务只取
  `.provider`，`RuntimeResources` 只登记 `.close`。测试不得调用 `_create_provider()` 或遍历
  `_provider`。
- close-before-first-embed 后不创建 client，后续空/非空 embed 均抛
  `EmbeddingConfigurationError("siliconflow_provider_closed")`。
- 用 blocking fake client factory 精确交错 lazy create/close：若 factory 获得 state 先行权，
  创建物最终 close 一次；若 close 先进入 `CLOSING`，factory call count 为 0。
- 用 blocking fake `post()` 证明 active embed 未完成时 close 等待；embed finally 递减 active count
  后 close 完成，后续 embed 拒绝。embed/factory/client.close 各自抛错时 condition waiter 无泄漏。
- owned client.close 抛错后 state 仍为 `CLOSED`，并发/重复 close 不再次调用；injected client
  从不被关闭。
- Factory/wrapper 构造中途失败清理已创建 owned resource；正常 runtime 仍重新加载并完整比较
  canonical approved Profile，Hash 未显式 `allow_hash` 时仍拒绝。
- `qualify.py` 的 capture/verify 成功和异常路径均 close bundle，只把 `.provider` 传给纯
  qualification Port；`qualification.py` 回归测试不出现 bundle/lifecycle 依赖。
- `manage_rag_index.py` build/query 在成功与异常路径均 close；status 断言 Factory call count=0。
  close 与业务同时失败时保留业务异常；仅 close 失败时返回非零和稳定安全 reason。
- `app.embeddings` 公开导出 bundle 类型；Memory/Semantic integration、Context Budget monkeypatch、
  Memory Worker、RAG generation 与 ApplicationRuntime fixtures 全部迁移到 bundle 返回类型。
- 多线程分别写 App JSONL、Trace JSONL、Prompt Run JSONL，三类文件所有行都可独立解析且
  行数完整；Prompt record 的 messages/response 内容在测试中使用合成标记，不使用真实敏感数据。
- delegate App/Trace logger 抛异常时，业务结果/业务异常、permit 和 Session lock 均不改变；
  `failure_count` 线程安全且不保存异常正文。
- Prompt logger 写锁不改变 failure contract：注入 write failure 时仍抛出，且不存在
  `SafePromptRunLogger`；失败后下一次 record 仍能获得锁，证明无 lock leak。

### 17.7 Semantic skip

- `早哇`、`好的`、`嗯`、纯符号/emoji：context `skipped`，provider call count 为 0。
- 大小写/有限句末标点 normalization 保持确定性。
- `还记得...`、`上次...`、`继续`、`然后呢`、`这个为什么...`：不得 skip。
- `好累`、`爱弥斯呢`、`黑潮？`、数字/实体短语：不得 skip。
- skip safe summary 只有版本/reason/count，不含输入正文。
- skip 后 Memory keyword/显式 recall fallback 与 WorldInfo 现有调用仍正常。
- feature flag 关闭时恢复现状。

### 17.8 故障注入

- Session holder 在模型调用中抛异常。
- 两个 global permits 被 fake blocker 占满。
- HTTP pool exhaustion。
- DeepSeek connect/read/write timeout。
- 429 + `Retry-After`，503 后成功，连续 503 exhaustion。
- malformed/oversized provider JSON（不把 body落日志）。
- AppLogger/TraceLogger mkdir/open/write/serialization delegate 失败；Safe wrapper 全部隔离。
- runtime close 的第一/中间/最后一个资源分别抛异常。
- Embedding bundle/concrete 在 lazy create、active embed、concurrent close、client close failure、
  injected/owned client 和 Factory 构造中途失败的确定性交错。
- qualification/RAG primary failure + close failure；断言 primary error/code/traceback 不被替换，
  安全日志和 stderr 不含 exception message、query、vector、provider body 或 credential。
- monotonic clock 精度边界和 bucket 同时恢复。
- Embedding holder 超时、随后下一个请求成功，证明 permit/lock 无泄漏。

### 17.9 验收命令

Windows pytest 使用仓库内 basetemp，避免已知系统 temp `PermissionError`：

```powershell
pytest -q tests/test_runtime_admission.py tests/test_runtime_concurrency.py `
  tests/test_runtime_resources.py tests/test_runtime_composition.py `
  tests/test_embedding_runtime.py tests/test_chat_input_limits.py `
  tests/test_cli_errors.py `
  tests/test_api_rate_limit.py tests/test_deepseek_client.py `
  tests/test_observability_safe.py tests/test_prompt_run_logger.py `
  tests/test_semantic_eligibility.py tests/test_memory_worker.py `
  tests/test_rag_generation_runtime.py tests/test_semantic_query_integration.py `
  tests/test_context_budget.py tests/embeddings/test_factory_config.py `
  tests/embeddings/test_hash_approved_profile.py tests/embeddings/test_siliconflow.py `
  tests/embeddings/test_qualification_cli.py tests/embeddings/test_qualification.py `
  --basetemp .pytest_tmp/runtime_guardrails_focused

pytest -q --basetemp .pytest_tmp/runtime_guardrails_full
ruff check .
python -m compileall -q app tests
git diff --check
```

真实 provider smoke 只能在 Manager 明确批准且不打印密钥/正文的情况下执行；单元和故障注入
不依赖真实 DeepSeek/SiliconFlow API。

## 18. 单实例限制

v0.1 的“全局”严格指一个 Python 进程：

- `threading.Lock` / `Semaphore` 不跨进程。
- token bucket 不跨进程，也会在进程重启后清空。
- Uvicorn `--workers > 1` 时，每个 worker 都会有独立 Session lock、并发额度和 API quota；
  因此 v0.1 上线必须明确使用单 worker。
- 同时运行 CLI 和 FastAPI 是两个进程；二者复用相同实现和配置语义，但不共享锁实例。
- 独立 Memory Worker 不共享 API 进程的 Embedding/LLM semaphore。
- 进程崩溃会释放所有本地锁，但进行中的 turn 可能已有 user message 等既有非事务副作用；
  本任务不引入跨整个 turn 的数据库事务。

对外文档不得把 v0.1 描述成多节点安全、全机器 quota 或 exactly-once。

## 19. 未来多实例迁移方案

本轮先稳定 Port 和 reason/error contract，将实现限制在进程内。未来迁移可以保持
`DialogueAdmissionPort` / `ApiRateLimiterPort` 调用方不变：

1. Session lock：迁移到带 owner token、租约、续租和 fencing token 的分布式 lock，或由
   数据库 advisory lock/专用 coordinator 实现；必须处理 holder 崩溃与过期 owner。
2. Global concurrency：迁移到分布式 semaphore 或集中 admission queue；需要原子 acquire、
   lease expiry 和公平性定义。
3. API rate limit：迁移到共享原子 token bucket，例如服务端脚本一次完成 refill/check/debit；
   保留当前 `Retry-After` 语义。
4. Cost stats：接入 metrics/usage pipeline，按 request id 去重聚合；不依赖 App Log 文件扫描
   作为精确账单。
5. Idempotency：API 增加明确 idempotency key 与持久状态，解决客户端 timeout 后重试产生的
   duplicate turn。
6. Worker quota：Memory Worker、API 和其他后台任务使用独立优先级与共享 provider budget，
   防止后台 repair 饿死交互请求。

上述只描述迁移方向；v0.1 不加入 Redis、队列或新 Persistence schema。

## 20. Deferred

- Streaming / SSE / WebSocket 和首 token latency。
- 严格整请求 wall-clock deadline、主动 cancellation、客户端断开传播。
- 多进程/多节点锁、quota 和公平队列。
- 请求 idempotency / exactly-once turn。
- 复杂多跳代理链自动发现、RFC 7239 规范化与运行时 trusted-proxy 动态更新；v0.1 只支持部署时
  明确列出可信直接代理。
- Uvicorn/FastAPI 内部的 streaming/pre-parse request-body limiter；v0.1 生产部署依赖反向代理
  body limit，不能省略该部署门禁。
- endpoint/role/plan 级差异化 quota。
- circuit breaker、adaptive concurrency、provider health routing。
- foreground dialogue 与 background Memory repair 的优先级调度。
- 精确 token、缓存 token、reasoning token、金额和账单对账。
- ModelRequest/ModelResponse 契约扩展。
- metrics endpoint、OpenTelemetry、云日志与 dashboard。
- managed vector RAG active-provider wiring。

## 21. 风险与取舍

1. Session lock 保证顺序，但长模型调用会让同 Session 后续请求在 5 秒后 busy；这是避免
   无界排队的有意取舍。
2. dialogue cap=2 很保守，可能降低多 Session 吞吐；先保护交互稳定性，再用数据调高。
3. read timeout 不 retry 会减少自动恢复机会，但避免重复 provider 执行、重复费用和 30 秒以上
   延迟；用户可收到可重试的稳定 `MODEL_TIMEOUT`。
4. token bucket 是进程内近似治理，不是安全边界；账号权限仍由 Auth/Session ownership 负责。
5. exact low-info allowlist 会漏掉部分无意义输入，但漏掉只产生少量额外成本；误 skip 可能损害
   召回，因此 v0.1 应优先低误杀而不是高覆盖率。
6. Prompt Log 现有 CLI 默认可能保存完整 prompt/response；本任务只保证新增 guardrail 事件不写
   正文，不改变该历史调试默认。
7. 复合 Session key 让不同用户的同 `session_id` 请求使用不同锁；这是有意隔离非法 user 的
   容量影响。合法 Session 的 user id 固定，故不削弱合法 turn 串行；ownership 仍由 Controller
   校验，admission 不是授权边界。
8. message 字符/字节与 output token cap 会拒绝过去可提交的超长内容；这是建立成本硬上限的
   必要兼容性变化。默认值应通过真实 Character 会话样本验证后再上线，但不得静默截断。
9. best-effort logger 会在磁盘故障时丢失观测事件；业务可用性优先，进程内 `failure_count` 和
   shutdown 摘要仅用于发现问题，不构成可靠审计日志。
10. 显式 runtime owner 会改变 composition root 返回形态；测试和入口必须整体迁移，不能一半
    使用 runtime、一半继续创建隐藏 client，否则 close 契约失效。
11. CLI dialogue/extraction 使用两个 wrapper 增加 composition 复杂度；使用明确变量名、必填
    surface 和 graph-shape 测试换取 fallback 不跨 purpose 泄漏。两者必须共享同一 gate 实例，
    否则总并发上限会被意外翻倍。
12. `EmbeddingProviderRuntime` 改变公开 Factory 返回类型，会影响 Memory、qualification、legacy
    和测试调用点；必须一次性迁移并经 Embedding Foundation owner 确认，不能用私有字段捷径
    缩小 diff。
13. Prompt Run 写锁只保护 JSONL 完整性，不保护可用性；Prompt 写入失败仍可能影响 turn，这是
    保持既有 failure contract 的有意边界。若未来要 best-effort，必须单独评审敏感日志丢失语义。
14. AgentService/Pydantic 限制不控制解析前 raw HTTP body；缺少反向代理 body limit 的部署不能
    作为互联网暴露的生产配置验收。
15. SiliconFlow close 等待 active embed 完成，shutdown 可能持续到当前 batch/retry 的既有超时
    预算结束；这是避免关闭正在使用的 client 和结果竞态的必要取舍，不引入强制线程终止。
16. standalone qualification/RAG 的 close failure 若处理不当会覆盖 primary error；实现必须用
    §5.7.4 的显式优先级和故障注入锁定，不能写裸 `finally: bundle.close()`。

## 22. Manager approval 与 Embedding Foundation 确认项

Manager 已确认“没有新的架构阻断问题”，并批准整体设计及第二轮修订。本版将以下事项视为
冻结，不再请求第三轮完整架构复审：

1. admission 位于 `AgentService`/独立 runtime layer，Session lock key 改为
   `(user_id, session_id)`。
2. dialogue=2、LLM=2、Embedding=1；Session 5s / dialogue 0.25s / LLM 2s / Embedding 2s；
   Semantic coordinator state lock 同样采用 2 秒有界等待。
3. user `12/min burst 4`、IP `60/min burst 20`，只作用于 `POST /v1/chat`。
4. `RUNTIME_BUSY -> 503`、`API_RATE_LIMITED -> 429` 及 `Retry-After` 契约。
5. DeepSeek connect/read/write/pool=`3/15/5/1s`、max retries=1、read/write timeout 不 retry。
6. `semantic-eligibility/v1` 精确 skip、`httpx>=0.27,<1` 核心依赖、pre-admission nullable
   `turn_id`。
7. message `4000 chars / 12000 UTF-8 bytes`、metadata `8192 UTF-8 bytes`、
   `DEEPSEEK_MAX_OUTPUT_TOKENS=1024` 的成本硬上限。
8. `RuntimeResources` 状态机、幂等逆序关闭，以及 FastAPI/CLI/Memory Worker 最外层 lifecycle。
9. JSONL App/Trace 实例写锁与 `SafeAppLogger` / `SafeTraceLogger`；Prompt Log 不加入
   failure-safe wrapper。
10. 128 字符 safe-ASCII Request ID 规则，以及明确 trusted proxy、禁止
    `forwarded-allow-ips="*"` 的部署契约。
11. §4.1 的 surface+purpose 模型图：只有 CLI dialogue 可持有 fallback；CLI `/memory extract`、
    API dialogue/extraction、Memory Worker 直接使用 DeepSeek，同时共享各自进程的 gate。
12. `EmbeddingProviderRuntime(provider, close)` bundle 方向及 Factory 返回类型原子迁移；
    Embedding Foundation 已完成只读确认并批准。
13. `JsonlPromptRunLogger` 只增加实例写锁和并发逐行解析测试，不增加
    `SafePromptRunLogger`，不改变敏感/failure-safe 边界。
14. 原始 HTTP body 上限属于生产反向代理部署门禁。

Embedding Foundation 已完成以下定向确认项，并给出最终 `Approved`：

1. **SiliconFlow concrete 状态机**：确认 §5.7.2 的 condition、active-call、
   `OPEN/CLOSING/CLOSED`、closed error、`client=` external ownership、`client_factory=` owned
   ownership 和 close-failure terminal semantics。
2. **完整调用点迁移**：确认 §5.7.3/§16/§17 已覆盖 Factory、公开导出、Memory、qualification
   owner、RAG build/query、测试/fixture，且 `qualification.py` 保持纯 Port consumer。
3. **standalone close precedence**：确认 §5.7.4 在存在 primary error 时不覆盖，在仅 close 失败
   时返回非零安全 terminal outcome。

非阻断部署项已写入 §10.5：业务层限制不覆盖解析前 raw HTTP body；生产反向代理 body limit
是部署验收条件，但不扩大本轮运行时代码范围。

## 23. 实施门禁

Manager 与 Embedding Foundation 均已解除实现门禁。实施期间仍保持以下边界：

- 不修改 `.env`；
- 不更新 `docs/task_log.md` 状态；
- 不将本任务标记为 `done`；
- 不开始真实 DeepSeek/SiliconFlow 故障注入。

当前已进入实现和本地 fake/mock 故障注入，无需第三轮完整设计复审。只有实现、完整回归、
跨边界审查与 Manager 最终验收完成后，才能更新任务状态；当前任务仍未完成。真实 Provider
请求仍须 Manager 另行明确授权。
