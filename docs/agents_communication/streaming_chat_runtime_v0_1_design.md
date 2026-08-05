# Worker Session 19：Streaming Chat Runtime And API v0.1 设计方案

Date: 2026-08-04

Status: **Accepted by Manager / phased implementation authorized**

Revision: v0.2 design repair for Manager re-review

Task: T-0143

本轮只修订本文档。没有修改 `frontend/`、运行时代码、Persistence schema、生产 Web Profile、
部署配置、Character、Memory、RAG、Embedding、账号/邀请规则或 Manager 管理的任务状态；没有读取
`frontend/`，也没有读取或修改 `docs/codex_cross_session_messaging.md`。

## 0. 结论摘要

Manager 已批准独立 Streaming Port、共享 `DialogueTurnEngine` 和 SSE adapter 的架构方向；本修订版
解决首轮审查提出的六项合同缺口，并已通过重新审查。实施采用分阶段授权，具体边界见第19节。

推荐在现有同步路径旁新增真正的 streaming capability，并把 HTTP/SSE 保持为最外层 adapter：

```text
POST /v1/chat/stream
  -> existing request-id / explicit Auth mode / CSRF / chat rate limit
  -> AgentStreamService
       -> existing input limit policy
       -> idempotency lookup/reservation
       -> existing (user_id, session_id) dialogue admission
       -> shared DialogueTurnEngine
            -> Character / Session / user message / recent messages
            -> Semantic / Memory / Evidence / Context / Prompt
       -> provider-neutral StreamingChatModelClient
            -> same process-wide LLM gate
            -> DeepSeek streaming transport
       -> stream completion persistence UoW
       -> shared terminal trace/prompt-summary helpers
  -> SSE adapter
       -> domain events -> UTF-8 SSE frames
       -> heartbeat comment only
       -> disconnect -> cancellation
```

核心结论：

1. 保留 `ChatModelClient.chat(...) -> str`、`AgentService.handle_chat(...)`、CLI 和
   `POST /v1/chat`。新增独立 `StreamingChatModelClient` capability；生产 streaming 不得用
   “先同步生成完整文本，再伪装成多个 delta”的 adapter。
2. 不复制一套完整 `DialogueController`。把当前 Controller 的模型调用前准备、终态记录和输出
   组装抽成最小 `DialogueTurnEngine`；现有 `DialogueController` 继续作为同步 facade，
   `AgentStreamService` 复用同一 engine。
3. streaming request 必须携带 `client_message_id`。它与 HTTP `request_id` 不同：前者是持久的
   业务幂等身份，后者只关联一次传输尝试。fingerprint 必须在 server metadata merge 前，仅从
   validated client business payload 按冻结的 v1 algorithm 计算。
4. user message 与 `in_progress` 幂等记录在一次 transaction 中、Provider 前提交；partial text
   只保存在有界内存 accumulator，不写数据库。assistant message 只在完整成功后与
   `completed` 幂等状态一次 transaction 提交。
5. `turn.completed` 必须在 assistant commit 成功后发送；`turn.failed` 必须在 failed terminal
   state 尽力提交后发送。取消、断开或 Provider 中途失败均不保存 partial assistant。
6. 新增显式、thread-safe、idempotently closeable 的 admission handle；sync context manager 只是
   compatibility facade。不得跨线程搬运 context manager 的 `__enter__` / `__exit__`。handle 覆盖
   reserve/replay 到 terminal cleanup；LLM permit 覆盖 upstream open、全部 chunk 和 response close。
7. SSE heartbeat 只是 adapter comment，不进入领域事件、Trace、Prompt Log 或 idempotency状态。
8. 现有 schema 没有 chat idempotency record，也没有把 assistant/idempotency/relationship commit
   组合成 UoW。Manager 只原则批准本文第8节方向；在修订设计重新accepted并由Persistence owner
   获得明确schema/migration授权前不得实施。
9. DeepSeek `[DONE]` 只表示 SSE framing 结束。业务成功还要求唯一 choice 0 产生受支持的
   `finish_reason`；空 final answer 必须失败且不得保存 assistant。
10. 同步 producer 与 async SSE 之间使用 event-loop-safe bounded bridge；Uvicorn event-loop thread
    绝不调用阻塞式 `queue.Queue.get()`，idle/backpressure stream 不能阻塞 `/health` 或其他请求。
11. replay 在任何内容返回前重新验证 Session owner/lifecycle：ended 仅可只读 replay terminal
    outcome；archived 固定返回 `SESSION_ARCHIVED`，不得返回历史正文。
12. 当前 Controller 在保存 assistant 后才更新 State/Memory；后续失败可能留下 assistant。该顺序
   不满足冻结的 streaming persistence contract，不能只替换模型调用。第 5、8、18 节给出最小
   重构和已批准的窄 completion UoW。

## 1. 审查依据与当前链路理解

### 1.1 已阅读和核对

- `AGENTS.md`
- `docs/architecture.md`
- `docs/interfaces.md`
- `docs/task_log.md`
- `docs/web_mvp_contract.md`
- `docs/error_handling.md`
- `docs/observability.md`
- `docs/fastapi_service.md`
- `docs/deployment.md`
- `docs/agents_communication/runtime_guardrails_v0_1_design.md`
- `docs/agents_communication/browser_auth_invitations_v0_1_design.md`
- `docs/agents_communication/browser_auth_invitations_v0_1_manager_review.md`
- `docs/agents_communication/streaming_chat_runtime_v0_1_manager_review.md`
- `docs/task_log.md` T-0143 Manager design review entry
- 指定的 Controller、model、services、API、runtime、observability、errors 和 composition code

当前工作树包含已经验收的 T-0142 Browser Auth 改动及其他既有修改。本设计不回滚、整理或
重写这些用户/其他 Worker 的变更。

### 1.2 当前同步调用链

```text
CLI normal chat
  -> AgentService.handle_chat(...)
       -> ChatInputLimitPolicyV01
       -> InProcessDialogueAdmission.acquire((user_id, session_id))
       -> DialogueController.handle_user_message(...)

POST /v1/chat
  -> request-id middleware
  -> explicit dev_bearer | web_session Auth
  -> web_session unsafe request CSRF validation
  -> shared authenticated user/IP chat rate limiter
  -> AgentService.handle_chat(...)
```

`DialogueController.handle_user_message(...)` 当前按以下顺序执行：

```text
load/fallback Character
  -> ensure Session
  -> turn_started trace
  -> build Character context
  -> save user message
  -> load recent messages excluding current message id
  -> Semantic Query context
  -> Memory / Evidence
  -> PromptContext / Context Budget / PromptCompiler
  -> ResponseGenerator -> ChatModelClient.chat(...)
  -> save assistant message
  -> decide + persist relationship State
  -> decide + persist/enqueue Memory work
  -> turn_completed trace
  -> completed PromptRunRecord
```

失败时 Controller 写 `turn_failed`，在 prompt 已构建时写 failed PromptRunRecord，然后重新抛出。
Session validation 发生在 user message 前，符合 ended/archived 无副作用的现有同步合同。

### 1.3 当前可直接复用的能力

- `AgentService` 的 input limits 和 `(user_id, session_id)` admission。
- `ConcurrencyGate` 及共享 LLM gate；API dialogue 没有 fallback。
- `DeepSeekClient` 的 persistent `httpx.Client`、connect/read/write/pool timeout、retry 分类、
  `max_tokens` 和 lifecycle owner。
- `ApplicationRuntime` / `RuntimeResources` 的 `OPEN -> CLOSING -> CLOSED`、逆序、幂等关闭。
- Browser Cookie Auth、CSRF、explicit `dev_bearer | web_session`、dual credential fail-closed。
- API request-id、`ErrorInfo`、mapper、HTTP status、sanitizer、App/Trace/Prompt log ports。
- Session/Message/Relationship 的 raw SQLite 与 SQLAlchemy-on-SQLite store implementations。

### 1.4 不能直接复用或必须修正的缺口

1. `ChatModelClient` 和 `LimitedChatModelClient` 只有同步 `chat()`。
2. `DeepSeekClient._client.post(...)` 会读取完整 JSON response，没有 `stream=True` transport/parser。
3. Controller 的 prep/model/finalize 是一个方法，stream service 无法复用 prep/finalize 而不复制。
4. assistant 在 State/Memory 前保存；后置失败可能出现“failed turn + persisted assistant”。
5. `MessageStore.save_message(...)` 每次生成新 id 并独立 commit，没有 chat idempotency UoW。
6. 当前 schema、models 和 repositories 中不存在 `chat_request_idempotency`。
7. API middleware 在 `StreamingResponse` headers 返回时就记录 request completion；它不能代表
   stream 的总 duration，也捕获不到 iterator 内 terminal error。
8. Starlette 将同步 iterable 转为 thread-pool iteration，但其 bridge 不负责调用原 iterator 的
   `close()`；T-0143 必须由自己的 adapter/handle 保证关闭。
9. 当前 PromptRunRecord 以保存完整 messages/sections/response 为目的；Web streaming 明确禁止把
   prompt 或 stream content 写入 App/Trace/Prompt 输出。
10. Caddy 当前没有显式 response buffer/stream timeout 配置。官方 `reverse_proxy` 语义会对
    `text/event-stream` 自动立即 flush；不应盲目配置 `flush_interval -1`，因为该模式还会改变
    downstream disconnect 对 upstream cancellation 的传播语义。
11. `AgentService._turn_metadata(...)` 会把每次HTTP attempt不同的 `request_id` 注入 merged metadata；
    fingerprint 不能从这个merged object计算。
12. `DialogueAdmissionPort.acquire(...)` 只返回 context manager，yield的lease只有timing fields；它
    不是可从API thread转移给producer thread的closeable handle。
13. raw/SQLAlchemy relationship update都在transaction外读取current row再覆盖；两个不同Session可
    对同一user/character并发并丢失numeric delta。
14. 同步producer若让async generator直接调用blocking `queue.get()`，会阻塞唯一Uvicorn worker的
    event loop；必须使用明确的thread-to-loop bridge。

## 2. 目标、稳定不变量与非目标

### 2.1 目标事件语法

每个已开始的 domain stream 必须严格满足：

```text
turn.started
response.delta * 0..N
(turn.completed | turn.failed) exactly once
```

不变量：

- `turn.started` 是第一个领域事件。
- delta 顺序等于 Provider final-answer content 的接收顺序。
- terminal 后不能再产生 delta 或第二个 terminal。
- heartbeat 不属于该语法；它是 SSE comment。
- disconnect 可能使 terminal 无法送达已消失的客户端，但 service/persistence/trace 仍必须形成一个
  安全内部终态。重新使用同一 `client_message_id` 可以查询/replay durable outcome。

### 2.2 必须保持兼容

- CLI 继续调用同步 `AgentService.handle_chat(...)`。
- `POST /v1/chat` request/response 行为和错误合同保持。
- `ChatModelClient.chat(...) -> str` 保持。
- API surface 继续禁止模型 fallback；CLI-only fallback 不进入 stream graph。
- Character、Memory retrieval/write policy、active World Info、Semantic Query、Context Budget、
  PromptCompiler 和 State classifier 不改变策略。
- 单 Uvicorn worker、process-local locks/gates/rate buckets 和 `ApplicationRuntime.close()` 保持。

### 2.3 非目标

- 不启用生产 Web Profile，不修改 frontend。
- 不加入 WebSocket、LangGraph、tool/MCP、Audio、managed vector RAG 或 Memory Worker。
- 不做多 worker/node distributed lock、Redis、PostgreSQL 或 queue。
- 不实现中途文本的持久化、resume offset、`Last-Event-ID` 或任意 cursor replay。
- 不实现 D6、D7、D8，不修改账号和邀请规则。
- 不做精确 token/金额计费；T-0146 quota 仍是独立任务。
- 不让 HTTP、SSE、Cookie、Header、FastAPI 或 DeepSeek chunk DTO 进入 Controller/domain ports。

## 3. 推荐模块架构

### 3.1 分层

```text
app/model/
  client.py                 # existing ChatModelClient unchanged
  streaming.py              # provider-neutral streaming model schemas/port
  deepseek.py               # sync + streaming capability, raw SSE parser stays here

app/agent/
  turn_engine.py            # shared pre-model preparation + terminal orchestration
  dialogue_controller.py    # existing sync facade, public method unchanged

app/services/
  stream_schemas.py         # StreamChatRequest / public domain events / handle
  agent_stream_service.py   # idempotency/admission/cancellation/application orchestration

app/runtime/
  streaming.py              # active stream registry, producer/queue lifecycle

app/db/
  streaming ports/repositories/migration after Persistence approval

app/api/
  routes/chat_stream.py     # POST /v1/chat/stream
  sse.py                    # domain event -> SSE frame, heartbeat/disconnect only
```

### 3.2 责任边界

| Component | Owns | Must not own |
|---|---|---|
| `StreamingChatModelClient` | final-answer deltas, upstream close/cancel | HTTP/SSE, persistence, Session |
| `DialogueTurnEngine` | common Character/Session/context/prompt/final trace orchestration | Auth, idempotency, SSE, DeepSeek chunk |
| `DialogueController` | synchronous compatibility facade | streaming transport |
| `AgentStreamService` | input, idempotency, admission lifetime, accumulator, terminal choice | Cookie/CSRF/SSE framing |
| `StreamTurnPersistencePort` | atomic reserve/complete/fail CAS | retrieval/model/business policy |
| SSE adapter | framing, heartbeat, disconnect observation | domain terminal policy, persistence |
| `ActiveStreamRegistry` | owned producer handles and shutdown cancellation | application business decisions |

## 4. Interface 与 DTO 草案

以下草案冻结 responsibility，不冻结最终文件拆分或每个字段名。

### 4.1 Provider-neutral model streaming port

保留：

```python
class ChatModelClient(Protocol):
    def chat(self, *, messages: list[dict[str, str]]) -> str: ...
```

新增独立 capability：

```python
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass(frozen=True)
class ModelStreamRequest:
    messages: list[dict[str, str]] = field(repr=False)


@dataclass(frozen=True)
class ModelTextDelta:
    text: str = field(repr=False)


@dataclass(frozen=True)
class ModelStreamFinished:
    finish_kind: Literal['complete', 'output_limit']


ModelStreamEvent = ModelTextDelta | ModelStreamFinished


class ModelStreamSession(Protocol):
    def __iter__(self) -> Iterator[ModelStreamEvent]: ...
    def cancel(self, *, reason: str) -> None: ...
    def close(self) -> None: ...


class StreamingChatModelClient(Protocol):
    def open_stream(
        self,
        *,
        request: ModelStreamRequest,
    ) -> AbstractContextManager[ModelStreamSession]: ...
```

设计理由：

- context manager 显式表达 response/client resource ownership，避免一个裸 iterator 被遗忘关闭。
- `ModelTextDelta` 只允许 final answer text；不含 reasoning、usage、provider id、raw JSON 或 SSE。
- `ModelStreamFinished` 是 provider-neutral completion proof。`complete` 表示自然停止，
  `output_limit` 表示达到模型输出上限；后者由 service 转为稳定 warning
  `model_output_truncated`。DeepSeek adapter 只有在受支持的 `finish_reason` 和后续 `[DONE]` 都已
  验证后才发出它。
- filtered、tool call、resource interruption、unknown/missing finish reason、duplicate terminal、
  unsupported choice 或 EOF-before-`[DONE]` 都以安全 mapped exception fail closed，不伪造
  `ModelStreamFinished`。
- 同一个 `DeepSeekClient` 可以结构化实现同步和 streaming 两个 Protocol，或由两个 sibling adapter
  共享一个 owned transport；调用方只获得所需 capability。
- `MockStreamingChatModelClient` 用于 deterministic tests。
- 禁止生产 `BufferedSyncToStreamAdapter`。若测试需要兼容 fake，可限定在 test fixture，不能进入
  API runtime composition。

### 4.2 Streaming LLM gate wrapper

```python
class LimitedStreamingChatModelClient:
    def __init__(self, *, gate, delegate, app_logger): ...

    @contextmanager
    def open_stream(self, *, request):
        with gate.acquire():
            with delegate.open_stream(request=request) as stream:
                yield stream
```

permit 必须从 upstream open 前一直保持到 iterator terminal、response close 和 parser cleanup 完成。
DeepSeek retry attempt 不重复 acquire。API sync 和 stream wrapper 必须引用 **同一个** runtime
`ConcurrencyGate` 对象，不能各建一个 limit=2 的 gate，否则合计并发会翻倍。

### 4.3 Application request 与 domain events

```python
@dataclass(frozen=True)
class StreamChatRequest:
    session_id: str
    character_id: str
    message: str = field(repr=False)
    client_message_id: str
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TurnStarted:
    event_type: Literal['turn.started']
    request_id: str
    turn_id: str
    session_id: str


@dataclass(frozen=True)
class ResponseDelta:
    event_type: Literal['response.delta']
    delta: str = field(repr=False)


@dataclass(frozen=True)
class TurnCompleted:
    event_type: Literal['turn.completed']
    turn_id: str
    status: Literal['completed']
    warnings: list[str]
    state_delta: dict[str, int]


@dataclass(frozen=True)
class PublicStreamError:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class TurnFailed:
    event_type: Literal['turn.failed']
    turn_id: str
    error: PublicStreamError


ChatStreamEvent = TurnStarted | ResponseDelta | TurnCompleted | TurnFailed
```

明确不进入 event：

- prompt/messages；
- reasoning content；
- Memory/Evidence/Character 正文或 source ids；
- provider model/chunk/body/finish payload；
- user input、metadata、Cookie/token；
- usage/vector；
- final full response 的重复副本。

`turn.completed` 不重复发送 response；浏览器从有序 delta 累积。事件语法允许零个 delta，但
Manager 已拒绝空回答成功：即使 Provider 给出受支持的 completion proof，final answer 为空也必须
`turn.failed`，且不得插入空 assistant message。Provider 未发送完整 completion proof 同样是
invalid/incomplete stream failure。

### 4.4 Closeable application handle

```python
class StreamEventSink(Protocol):
    def put_from_producer(
        self,
        *,
        event: ChatStreamEvent,
        timeout_seconds: float,
    ) -> None: ...

    def close_from_producer(self) -> None: ...


class ChatStreamHandle(Protocol):
    request_id: str
    turn_id: str
    session_id: str
    started_event: TurnStarted

    def start(self, *, sink: StreamEventSink) -> None: ...
    def cancel(self, *, reason: str) -> None: ...
    def close(self) -> None: ...


class AgentStreamServicePort(Protocol):
    def stream_chat(
        self,
        *,
        user_context: UserContext,
        request: StreamChatRequest,
    ) -> ChatStreamHandle: ...
```

`stream_chat(...)` 在返回 handle 前完成所有 pre-stream checks。`ChatStreamHandle` 是 application
lifecycle object，不是 HTTP response/SSE generator。`StreamEventSink` 只认识 domain events 和有界
put/close，不认识 asyncio、ASGI、HTTP 或 SSE；API adapter 提供 event-loop-safe sink implementation。
`started_event` 由service预先创建而不是adapter伪造；body在running loop先把它放入空bridge，再调用
只能成功一次的`start()`。因此即使producer thread创建失败，post-header domain sequence仍能以
started后failed收束；任何模型delta/terminal都排在started之后。

handle 不能隐式保存一个已 `__enter__` 的 context manager。它只持有明确的 admission handle、
registered producer 和 cancellation state；这些对象均有独立、可测试的 close contract。

### 4.5 Cancellation token

```python
class CancellationToken:
    def request_cancel(self, *, reason: str) -> bool: ...
    def is_cancelled(self) -> bool: ...
    def reason(self) -> str | None: ...
```

reason 只允许：

```text
user_cancelled
client_disconnected
sse_generator_closed
consumer_slow
runtime_closing
```

token 不携带 Request/Cookie/body，也不向 Provider 发送任意用户字符串。

### 4.6 Transferable admission handle

现有 `DialogueAdmissionPort.acquire(...) -> AbstractContextManager[DialogueAdmissionLease]` 不可直接
跨线程持有。Runtime Guardrails 需要新增显式 capability：

```python
class DialogueAdmissionHandle(Protocol):
    @property
    def lease(self) -> DialogueAdmissionLease: ...

    def close(self) -> None: ...


class DialogueAdmissionPort(Protocol):
    def open_handle(
        self,
        *,
        request: DialogueAdmissionRequest,
    ) -> DialogueAdmissionHandle: ...

    def acquire(
        self,
        *,
        request: DialogueAdmissionRequest,
    ) -> AbstractContextManager[DialogueAdmissionLease]: ...
```

冻结合同：

- `open_handle()` 同步完成 Session lock 与 dialogue permit acquire；timeout 在 HTTP headers 前抛出。
- 返回对象是 `OPEN -> CLOSING -> CLOSED` 状态机，以 per-handle `RLock`/condition 保护。first closer
  记录closing thread；同线程reentrant `close()`立即no-op返回，其他线程看到CLOSING可bounded等待
  CLOSED，CLOSED后的调用直接返回。producer/runtime跨线程或任意repeated/concurrent close最终只释放
  一次底层资源。
- 底层 Session entry reference、Session lock 和 dialogue permit 都由 handle 按逆序释放；实现必须提供
  明确的 cross-thread release primitive，不能保存 generator context 并从另一线程调用 `__exit__`。
- `acquire()` 保持现有同步 API：在调用线程内 `handle = open_handle()`，yield `handle.lease`，并在
  `finally` 调用 `handle.close()`。`AgentService` 与同步 CLI/`/v1/chat` 不改 public behavior。
- close 不在 registry/global lock 内执行外部 callback；某一步 close 失败也继续释放剩余资源，最终只
  记录 content-safe close error。

Runtime Guardrails owner 必须与 streaming implementation 一起 review 此变更；它不是 SSE adapter 的
私有技巧，也不得由 adapter 手工操作 admission internals。

## 5. Controller 与共同编排的最小重构

### 5.1 为什么需要重构

若新增一份 `StreamingDialogueController` 并复制当前 300 多行主流程，会产生两套 Session、
Semantic、Memory、Evidence、Context Budget、Trace、Prompt 和 State 行为，之后任何修复都容易漂移。

若完全不重构，只把 `ResponseGenerator.generate()` 替换为 iterator，又无法做到：

- Provider 前原子 reserve user/idempotency；
- terminal 前原子 assistant/idempotency commit；
- partial failure 不保存 assistant；
- 同步和 streaming 共用 prompt preparation；
- stream-specific cancellation/terminal lifecycle。

### 5.2 推荐 `DialogueTurnEngine`

从 `DialogueController` 抽取以下 provider-neutral stages：

```python
class DialogueTurnEngine:
    def preflight(
        self,
        *,
        turn_input: AgentTurnInput,
        turn_id: str,
    ) -> TurnPreflight: ...

    def prepare(
        self,
        *,
        preflight: TurnPreflight,
        user_message_id: str,
    ) -> PreparedDialogueTurn: ...

    def decide_completion(
        self,
        *,
        prepared: PreparedDialogueTurn,
        assistant_response: str,
    ) -> TurnCompletionPlan: ...

    def record_completed(...): ...
    def record_failed(...): ...
```

`TurnPreflight` 只含 turn/user/session/active character/warnings 等安全身份；
`PreparedDialogueTurn` 包含 prompt/messages/retrieved content，必须 `repr=False`，只存在当前 turn
内存中，不可序列化到 idempotency、App Log 或 public event。

职责拆分：

1. `preflight`：Character load/fallback 和现有 Session ensure；Streaming Persistence reserve 仍在
   transaction 内再次验证 Session，preflight 不是 durable authority。
2. `prepare`：从已存在的 `user_message_id` 开始，复用 recent exclusion、relationship read、
   Semantic、Memory、Evidence、PromptContext、Context Budget、PromptCompiler 和 safe trace。
3. `decide_completion`：复用 State classifier、Memory classifier、warnings 和 output DTO 组装所需
   信息；不处理 HTTP/SSE。
4. terminal record helpers：保证 Trace/Prompt summary 每轮只有一个 terminal。

### 5.3 同步 facade 保持兼容

```text
DialogueController.handle_user_message(existing AgentTurnInput)
  -> engine.preflight(new turn_id)
  -> existing MessageStore.save_message(user)
  -> engine.prepare(...)
  -> existing ResponseGenerator.generate(...)
  -> existing synchronous completion adapter
  -> AgentTurnOutput
```

公开 method signature、CLI 和 `/v1/chat` 不变。本任务不要求同步接口支持 `client_message_id`。
同步 completion 的行为若不需要满足 streaming persistence contract，可以先保持现状，以降低
回归面；但共享 prep/trace helper 必须用 characterization tests 锁定。

### 5.4 Streaming path 复用

```text
AgentStreamService
  -> engine.preflight(turn_id)
  -> StreamTurnPersistence.reserve(...) -> existing user_message_id
  -> engine.prepare(...)
  -> StreamingChatModelClient.open_stream(...)
  -> accumulate + emit deltas
  -> engine.decide_completion(...)
  -> StreamTurnPersistence.complete(...)
  -> approved post-commit Memory behavior
  -> engine.record_completed(...)
```

该设计只为模型边界、durable commit 和 transport lifecycle 分叉，不复制 Character/Memory/RAG/
Semantic/Prompt 收集链。

### 5.5 Manager-approved narrow completion UoW

当前 State 和 Memory ports 各自 commit，无法与 assistant/idempotency transaction 自动组成一个
全 turn transaction。Manager 已冻结 v0.1 窄 UoW：

1. `decide_completion` 先计算纯 `StateUpdate` / `MemoryDecision`；
2. Persistence UoW 在transaction内读取最新relationship row、apply/clamp delta，并原子提交
   assistant、latest relationship State 和 idempotency completed；
3. Memory writer/enqueue 在 response durable commit 与 admission release 后调用，并作为有deadline/
   work bound的best-effort ancillary work；它不得无限延迟terminal delivery；
4. Memory failure只写安全 warning/App Log，不把已经 durable completed 的 stream 改回 failed。

理由：首个 Web Profile 明确关闭 long-term Memory；assistant/State/idempotency 是用户可见完成
语义的核心，Memory extraction scheduling 不应造成“assistant 已保存但 turn.failed”。

该选择会让 streaming path 的 Memory write failure 从 fatal 变为 non-fatal，与当前同步路径有细微
差异；这是已批准的streaming-specific合同，不授权顺带修改sync completion semantics。

## 6. Turn 和事件状态机

### 6.1 Durable request state

```text
ABSENT
  -> IN_PROGRESS                 reserve transaction: user + request record
       -> COMPLETED              success UoW: assistant + state + terminal metadata
       -> FAILED                 provider/prep/cancel/disconnect failure CAS

IN_PROGRESS --process restart/orphan recovery--> FAILED
COMPLETED / FAILED are immutable terminal states
```

不允许 `FAILED -> IN_PROGRESS`。用户要主动再试必须创建新的 `client_message_id`。

### 6.2 Domain event emitter state

```text
CREATED
  -> STARTED                     emit turn.started exactly once
       -> STARTED                emit zero or more response.delta
       -> COMPLETED_TERMINAL      emit turn.completed once
       -> FAILED_TERMINAL         emit turn.failed once
```

实现使用一个 lock-protected terminal flag。所有 producer exception 都汇入一个 terminal helper；
parser、persistence、logger 或 adapter 不能各自随意 emit terminal。

### 6.3 Completion/cancel race linearization

`StreamTurnPersistence.complete(...)` 和 `.fail(...)` 都必须执行
`WHERE status='in_progress'` CAS：

- completion transaction 先赢：assistant/State/completed durable。即使随后 disconnect，不能改为
  failed；同 key replay 返回 completed。
- cancellation/failure transaction 先赢：record failed，不插入 assistant；迟到 completion 必须
  看到 CAS miss 并丢弃 accumulator。
- 两者都不能成功，不能出现两个 terminal durable outcome。

### 6.4 零 delta

事件 grammar 允许 `turn.started` 后零个 delta，但不保证成功。Manager 已冻结空回答失败；为复用现有
sync validity rule，service用`answer.strip()` **仅判断是否blank**，但不修改已经发送/将持久化的原始
answer。零delta、empty或whitespace-only accumulator的任何Provider terminal都产生
`turn.failed(MODEL_INVALID_RESPONSE, retryable=false)`，不得插入assistant。连接EOF、JSON结束或
response close但没有完整completion proof同样映射`MODEL_INVALID_RESPONSE`；不能为了满足grammar
伪造delta。

## 7. 调用、成功、失败与取消时序

### 7.1 新请求成功

```text
API adapter
  request-id -> Auth -> CSRF(if web) -> shared chat rate limit -> DTO validation
  -> AgentStreamService.stream_chat()
       validate immutable client payload/client_message_id
       fingerprint v1 before server metadata merge
       merge request_id into turn metadata; apply existing input limits
       acquire bounded/ref-counted per-key lock            [held; may wait <= Session timeout]
       dialogue admission open_handle                      [held]
       engine preflight + current owner/lifecycle check
       transaction: lookup/replay or active Session reserve
       release per-key lock
       return ChatStreamHandle
  -> send HTTP 200 SSE headers
  -> OwnedStreamingResponse.__call__ / async body
  -> bind AsyncStreamBridge to running loop
  -> bridge.put_initial(handle.started_event)
  -> producer starts; any producer event is ordered after turn.started
  -> producer: engine.prepare
  -> LLM gate acquire                                    [held]
  -> DeepSeek response open                              [owned]
  -> response.delta ...
  -> unique choice 0 accepted finish_reason
  -> [DONE] -> provider-neutral ModelStreamFinished
  -> DeepSeek response close
  -> LLM gate release
  -> require non-empty final answer
  -> engine.decide_completion
  -> atomic assistant + latest-row relationship delta + completed commit
  -> dialogue admission handle close
  -> bounded Memory ancillary work; no durable-outcome reversal
  -> terminal Trace / summary Prompt record
  -> event turn.completed
  -> handle deregister/close
```

### 7.2 Provider 首 delta 前失败

```text
turn.started
  -> prepare or upstream open/read/parse failure
  -> map_exception
  -> failed CAS, no assistant
  -> terminal trace/prompt summary
  -> turn.failed
  -> response/LLM/dialogue/handle cleanup
```

### 7.3 Provider 部分 delta 后失败

```text
turn.started -> delta A -> delta B
  -> provider failure
  -> discard in-memory "AB" after terminal bookkeeping
  -> failed CAS, user retained, no assistant
  -> turn.failed
  -> cleanup
```

浏览器可暂时显示 A/B 并标记失败，但 reload/history 只能看到 durable user message；partial assistant
不进入 history、State、Memory、Prompt Log 或 retry result。

### 7.4 用户点击停止 / fetch abort

v0.1 没有额外 cancel endpoint。Web frontend 的 Stop 会 abort 当前 fetch；HTTP adapter 将其归类为
`user_cancelled`（若本地 UI 主动 signal 可知）或 `client_disconnected`：

```text
SSE async generator finally
  -> handle.cancel(allowlisted reason)
  -> cancellation token set once
  -> AsyncStreamBridge closes/cancels pending put future
  -> best-effort close upstream response
  -> producer exits parser/queue wait
  -> failed CAS if completion has not won
  -> no assistant / no partial persistence
  -> release LLM gate and admission handle exactly once
  -> bounded join/deregister
```

客户端已经断开时，`turn.failed` 无法保证送达网络；内部 event state、persistence、Trace/App Log
仍为 failed/cancelled。若 completion commit 已经先完成，则 Stop 只能造成“completed terminal 未送达”，
不能回滚 durable answer；同 key reconnect 会 replay completed。

### 7.5 SSE generator 被关闭但 socket 未明确断开

route 在 `AgentStreamService.stream_chat()` 返回后先持有 handle 的唯一 ownership：

1. 构造 `AsyncStreamBridge` / `OwnedStreamingResponse` 的任一步失败，route 的 `except/finally` 立即
   `cancel(reason='sse_generator_closed')` 并 `close()`；reserved turn 不会丢失 admission handle。
2. response 构造成功后做一次显式 ownership transfer；`OwnedStreamingResponse.__call__` 在
   send-start、body iteration、ASGI cancellation、`GeneratorExit` 或 send `OSError` 的统一 `finally`
   中 cancel/close。
3. body iteration 开始时才调用 `handle.start(sink=bridge)`；因此 response construction failure 不会
   启动 Provider。handle 同时已注册于 runtime registry，shutdown 是最后一道确定清理边界。

不得依赖 garbage collection，也不得让普通 `StreamingResponse` 的 iterator bridge 猜测何时释放
domain/runtime resources。

### 7.6 Application shutdown

`ActiveStreamRegistry` 作为 runtime-owned resource 在 DeepSeek client 之前关闭：

```text
RuntimeResources reverse close
  -> active_stream_registry.close()
       reject new streams
       cancel all handles(runtime_closing)
       bounded join
  -> DeepSeekClient.close()
  -> remaining resources
```

注册顺序必须使 registry 在 reverse close 中先于 transport。`ApplicationRuntime.close()` 仍是现有
同步、幂等 API，不新增第二套 `aclose()`。

## 8. Persistence change request（Blocking）

### 8.1 请求原因

冻结的 Web contract 要求：

- same account/session/client id 不调用 Provider 两次；
- user message 与 request record Provider 前提交；
- assistant 只在完整成功后提交；
- complete event 在 assistant commit 后；
- crash/reconnect 可得到确定 outcome。

现有 `messages` 只有独立 `save_message()`，没有 request id、turn status 或跨表 UoW。因此 T-0143
需要新增 persistence record。该方向已原则批准，但本轮及重新审查前均未授权schema实施。

### 8.2 建议新增 table

建议 migration/revision：`streaming_chat_idempotency_v0_1`。

```sql
CREATE TABLE chat_request_idempotency (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    active_character_id TEXT NOT NULL,
    request_fingerprint_version INTEGER NOT NULL,
    request_fingerprint TEXT NOT NULL,
    turn_id TEXT NOT NULL UNIQUE,
    user_message_id TEXT NOT NULL,
    assistant_message_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    owner_runtime_id TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    state_delta_json TEXT NOT NULL DEFAULT '{}',
    terminal_error_code TEXT,
    terminal_retryable INTEGER,
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    failed_at TEXT,
    UNIQUE (user_id, session_id, client_message_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
    FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE RESTRICT,
    FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE RESTRICT
);
```

最终 DDL 必须增加完整 status/nullable coherence CHECK、indexes 和 startup schema drift validation。
是否增加 `accounts(id)` FK 由 Persistence owner 根据当前 legacy/backfill contract 决定；不能只在一个
backend 加 FK。

字段规则：

- 不保存 response 副本；completed replay 通过 `assistant_message_id` 读取 message。
- `request_fingerprint_version` v0.1 固定为 integer `1`；lookup 必须同时比较 version + digest，不能
  让未来算法静默解释旧 row。
- `active_character_id` 保存 Character fallback/解析后的实际角色 ID；原始 requested
  `character_id` 只进入 fingerprint v1，不得被该字段替代或覆盖。
- `request_fingerprint` 是 domain-separated deterministic SHA-256，只覆盖 merge 前 immutable
  idempotency payload：exact `character_id`、exact `message`、validated client-supplied `metadata`。
  它明确排除 `request_id`、Auth/IP data、timestamps、runtime id、rate-limit/admission summaries 和
  所有 server-derived fields。它只用于 conflict compare，绝不进入日志/public DTO/error。
- user message 正文仍只保存于 existing `messages`；不能保存 canonical preimage 副本。
- `terminal_reason` 只允许固定内部 reason enum，不保存 exception text/body。
- `warnings_json/state_delta_json` 只存 public terminal DTO 所需的安全数据。
- `owner_runtime_id` 是每次 ApplicationRuntime 创建的随机 opaque id，用于识别 restart orphan；
  不含 host/IP/PID/secret。
- 不保存 partial text、delta、prompt、reasoning、provider chunk、Cookie/token 或 raw exception。

### 8.3 建议 persistence port

```python
class StreamTurnPersistencePort(Protocol):
    def resolve_or_reserve(
        self,
        *,
        request: StreamTurnResolveOrReserve,
    ) -> StreamTurnResolveOutcome: ...

    def complete(
        self,
        *,
        request: StreamTurnComplete,
    ) -> StreamRequestRecord: ...

    def fail(
        self,
        *,
        request: StreamTurnFail,
    ) -> StreamRequestRecord: ...

    def fail_orphan_if_owned_by_other_runtime(...): ...
```

### 8.4 Transaction contract

`resolve_or_reserve` 必须在一个 consistent transaction/read contract 中，不能向service暴露一个可绕过
Session lifecycle的裸`lookup()`：

1. 先读取Session并验证owner/character；ownership mismatch不查询/暴露idempotency row。
2. archived立即`SESSION_ARCHIVED`，不得读取assistant content。
3. 查询unique scope并比较fingerprint version/digest。ended只允许same-fingerprint completed/failed
   terminal read-only replay；absent/in_progress返回`SESSION_ENDED`且无写入。
4. active existing row返回conflict/in-progress/replay/orphan outcome；active absent才插入exactly one user
   message和`in_progress` record。
5. replay outcome读取referencedassistant时必须与Session status检查处在同一consistent contract；任一步
   fault全部rollback/不返回content。

调用方在整个resolve/replay delivery期间持有Session admission handle；未来end/archive transition也
必须经过同一boundary，使lifecycle change不能越过检查后正在发送的历史正文。

`complete` 必须：

1. `WHERE id/turn_id/status='in_progress'` CAS；
2. transaction内重新确认Session仍active；正常路径由admission保证，若发现违规并发生lifecycle
   change则fail closed、无assistant；
3. 插入 exactly one assistant message；
4. 在 **同一 transaction 内** 读取 `(user_id, active_character_id)` 最新 relationship row（不存在则按共享
   默认值创建），把本 turn 的 trust/affection/familiarity delta 加到最新值并 clamp；不得使用 prompt
   preparation 时读取的 snapshot 作为 update base；
5. 按现有 policy 写 mood/user_emotion/last_topic，保留最新 `current_scene`；多个 Session 并发时数值
   delta 不丢失，非加法字段保持 current completion last-commit-wins 语义；
6. 更新 record 为 completed、写 assistant id/warnings/state delta/timestamp；
7. commit 后才允许 `turn.completed`。

raw SQLite 使用短 `BEGIN IMMEDIATE` 保护 read-latest/apply/write；SQLAlchemy-on-SQLite 必须进入等价
write transaction 后再 query latest row。clamp、default、mood/emotion/topic/current-scene 规则放在两套
backend 共用的 pure policy/helper 中，不能复制出 backend-specific 业务语义。

`fail` 必须：

1. CAS `in_progress -> failed`；
2. 只写 error code/retryable/allowlisted reason/timestamp；
3. 不插入 assistant，不保存 partial response；
4. commit 后才尝试 emit `turn.failed`。

### 8.5 Backend/migration gate

- raw SQLite 与 SQLAlchemy-on-SQLite 必须通过同一 store contract suite。
- migration incremental、idempotent，不重建或删除 existing Auth/Session/Message/Memory/State rows。
- marker 存在仍做 exact schema/trigger/index introspection，遵守 T-0142 fail-closed precedent。
- startup orphan recovery 只做 indexed/bounded update；不扫描/记录 message content。
- D6 后续必须纳入新 table/revision，但 T-0143 不实现 D6。
- 未经 Persistence/Manager批准，不得先在某个 backend 写临时 table 或把 key 塞进 message metadata。

### 8.6 Retention

v0.1 推荐 idempotency record 与 dialogue Session 同寿命；当前没有 browser hard delete，因此不设置
短 TTL，也不在普通 request 清理。若删除 terminal row，旧 key 可能再次调用 Provider，违反冻结
合同。未来 retention 必须保留不可重复使用 tombstone，或明确升级 contract，不能仅按 24h/30d
删除。

## 9. 幂等、重复提交与重连语义

### 9.1 两类 ID

| ID | Source | Scope/lifetime | Meaning |
|---|---|---|---|
| `request_id` | header or server generated | one HTTP attempt | logs/error correlation |
| `client_message_id` | browser generated | account + dialogue Session lifetime | one intentional send |

`client_message_id` 建议完整匹配：

```text
^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$
```

不 trim、不 Unicode normalize；invalid value 是 pre-stream `INVALID_INPUT`。key 本身可出现在 safe
debug DTO 吗由 Manager 决定；默认 App/Trace 不记录它，只记录 `idempotency_outcome`。

### 9.2 Scope

唯一 scope 固定为：

```text
(authenticated user_id, dialogue session_id, client_message_id)
```

`user_id` 只来自 `UserContext`。`character_id` 和 request fingerprint 是冲突验证字段，不是 unique
scope 的替代。跨用户即使 key/session text 相同也不能读取彼此 outcome。

### 9.3 Fingerprint v1（冻结）

`IdempotencyPayloadV1` 必须在 `_turn_metadata(...)` 或任何 server-derived enrichment 之前，由 validated
`StreamChatRequest` 深拷贝并冻结：

```text
version = 1
character_id = exact validated request.character_id
message = exact validated request.message
metadata = exact validated client-supplied request.metadata
```

不含 `session_id/client_message_id`（它们已在 unique scope 中），也不含 `request_id`、UserContext/Auth、
IP、timestamp、runtime owner、admission/rate data 或任何 server field。之后可以为 prompt/trace 单独构造
merged turn metadata，但不得反向修改 payload。

为避免语言/runtime JSON serializer 漂移，v1 先把 payload 递归编码为 type-tagged canonical tree：

```text
null       -> ["null"]
boolean    -> ["bool", true|false]
integer    -> ["int", base10-string]
finite f64 -> ["float64", IEEE-754-big-endian-lowercase-hex]
string     -> ["str", exact-string]
array      -> ["array", child-nodes-in-source-order]
object     -> ["object", [[exact-key, child-node], ... sorted by key UTF-8 bytes]]
```

字符串不 trim、不 Unicode normalize；array 顺序保留；NaN/Infinity 在 validation 阶段拒绝。canonical
tree 再以 UTF-8、`ensure_ascii=false`、无额外空白的 JSON 编码，最后计算：

```text
SHA-256("AEMEATH_CHAT_REQUEST_FINGERPRINT_V1\0" UTF-8 bytes || canonical-tree bytes)
```

数据库持久保存 `request_fingerprint_version=1` 和 digest。实现必须有 committed golden vectors，覆盖
Unicode、key ordering、nested arrays、int/float/negative-zero，并证明 request_id A/B digest 相同，
message 或 client metadata 改变则 digest 不同。未来算法只能新增 version，不能重解释 v1 rows。

### 9.4 Per-key admission 与 Session admission

不能在 Session ownership/lifecycle validation 前用 durable record 返回 outcome 或正文。顺序固定为：

1. Auth、rate、DTO/input validation 和 fingerprint v1。
2. 获取 process-local per-scope key entry。registry 是 thread-safe、ref-counted、zero-ref立即删除，且有
   hard cap（v0.1建议128）；cap 满时 fail-closed `RUNTIME_BUSY`，不能无限增长。
3. 持有 key lock 时调用 `DialogueAdmissionPort.open_handle()`；Session wait 最长沿用现有5秒。
4. admission 内验证 Session owner/current lifecycle，再在 transaction 内 lookup + fingerprint compare，
   对 active absent row 执行 reserve，或建立允许的 replay handle。
5. durable decision 后释放 key lock/entry；admission handle 由 stream/replay handle 持有到 terminal。

因此相同 key 的 concurrent duplicate **可能等待 key owner 的 Session admission，最长达到现有5秒
wait budget**；不再承诺立即 rejection。它不会等待首个请求的完整 Provider stream，因为 reserve
commit 后 key lock 立即释放。不同 key 的同 Session 请求继续按现有 Session lock 串行。

### 9.5 Session lifecycle 与 outcome matrix

所有 lookup/replay 都 owner-scoped，并在读取 assistant content 前检查当前 Session。terminal replay
也持有 admission handle；它不占 LLM gate或调用 Provider，但会与同 Session lifecycle/turn 串行。
未来 end/archive endpoint 必须复用同一 admission boundary，避免 validation 后 archive overtaking replay。

| Current Session | Durable row | Same fingerprint | Result | Provider call |
|---|---|---:|---|---:|
| ownership mismatch | any | any | `SESSION_OWNERSHIP_DENIED`；不暴露row存在性 | 0 |
| archived | any | any | `SESSION_ARCHIVED`；不读取/返回历史answer | 0 |
| ended | completed/failed | yes | read-only replay existing terminal outcome | 0 |
| ended | absent/in_progress | any | `SESSION_ENDED`；不得reserve/recover/provider | 0 |
| active | absent | n/a | reserve/start | 1 |
| active | in_progress, live local owner | yes | `CHAT_REQUEST_IN_PROGRESS` 409 + Retry-After | 0 |
| active | completed | yes | replay started + bounded deltas + completed | 0 |
| active | failed | yes | replay started + failed | 0 |
| active/ended | any existing | no | `CHAT_REQUEST_CONFLICT` 409 | 0 |
| active | in_progress orphan | yes | atomic interrupted fail, then failed replay | 0 |

Session status、owner、record 和 referenced assistant 必须在 backend contract 定义的 consistent read/
transaction 中取得；archived race 不能在 validation 后泄露正文。Completed replay 可按 public delta size
重新切片，不保证原 Provider chunk boundary。API chat limiter仍先扣额度，replay 占 admission但不占
LLM gate。

### 9.6 网络中断后重试

- 原 stream 已 durable completed：same key replay completed answer。
- 原 stream 仍 live：same key 409 in-progress，不 attach 到内存 queue，不调用第二次 Provider。
- 原 stream 在断开时 failed/cancelled：same key replay same failed machine outcome。
- 用户选择“重新生成”是新的 intentional attempt，必须生成新 key；旧 user message仍保留。
- request_id 每次重连可以不同；fingerprint v1明确排除它，因此不影响 business dedupe。

### 9.7 Crash orphan

每个 runtime 有 `owner_runtime_id`，ActiveStreamRegistry 维护本进程 live turn ids：

- lookup 到其他 runtime id 的 in-progress record：CAS fail 为 `runtime_interrupted`。
- lookup 到当前 runtime id 但 registry 无 live handle：CAS fail 为 `local_stream_orphaned`。
- startup 可以 bounded fail 上一 runtime 的 in-progress records；request-time CAS仍是后备。
- 不能自动重新调用 Provider，因为 crash 前是否已经计费/生成不可知。

## 10. Provider streaming、输出边界与 reasoning 隔离

### 10.1 DeepSeek request/parse

DeepSeek streaming 仍使用现有 persistent client 和 `/chat/completions`，payload 在同步基础上增加：

```json
{
  "stream": true,
  "stream_options": {"include_usage": false}
}
```

保留 `max_tokens=DEEPSEEK_MAX_OUTPUT_TOKENS`。provider adapter 使用
`httpx.Client.stream(...)`/manual streaming response，并在 context `finally` close。

parser 规则：

- 只接受 `data:` framing；`data: [DONE]` **只标识 framing终止**，本身不证明业务成功。
- 整个 stream 只支持 choice `index == 0`。empty choices 仅可作为 usage-only chunk忽略；任一非零
  index、同 chunk 多 choices、跨 chunk 多 choice identity 都 fail closed。
- choice 的 `finish_reason=null` 可出现在普通 delta；adapter 必须跟踪 exactly one non-null terminal
  reason，terminal 后又出现 content/reason 或 duplicate reason 均为 invalid response。
- `stop` + non-blank accumulated final answer + 后续 `[DONE]`：发出
  `ModelStreamFinished(finish_kind='complete')`。
- `length` + non-blank accumulated answer + 后续 `[DONE]`：发出
  `ModelStreamFinished(finish_kind='output_limit')`；service 固定加入 public warning
  `model_output_truncated`，完整已有文本才可持久化。
- `content_filter` 映射 `MODEL_INVALID_RESPONSE` / allowlisted reason `model_content_filtered`；
  `tool_calls` 或 `delta.tool_calls` 映射 `MODEL_INVALID_RESPONSE` /
  `model_tool_calls_unsupported`；两者均 `retryable=false` 且不保存 partial。
- `insufficient_system_resource` 映射现有 `MODEL_UNAVAILABLE` /
  `model_insufficient_system_resource`；不把 partial 当成功。
- unknown/missing reason、`[DONE]` before accepted reason、EOF-before-`[DONE]`、malformed JSON/schema、
  非法 content type全部映射 `MODEL_INVALID_RESPONSE`，不记录 raw line/body。
- JSON chunk 只把 choice 0 的 `delta.content` string/null 转为 answer delta。
- `reasoning_content` 永远忽略，不转为 ModelTextDelta、不累积、不记录。
- final content 不调用 `.strip()`；delta byte sequence/line break保持 provider final answer 原样。

Manager 拒绝空回答成功，所以 adapter 即使观察到 accepted reason + `[DONE]`，service 仍在 UoW 前
检查 accumulator 非空；空 answer 形成 `MODEL_INVALID_RESPONSE`，不插入 assistant。DeepSeek 原始
`finish_reason`、chunk id、provider model 或 system fingerprint都不进入 domain event/persistence/log。

### 10.2 Retry

- connect failure、HTTP 408/429/5xx 可以沿用现有 bounded retry，但仅限还未打开一个 200 stream。
- 一旦收到 200 headers 或任意 stream bytes，不自动 retry；否则可能重复计费或重复 delta。
- read/write timeout、parse error、partial disconnect均不 retry，转 `turn.failed`。
- API streaming 不安装 fallback。

### 10.3 输出硬上限

Provider `max_tokens` 是第一层。建议再加本地 accumulator 上限：

```text
AEMEATH_STREAM_RESPONSE_MAX_CHARS=32768
AEMEATH_STREAM_RESPONSE_MAX_UTF8_BYTES=98304
AEMEATH_STREAM_PUBLIC_DELTA_MAX_UTF8_BYTES=4096
```

- 字符/byte同时计算，不 trim、不截断 durable answer。
- 单个 provider delta 可按 Unicode code point安全切成多个 public delta。
- cumulative 超限立即 cancel upstream，并以稳定 `MODEL_INVALID_RESPONSE` + internal
  `stream_output_limit_exceeded` failed；不保存 partial。
- config 必须 positive/bounded 并 startup fail-fast。

### 10.4 有界内存与 slow client

生产者不直接写 ASGI socket。建议每个 handle 一个 bounded queue：

```text
capacity = 32 domain events
put timeout = 5 seconds
```

max dialogue/LLM=2 使 producer thread 数受现有 gate约束。queue 满并超时视为
`consumer_slow` cancellation，防止慢客户端无限占用 Session/LLM permit。Manager 已 provisionally
批准这些默认值；测试必须使用 injected clock/queue，不用真实 sleep。

## 11. 资源所有权与无泄漏合同

### 11.1 Ownership table

| Resource | Owner | Acquire | Release |
|---|---|---|---|
| per-key ref-counted lock | `AgentStreamService` preflight | after fingerprint | durable decision finally |
| Session/dialogue handle | `ChatStreamHandle` | `open_handle()` before lookup/reserve | terminal/cancel/close finally |
| LLM permit | `LimitedStreamingChatModelClient` | before upstream open | after response close finally |
| httpx stream response | DeepSeek `ModelStreamSession` | `client.stream` enter | parser/cancel/error finally |
| shared httpx Client | `ApplicationRuntime` | composition | RuntimeResources close once |
| producer thread | `ActiveStreamRegistry` | body start | handle close/runtime close |
| asyncio bridge | `OwnedStreamingResponse` | body start/running loop | terminal/disconnect/send finally |
| accumulator | producer | first delta | success commit or failure discard |
| Trace run | `DialogueTurnEngine` | turn.started | exactly one completed/failed record |
| Prompt summary run | `DialogueTurnEngine` | prompt prepared | exactly one summary terminal if applicable |

### 11.2 Producer model

当前 domain/model/storage 主路径是同步代码。每个 accepted handle 由一个 bounded producer thread执行
core iteration；async adapter 通过明确的 `AsyncStreamBridge` 消费。v0.1 冻结实现为：

```text
event loop side: asyncio.Queue[BridgeEnvelope](maxsize=32)
producer side:   asyncio.run_coroutine_threadsafe(queue_put(...), owning_loop)
                 -> concurrent Future.result(timeout=5s) only blocks producer
consumer side:   await queue.get() / asyncio.wait_for(..., heartbeat interval)
```

禁止在 Uvicorn event-loop thread调用 `queue.Queue.get()`、`Future.result()`、同步 iterator next、
`time.sleep()` 或 httpx sync read。详细合同：

- `OwnedStreamingResponse.__call__` 在 running loop创建 bridge，先以loop-side nonblocking
  `put_initial(handle.started_event)`放入唯一started，再`handle.start(sink=bridge)`；start只生成owned
  producer thread并立即返回，不在event loop运行core work。
- 同一个FIFO bridge之后才接收producer delta/terminal，因此started ordering不依赖thread scheduling。
  thread start/submission失败必须通过non-event-loop terminalization path形成failed CAS和
  `turn.failed`；不得在event loop同步访问DB或运行Controller。
- producer 每次 `run_coroutine_threadsafe` 返回的 pending Future 在 bridge lock下登记；put超过5秒则
  cancel Future、触发 `consumer_slow`、关闭 upstream 并走 failed terminal cleanup。
- async consumer 用 cancellable `await queue.get()`；heartbeat使用 async timeout后发送 SSE comment，
  不向 queue写 domain heartbeat，也不使用真实 sleep/poll阻塞loop。
- normal producer enqueue唯一 domain terminal envelope并标记`close_after_delivery=true`；consumer yield该
  terminal后停止，不再等待额外item。private `BRIDGE_ABORTED` sentinel只用于disconnect/runtime abort
  在无可投递terminal时唤醒idle consumer，永不转换成domain/SSE event。一个shared terminal
  arbiter/CAS拒绝cancel/failure/completion race产生第二terminal。
- disconnect、ASGI cancel、runtime close或send failure调用 `bridge.abort_from_any_thread()`：通过
  `loop.call_soon_threadsafe` 在 loop中标记closed、取消pending put、丢弃非terminal backlog并放入
  private sentinel，同时设置 CancellationToken/close upstream，分别唤醒 async consumer 和 sync
  producer。bridge closed 后的新put立即失败，不永久阻塞。
- 客户端已消失时 domain terminal 可无法入网；producer仍独立完成 durable CAS、Trace terminal和
  admission/LLM cleanup。若 durable completed 已先赢，只记录delivery failure。
- thread由 registry登记，不创建 unowned fire-and-forget task；response/handle/runtime close均可找到它。

thread 数受 dialogue limit 2 硬限制；runtime shutdown cancel全部并在现有 DeepSeek read timeout
预算内 join。实现不得依赖 daemon thread自然消失作为正确性机制。

event-loop responsiveness 是 acceptance contract：一个 stream 在 Provider idle 或 queue backpressure
期间，`/health` 和另一个不应被同 Session serialization阻塞的 authenticated request 必须在独立短
deadline内完成。测试用 barriers/fake blocking stream制造状态，不用 `sleep()` 猜测时序。

### 11.3 Close precedence

- 正常 business/provider error优先；close error只进入安全 `resource_name/error_type` summary，不能
  覆盖原 terminal error。
- 若业务成功但 response close失败发生在 complete commit前，stream failed，无 assistant。
- complete commit已成功后，adapter send/close失败只改变 delivery status，不回滚 completed。
- logger error由 SafeApp/SafeTrace隔离，不能阻止 gate/lock/response close。
- Manager 已批准 streaming Prompt summary logger 为 best-effort；其失败不改变 durable outcome，且
  stream terminal cleanup必须置于更外层 finally，确保 admission/gate/response/bridge均释放。

## 12. Runtime Guardrails 集成

### 12.1 Session serialization

- 新 request继续使用现有 `(user_id, session_id)` key。
- explicit admission handle覆盖 lookup/reserve/replay、prepare、全部 delta、completion/failure commit和
  cleanup；允许 acquiring/API thread 与 producer/runtime close thread不同。
- 同 Session不同 key不能并行 Provider或改变历史顺序。
- terminal replay为保证当前lifecycle不被archive overtaking，仍占 admission handle到replay terminal；
  它不占LLM gate。

### 12.2 Global dialogue/LLM/Embedding gates

- stream占一个 dialogue permit直到 terminal cleanup。
- stream只在模型阶段占一个 shared LLM permit；sync chat、stream chat和同 runtime extraction合计
  不超过 existing limit。
- Semantic Query仍走 existing Embedding gate=1；stream不能直接调用 inner provider。
- producer/provider exception、cancel、disconnect、queue full、SSE close、Prompt logger failure均有
  release fault tests。

### 12.3 Input/metadata/output

- `AgentStreamService`先验证/冻结 client payload并算 fingerprint v1，再产生 final merged turn metadata；
  `request_id`可以继续进入后者，但永远不能进入前者。
- 现有 `ChatInputLimitPolicyPort`继续验证message和 final merged metadata；fingerprint canonical tree不
  计入业务metadata size，也不能替代原input limit。
- HTTP Pydantic可做相同早拒绝，但 Service是权威。
- Caddy 128 KiB raw body limit不变。
- DeepSeek `max_tokens`不变；新增 accumulator char/byte limits不替代 token cap。

### 12.4 API rate limit

`/v1/chat`和`/v1/chat/stream`必须共享同一个 authenticated user/IP token bucket和operation budget，
不能为 stream另建 bucket造成额度翻倍。Cookie和dev Bearer都在 Auth成功后使用同一 limiter。

### 12.5 CLI fallback isolation

- CLI仍只使用 sync dialogue wrapper和approved fallback allowlist。
- API sync/stream都直接使用 DeepSeek/mock explicit mode，不安装 fallback。
- `AEMEATH_USE_MOCK_LLM=true`是显式测试/离线provider，不是 fallback；可以提供真实的 mock delta
  sequence。

### 12.6 ApplicationRuntime close

- ApplicationRuntime新增 `agent_stream_service`和owned ActiveStreamRegistry引用。
- `close()` public shape不变，仍幂等同步关闭。
- unknown/new stream在 registry CLOSING后必须 fail-fast；registry cancel active handles并通过显式
  admission handle cross-thread close，不能调用遗留 context manager `__exit__`。
- one Uvicorn worker contract继续是生产前提；多 worker会破坏 Session lock、live-owner判断和gate。

## 13. FastAPI 与 SSE adapter

### 13.1 Endpoint

```text
POST /v1/chat/stream
Content-Type: application/json
Accept: text/event-stream
```

request body：

```json
{
  "session_id": "session_...",
  "character_id": "aemeath",
  "message": "...",
  "client_message_id": "cm_...",
  "metadata": {}
}
```

Response headers：

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store
X-Request-ID: req_...
```

不设置 hop-by-hop `Connection` header，不返回 `Content-Length`。不使用 `EventSource`-specific GET
或 browser auto-reconnect；frontend使用 `fetch()`读取 stream。

route的pre-header `AgentStreamService.stream_chat()`包含同步admission/SQLite/Controller preflight，
因此endpoint应保持FastAPI sync `def`（由其threadpool调度），或显式使用cancellation-safe AnyIO
thread offload；禁止在`async def` event-loop thread直接调用这些blocking operations。返回response后，
只有`OwnedStreamingResponse.__call__`和async bridge运行在event loop。

### 13.2 SSE frames

```text
event: turn.started
data: {"request_id":"req_...","turn_id":"turn_...","session_id":"session_..."}

event: response.delta
data: {"delta":"..."}

event: turn.completed
data: {"turn_id":"turn_...","status":"completed","warnings":[],"state_delta":{"trust":0,"affection":0,"familiarity":0}}

event: turn.failed
data: {"turn_id":"turn_...","error":{"code":"MODEL_TIMEOUT","message":"...","retryable":true}}

: heartbeat

```

- 每个 data是一行 compact JSON，JSON负责转义换行；frame以空行结束。
- 不输出 provider event id或`retry:`；v0.1没有offset resume。
- `id:`字段省略，避免暗示`Last-Event-ID`可以恢复原 chunk。
- heartbeat默认15秒，仅在 started后且没有domain event时由 adapter发出；不写日志/计数为turn event。

### 13.3 Auth / CSRF / dev Bearer

endpoint复用现有 `get_rate_limited_user_context`：

- `web_session`：Cookie resolve + exact Origin/Referer + JSON + Fetch Metadata +
  `X-CSRF-Token`；Authorization即使单独出现也拒绝。
- `dev_bearer`：Bearer compatibility；不接受 Cookie，不要求CSRF。
- Cookie+Bearer：`AUTH_CREDENTIAL_CONFLICT`，不隐式选择。
- production Web Profile仍未在T-0143启用；实现endpoint不等于授权切换部署profile。
- production同源CORS仍为空。若dev CORS未来需要`X-CSRF-Token`，必须在Web profile任务单独批准，
  不能在本轮扩大当前allowlist。

### 13.4 Pre-stream 与 post-stream errors

在构造/返回 `StreamingResponse` 前完成：

- Auth / dual credential / Cookie resolve；
- Origin/content/fetch/CSRF；
- authenticated chat rate limit；
- Pydantic body；
- Service message/metadata/client id limits；
- immutable client payload + fingerprint v1；
- bounded/ref-counted per-key entry与 explicit dialogue admission handle；
- Character/Session owner/current lifecycle preflight；
- transaction内 lifecycle recheck + idempotency replay/conflict/in-progress/reserve decision；
- archived固定 `SESSION_ARCHIVED`、ended仅允许existing terminal read-only replay。

这些失败使用现有 JSON ErrorResponse、真实 HTTP status、request id和Retry-After，不发送SSE。

reserve成功且HTTP headers已发送后，任何 prepare/provider/parser/persistence/cancel failure只能形成一个
安全 `turn.failed`，HTTP status保持200。iterator exception不得逃出成为Caddy 502/raw traceback。

Manager 已批准 Prompt summary logger 为best-effort：post-header logger failure只写safe logger
failure summary，不改变durable terminal，也不能延迟/阻止resource cleanup。现有同步Prompt Log仍
保持原fail-loud behavior。

### 13.5 Client disconnect detection

adapter同时依赖：

- `OwnedStreamingResponse.__call__` 观察ASGI cancellation或send `OSError`；
- async body `finally`；
- cancellable async queue wait/heartbeat timeout间隔内的 `request.is_disconnected()` check。

任一信号调用幂等 `handle.cancel(...)` 和 `bridge.abort_from_any_thread()`。event loop只做async
wait/cancel/sentinel处理，不同步等待producer；producer/handle owner做 durable terminal、upstream、
LLM和admission cleanup，避免双release。

### 13.6 Caddy

当前Caddy官方文档说明`reverse_proxy`识别`Content-Type: text/event-stream`后会立即flush，不使用
普通response buffering。因此推荐：

- 不配置`response_buffers`；
- 不必为SSE设置`flush_interval -1`；该low-latency模式还会使Caddy在client提前断开时不取消
  backend request，反而妨碍本任务的disconnect propagation；
- 不设置短于应用Provider/read/graceful budget的`stream_timeout`或
  `response_header_timeout`；当前无短timeout可保持；
- 保留h1/h2、128 KiB body limit、trusted proxy和no access log合同；
- heartbeat 15s应小于任何后续cloud/LB idle timeout；D8必须实测，不在T-0143假设。

若实现不需Caddyfile修改，则预计只新增targeted Caddy streaming test和文档；不能为了“看起来像
SSE配置”而加入会削弱disconnect的`flush_interval -1`。

## 14. Error 与 observability策略

### 14.1 新错误建议

| ErrorCode | HTTP before stream | retryable | Use |
|---|---:|---:|---|
| `CHAT_REQUEST_IN_PROGRESS` | 409 | true | same key live |
| `CHAT_REQUEST_CONFLICT` | 409 | false | same key different request |
| `CHAT_STREAM_CANCELLED` | n/a after headers | false | user/consumer/runtime cancellation replay |
| `CHAT_STREAM_INTERRUPTED` | n/a or replay SSE | false | orphan/runtime restart |

Provider、Session、Storage、RUNTIME_BUSY、API_RATE_LIMITED和INVALID_INPUT继续复用现有code。
Manager 已冻结 `CHAT_STREAM_CANCELLED` / `CHAT_STREAM_INTERRUPTED` 的 public
`retryable=false`。用户有意识地“重新生成”必须由UI生成新 `client_message_id`；same key始终只
replay，不再次执行Provider。

### 14.2 安全App Log事件

```text
agent_stream.request_reserved
agent_stream.started
agent_stream.completed
agent_stream.failed
agent_stream.cancelled
agent_stream.disconnected
agent_stream.replayed
agent_stream.in_progress_rejected
agent_stream.idempotency_conflict
runtime.stream_registry_closing
runtime.stream_close_failed
model.deepseek_stream_completed
model.deepseek_stream_failed
```

允许字段：

```text
request_id, turn_id, session_id, user_id, character_id,
event type, status, error_code, safe reason,
duration_ms, time_to_first_delta_ms,
delta_count, response_char_count, response_utf8_bytes,
queue_peak, wait_ms, attempt_count, retry_count,
idempotency_outcome, delivery_status
```

不记录 `client_message_id`/fingerprint默认值；如Manager要求correlation，只记录独立domain-separated
不可逆摘要，不能记录message fingerprint。

### 14.3 Trace

复用现有turn内部trace语义并增加content-free：

```text
turn_started
semantic_context_prepared
context_sources_selected             # ids/counts only
prompt_context_built                 # section ids/counts only
response_stream_started
response_stream_completed|failed     # count/length/duration only
state_update_decided                 # deltas/reason enum only
memory_write_decided                 # ids/status/reason enum only
turn_completed|turn_failed exactly one
```

cancel/disconnect都以`turn_failed`为Trace terminal，payload status可以是`cancelled`或
`client_disconnected`。若completion durable后才disconnect，Trace terminal保持completed，另写App
delivery event，不产生第二个turn_failed。

### 14.4 Prompt Log

Web/API streaming无论`AEMEATH_API_ENABLE_PROMPT_RUN_LOG`是否误开，都不能写full prompt或stream
content。Manager 已批准复用`PromptRunLoggerPort`写summary-only、best-effort `PromptRunRecord`：

- `messages=[]`
- `sections=[]`
- `response=None`
- metadata只含section ids/counts、context budget safe summary、delta/char count
- status completed/failed；cancel通过error code/reason表达

现有CLI/sync Prompt Log合同不变。streaming不再保留“完全不写”的实现分支；Prompt inspector需要
兼容empty内容。logger failure只形成safe App Log summary，不改变durable outcome或资源释放。

### 14.5 明确禁止

任何App/Trace/Prompt/HTTP error/CLI stderr都禁止：

- response delta或final response正文；
- prompt、compiled messages、user input；
- reasoning content；
- provider raw chunk/body/header；
- Cookie、Set-Cookie、Authorization、CSRF、token、API key；
- Memory/RAG/Character正文；
- vector/query；
- raw IP、request body、metadata values；
- raw exception body/repr/SQL params；
- idempotency request fingerprint。

sanitizer是第二层，所有新event先按allowlist构造。

### 14.6 Outcome table

| Outcome | Durable state | Domain terminal | Trace terminal | App status |
|---|---|---|---|---|
| success delivered | completed | completed | completed | completed/delivered |
| provider/prep fail | failed | failed | failed | failed |
| user cancel before commit | failed | failed if channel alive | failed(cancelled) | cancelled |
| disconnect before commit | failed | cannot guarantee delivery | failed(disconnected) | disconnected |
| disconnect after commit | completed | may be undelivered | completed | completed/disconnected_after_commit |
| runtime restart orphan | failed | failed on same-key replay | failed(interrupted) | interrupted/recovered |

## 15. 预计修改文件

### 15.1 新增（Manager批准后）

```text
app/model/streaming.py
app/agent/turn_engine.py
app/services/stream_schemas.py
app/services/agent_stream_service.py
app/runtime/streaming.py
app/api/sse.py
app/api/streaming_response.py
app/api/routes/chat_stream.py
app/db/streaming_chat_migrations.py
app/db/repositories/stream_turns.py
tests/test_model_streaming.py
tests/test_agent_stream_service.py
tests/test_stream_runtime_resources.py
tests/test_api_chat_stream.py
tests/test_sse_adapter.py
tests/test_stream_admission_handle.py
tests/test_stream_event_loop_responsiveness.py
tests/test_streaming_chat_migrations.py
tests/store_contract/test_streaming_chat_contract.py
```

### 15.2 修改

```text
app/model/deepseek.py
app/model/mock.py
app/model/__init__.py
app/agent/dialogue_controller.py
app/agent/ports.py
app/services/__init__.py
app/runtime/resources.py
app/runtime/guardrails/admission.py
app/runtime/guardrails/concurrency.py
app/runtime/guardrails/ports.py
app/runtime/guardrails/schemas.py
app/api/dependencies.py
app/api/schemas.py
app/api/routes/__init__.py
app/api/server.py
app/api/errors.py
app/main.py
app/config.py
app/db/schema.sql                         # only after Persistence approval
app/db/models.py
app/db/sqlite_store.py / facade typing
app/db/sqlalchemy_store.py / facade typing
app/db/store_factory.py
app/errors/codes.py
app/errors/http.py
app/observability/schemas.py
app/observability/prompt_log_inspector.py
.env.example
tests/test_dialogue_controller.py or existing controller suites
tests/test_agent_service.py
tests/test_api.py
tests/test_deepseek_client.py
tests/test_runtime_concurrency.py
tests/test_runtime_admission.py
tests/test_runtime_resources.py
tests/test_observability.py
tests/test_prompt_run_logger.py
```

`deploy/Caddyfile`只有在targeted test证明现有自动SSE flush不足时才提出窄改；当前推荐不改。
后续实现完成后新增delivery report；Manager-owned architecture/interfaces/task status由Manager决定
是否更新。

### 15.3 明确不修改

```text
frontend/**
docs/codex_cross_session_messaging.md
Character/Memory/RAG/Embedding business strategy
production Web Profile activation
D6 backup/restore
account/invitation rules
LangGraph/tool/MCP/Audio
```

## 16. 测试矩阵

### 16.1 Domain event grammar

- normal multi-delta: started, deltas in order, one completed。
- zero delta + accepted finish reason + `[DONE]`: started, failed, no assistant。
- delta before started rejected by emitter invariant。
- producer thread start/submission failure仍为started后唯一failed，event loop不执行terminal DB work。
- delta/terminal after terminal rejected；concurrent failure/cancel只一个terminal。
- heartbeat不出现在domain iterable/trace/idempotency。

### 16.2 Provider

- DeepSeek valid SSE lines、blank/comment、content null、usage-only、`[DONE]` framing。
- choice 0 only；nonzero/multiple choice、duplicate/missing/unknown finish reason、content after reason失败。
- `stop` non-empty成功；`length` non-empty成功且唯一 `model_output_truncated` warning。
- `content_filter`、tool_calls/delta.tool_calls、`insufficient_system_resource`分别映射冻结的安全失败。
- `[DONE]` without accepted reason、accepted reason without `[DONE]`、empty/whitespace-only answer均失败
  且无assistant；accepted raw whitespace边界不被trim，replay与首次delivered answer一致。
- reasoning-only chunk不产生public delta，unique marker不泄漏。
- first delta前connect/status/read/JSON failure。
- partial delta后timeout/malformed/EOF-before-DONE。
- no retry after 200/stream bytes；connect/eligible status bounded retry before stream。
- HTTP response/client close exactly once on success/error/cancel。
- `max_tokens`始终存在；local char/byte boundary和Unicode-safe delta split。
- API graph没有FallbackChatModelClient；explicit mock stream可控。

### 16.3 Controller reuse/regression

- sync Character fallback、Session validation、user exclusion、Semantic single context、Memory、Evidence、
  Context Budget、prompt section order、State、Memory decision与当前characterization一致。
- sync CLI和`/v1/chat` public DTO/error不变。
- static test防止第二份完整StreamingDialogueController或HTTP import进入agent/model ports。
- prepared turn敏感DTO repr不含prompt/user/response。

### 16.4 Persistence/store contract（两个backend）

- reserve一次提交user+in_progress；任一步fault全部rollback。
- reserve transaction内ended/archived/ownership拒绝且零side effect。
- complete一次提交assistant+latest relationship+completed；fault rollback且无assistant。
- 两个Session并发更新同user/character，transaction内read latest + apply/clamp保证所有numeric delta保留；
  mood/emotion/last_topic/current_scene保持shared policy语义，两个backend parity。
- fail保留user、无assistant/partial。
- concurrent reserve same key只有一个user row/turn/provider winner。
- completion/cancel barrier race只一个CAS terminal。
- assistant message exactly once；completed event一定在commit后。
- fingerprint v1 golden vectors；不同request_id same digest/replay；message/client metadata变化conflict；
  server metadata/Auth/IP/timestamp/runtime id不影响digest。
- completed replay从assistant id读取；failed replay不调用provider。
- in-progress live、current-runtime orphan、previous-runtime orphan。
- cross-user lookup不泄露row；active replay；ended terminal read-only replay；ended absent/in-progress拒绝；
  archived任何row都 `SESSION_ARCHIVED` 且不返回content；end/archive/lookup lifecycle races。
- migration fresh/upgrade/restart/introspection/drift/FK/CHECK/index parity。
- existingAuth/Session/Message/Memory/State rows不变。
- retention不删除dedupe authority。

### 16.5 Cancellation/resource

- user cancel before first delta、afterpartial delta、duringqueue wait、just beforecomplete。
- client disconnect、SSE generator close、ASGI send OSError/GeneratorExit。
- admission handle same-thread reentrant close、cross-thread close、repeated/concurrent close、underlying
  release failure continuation；sync context-manager facade behavior不变。
- response/bridge construction failure、response从未开始iteration、send-start failure都关闭reserved
  handle，不调用Provider。
- DeepSeek close解除blocked fake read；若close也失败仍释放gates。
- Session lock、dialogue permit、LLM permit在每个success/fault/cancel路径归零。
- ActiveStreamRegistry close拒绝new、cancel all、bounded join、then DeepSeek close。
- repeated/concurrent handle cancel/close和ApplicationRuntime.close幂等。
- Prompt/App/Trace logger fault不泄漏lock/permit/response。
- slow client queue full产生bounded cancel，不永久占permit。
- bridge normal terminal self-closes delivery；private abort sentinel不进入domain；cancel/failure race
  唯一terminal；abort同时唤醒async consumer和blocked sync producer。

### 16.6 Concurrency/Guardrails

- same Session不同keys严格不重叠。
- same key simultaneous duplicate可等待per-key owner最多Session admission budget，但不等完整stream；
  ref-counted key entry zero-ref删除，hard-cap满fail-closed。
- differentSession在dialogue limit内并发。
- sync chat + stream chat + extraction合计LLM active不超过existing limit。
- Semantic/Embedding仍受gate=1。
- message 4000/4001 chars、12000/12001 bytes、metadata 8192/8193、invalid JSON values。
- rejected input/idempotency conflict/in-progress无Provider和额外message。
- `/v1/chat`与`/v1/chat/stream`共享user/IP bucket；CLI不进HTTP limiter。
- idle Provider和full/backpressured bridge期间，single Uvicorn worker的`/health`及另一个不受同Session
  serialization限制的authenticated request在短deadline内响应；event loop无blocking queue/sleep/read。

### 16.7 Auth/API/SSE

- Web Cookie success、missing/revoked/expired、CSRF missing/wrong、Origin/Referer/Fetch matrix。
- dev Bearer compatibility；web mode拒Bearer，dev mode拒Cookie，dual fail-closed。
- Auth/CSRF/rate/validation/Session/admission/idempotency pre-stream errors是JSON ErrorResponse和正确
  HTTP status/Retry-After。
- post-header错误只有safe turn.failed并close，HTTP仍200。
- exact UTF-8 SSE frame、JSON newline escape、one blank separator、no `id:`/`retry:`。
- request id在header、started和error一致；client_message_id不替代request id。
- heartbeat在idle时出现、无content、不开启second terminal。
- disconnect after completed commit的same-key reconnect replay。

### 16.8 Observability zero leak

为user text、delta、reasoning、provider raw line、prompt、Memory、Evidence、Character、Cookie、CSRF、
Bearer、API key、raw IP、exception body、request fingerprint分别使用unique marker，扫描：

- HTTP body/headers（授权的delta正文只允许出现在对应SSE response.delta）；
- App Log；
- Trace Log；
- Prompt Log；
- mapped errors；
- test stderr/stdout；
- idempotency terminal fields。

App/Trace/Prompt均不得含stream正文；只允许客户端请求的SSE `response.delta`带answer text。

### 16.9 Caddy/部署（后续授权环境）

- Caddy下首delta实时到达，不等response结束。
- heartbeat实时到达。
- browser abort使upstream/request及时cancel；验证未设置会抑制cancel的`flush_interval -1`。
- 128 KiB body boundary不回退；h1/h2均通过。
- one Uvicorn worker、restart orphan recovery、graceful shutdown active stream。
- 不启用生产Web Profile、不调用真实Provider，除非Manager另行授权。

### 16.10 回归命令（实施阶段）

```powershell
pytest -q <focused streaming/controller/store/api suites> `
  --basetemp .pytest_tmp/streaming_chat_focused -p no:cacheprovider

pytest -q --basetemp .pytest_tmp/streaming_chat_full -p no:cacheprovider
ruff check . --exclude frontend
python -m compileall -q app tests
python scripts/deployment/check_deploy_lock.py
git diff --check
```

设计轮没有运行pytest/real Provider/browser/Caddy smoke；实现不能引用历史657/658结果作为fresh
T-0143 acceptance evidence。

## 17. 风险与 Deferred

### 17.1 主要风险

1. **Controller refactor regression**：阶段抽取可能改变trace/prompt/order。用sync characterization和
   public method unchanged降低。
2. **assistant/State/Memory atomicity**：现有ports不是全turn UoW。必须先批准第5.5/8节，不在代码
   中临时best-effort。
3. **SQLite write contention**：stream持Session lock时间长，但DB transaction只在reserve/terminal
   短时持有；不在整个Provider stream持DB transaction。
4. **client slow持permit**：bounded queue/put timeout取消；不可无限buffer。
5. **sync HTTPX cancellation latency**：cross-threadresponse close和15s read timeout是退出上界；fake
   blocking tests必须验证，不声称即时强杀。
6. **crash ambiguity**：不自动Provider retry；orphan持久failed，避免重复费用。
7. **replay chunk boundary**：只保证answer等价和event grammar，不保证原Provider chunk boundary。
8. **Prompt Log drift**：streaming summary-only与sync contentful合同不同；必须清晰测试和文档化。
9. **middleware duration**：现有api.request_completed只代表headers/adapter返回。stream总duration以
   AgentStreamService terminal log为准，不能混用。
10. **Caddy误配置**：强制`flush_interval -1`会弱化disconnect propagation；需用actual proxy test而非
    经验配置。
11. **single-process guarantee**：owner registry和per-key lock不跨worker；生产仍必须workers=1。
12. **cross-thread admission release**：必须通过显式handle/state machine实施和fault-test；跨线程调用
    context manager exit会造成未定义ownership，明确禁止。
13. **event-loop starvation**：任何blocking queue/get/read/sleep进入async consumer都会阻塞单worker；
    bridge responsiveness test是acceptance gate。
14. **fingerprint drift**：serializer/refactor可能破坏replay；version column + golden vectors永久锁定v1。
15. **relationship lost update**：Session lock不是user/character lock；只有completion transaction内读取
    latest row并apply delta才能避免跨Session覆盖。

### 17.2 Deferred

- attach到live stream、event offset、`Last-Event-ID`、partial resume。
- 保存partial assistant/draft状态。
- explicit cancel endpoint和跨tab cancel；v0.1 Stop使用fetch abort。
- distributed idempotency/live registry和multiworker。
- strict total turn deadline/circuit breaker/adaptive concurrency。
- exact provider usage/token/cost、T-0146 quota reserve/settle。
- provider usage/token/cost public DTO；provider-neutral finish proof已纳入v0.1，不再Deferred。
- Caddy/cloud load-balancer final idle settings和real-browserE2E（T-0146/D8）。
- productionWeb Profile activation、frontend实现、D6/D7。
- Message/Relationship/Memory全turn transaction；v0.1已冻结narrow completion UoW。

## 18. Manager 决策落实与重新审查门禁

### 18.1 已冻结决策

1. **Persistence direction**：`chat_request_idempotency`原则批准，但当前仍不得实施；本版已加入
   fingerprint version、Session lifecycle和latest-row completion合同，等待Persistence owner后续授权。
2. **Completion UoW**：采用窄UoW，assistant + latest relationship update + idempotency completed原子
   提交；Memory是post-commit、bounded、best-effort ancillary work。
3. **Controller**：批准单一`DialogueTurnEngine`和同步facade，禁止复制streaming controller。
4. **Replay/retention**：terminal record随Session同寿命、无短TTL、same key不重跑Provider；ended只读
   replay，archived固定拒绝且不返回正文。
5. **Errors**：四个public error方向批准；`CHAT_STREAM_CANCELLED`和
   `CHAT_STREAM_INTERRUPTED`固定`retryable=false`。
6. **Resource defaults**：heartbeat 15s、bridge capacity 32、slow-consumer timeout 5s、response
   32768 chars/98304 bytes、public delta 4096 bytes provisionally approved，须bounded config/fault tests。
7. **Empty response**：zero-delta success被拒绝；空answer只能`turn.failed`且无assistant。
8. **Prompt Log**：streaming固定summary-only、best-effort；sync/CLI合同不变。
9. **Caddy**：依赖`text/event-stream`自动flush；不配置`flush_interval -1`，仍需真实proxy fault test。

### 18.2 本次 Changes Requested 的修复位置

| Required repair | Revised contract |
|---|---|
| volatile request_id fingerprint | 8.2、9.3、12.3、16.4 |
| finish_reason / empty answer | 4.1、4.3、6.4、10.1、16.1-16.2 |
| transferable admission | 4.4、4.6、7.5、11-12、16.5 |
| event-loop-safe bridge | 4.4、7.1/7.4、11.2、13.5、16.5-16.6 |
| replay Session lifecycle | 8.4、9.4-9.5、13.4、16.4 |
| latest relationship row | 5.5、8.4、16.4 |

### 18.3 Manager 重新审查结论

六项 repair 已 accepted，per-key registry hard cap v0.1 固定为 `128`。Streaming Worker 获准实施
S1 与 S3；Persistence schema/migration 只授权给 Persistence owner 实施 S2。S4-S7 必须等待两条
前置交付均通过 Manager checkpoint，当前 Worker 不得自行修改 task status。

## 19. 实施门禁与建议顺序

当前状态是 **design accepted / phased implementation authorized**。实施顺序冻结为：

```text
Track A / Worker 19:      S1 characterization tests + DialogueTurnEngine extraction
Track A / Worker 19:      S3 provider-neutral model stream + DeepSeek/mock parser
Track B / Persistence:    S2 migration/ports/store contracts
Manager checkpoint:       accept S1/S2/S3 contracts and regression evidence
Track A / Worker 19:      S4 AgentStreamService + idempotency/admission/cancellation/runtime registry
Track A / Worker 19:      S5 SSE/API/Auth/CSRF/rate integration
Track A / Worker 19:      S6 observability/error/Caddy targeted tests
Track A / Worker 19:      S7 focused/full regression and delivery report
```

Persistence S2必须由Persistence owner执行/审查raw SQLite与SQLAlchemy parity。Worker 19 在
checkpoint前不得进入S4-S7。每阶段都必须保持sync CLI和`/v1/chat`通过。Worker不更新T-0143为
done；最终状态由Manager在delivery review后决定。

## 20. 参考

- Manager Review：`docs/agents_communication/streaming_chat_runtime_v0_1_manager_review.md`
- DeepSeek Chat Completion streaming：<https://api-docs.deepseek.com/api/create-chat-completion/>
- HTTPX streaming responses：<https://www.python-httpx.org/quickstart/#streaming-responses>
- FastAPI `StreamingResponse`：<https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse>
- Caddy `reverse_proxy` streaming/flush：<https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming>
