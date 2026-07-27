# Worker Session 15：Semantic Query v0.1 设计方案

日期：2026-07-27

任务：T-0135 Semantic Query 共享层

状态：Changes Requested；已按 2026-07-27 Manager review 修订，等待复审；尚未实施运行时代码

## 0. 结论摘要

本设计建议新增中立的 `app/semantic/` 模块，在一次 dialogue turn 内创建一个显式传递的
`TurnSemanticContext`。它负责：

1. 接收当前用户输入、已经排除当前消息的近期消息，以及由 composition root 启用的具名
   query role；
2. 按版本化、确定性规则和独立的 `SemanticQueryBudget` 构造有界中立 query，不使用 LLM rewriting；
3. 对 canonical query 做轮内去重，通过一次 batch-first
   `EmbeddingProviderPort.embed(..., purpose="query")` 调用生成向量；
4. 校验完整 exact signature、结果数量和向量，再把同一结果映射给 Memory、未来 canonical
   RAG 和未来 Character example selector；
5. 只在当前 Python 调用栈/未来 workflow 的非持久化 state 中存活，turn 结束即释放。

推荐保留多个具名 role，但 v0.1 的所有 role 默认使用同一个 `turn_neutral_v0_1` query。
这样当前常见路径只产生一个唯一 query 和一个向量，同时避免把未来 Memory、canon lore、
canon voice、Character examples 的访问语义压成无法演进的单一匿名字段。未来如确有评测证据，
可以在新的 construction version 中让不同 role 产生不同确定性 query；Coordinator 仍应把
全部唯一 query 放进同一个 Provider batch。

`SemanticQueryBudget` 是送入 Embedding Provider 前的检索输入安全边界，用来消除不同 Provider
各自截断/拒绝造成的语义差异；它不裁剪 prompt，也不是尚未实现的 Prompt Context Budget。

本任务不实现 Prompt Context Budget，不把 managed vector RAG 接入 `app/main.py`，不改变
`WorldInfoEvidenceProvider` 的关键词行为，不改变 Character 当前静态样例结果，不修改
Persistence schema，不批准 BGE-M3/SiliconFlow Profile，也不增加任何跨轮 cache。

## 1. 已核对的当前事实

### 1.1 Embedding Foundation

- `app/embeddings/` 是唯一正式 Foundation。
- `EmbeddingProviderPort` 是同步 batch-first Port，`EmbeddingRequest.purpose` 明确区分
  `query` 与 `document`。
- Provider 返回 L2-normalized、little-endian float32，并携带完整
  `EmbeddingModelSignature`。
- 普通 Factory 会重新加载仓库内 canonical Profile，只接受 `approved` 状态；目前只有
  `deterministic_hash_v1` 获批，且 Hash 仍必须显式允许。
- Provider 失败后不存在、也不得新增隐式 Hash fallback。
- `DirectQueryEmbeddingSource` 已实现单 query 解析，并在 Provider 调用前做 NFC/LF
  canonicalization。

### 1.2 Memory

- `MemoryReader` 当前拥有 `QueryEmbeddingSourcePort`，因此开启长期记忆时仍可能每轮自行调用
  Provider。
- canonical recall 已按 resolved signature 查询 exact-signature artifact，并在 coverage
  不完整时用 keyword 去重补位。
- query embedding 失败或没有有效 vector hit 时，Memory 会走 keyword fallback；显式中文
  回忆还保留 scope-visible active-memory fallback。
- Memory scope、provenance、active/status 二次校验和 `mark_memory_used` 行为不属于本任务
  的变更范围。

### 1.3 RAG 与 active provider

- `CanonicalRagRetriever.search_resolved(...)` 已能直接消费
  `ResolvedQueryEmbedding`，并校验 active ready generation 的 exact signature。
- Managed generation runtime 仍是隔离工具；`app/main.py` 没有接线。
- 主对话当前使用 `WorldInfoEvidenceProvider`，它继续以原始 `query` 做关键词检索。

### 1.4 Character 与 Prompt Context Budget

- Character v0.2 当前把静态 `style_examples.yaml` 全部格式化进
  `CharacterRuntimeContext.style_examples`，没有动态语义选择。
- 静态 Character examples 与动态 canon voice evidence 是不同资料层，不应混用。
- Prompt Context Budget 只有设计，没有正式实现；Semantic Query 不修改 `PromptContext`、
  `ContextBuilder` 的裁剪行为或 prompt section 顺序。

## 2. 范围与非目标

### 2.1 本任务负责

- 单轮中立 semantic input 与 query construction contract；
- 版本化 Semantic Query 字符/UTF-8 字节预算及超限处理；
- query role、request、resolution、status 和 safe summary schema；
- `SemanticQueryCoordinatorPort` 及默认/disabled 实现；
- 同一 Provider、同一 purpose 下的轮内规范化、去重、batch 和结果映射；
- Controller 的显式编排与共享 context 传递；
- Memory 对共享 vector 的优先消费与既有 fallback 保留；
- 给 canonical RAG 和 Character example selection 留出明确 role/Port 接点；
- CLI、FastAPI、未来 workflow 的对象生命周期；
- Trace/App Log 的安全摘要和稳定失败 reason code；
- 单元、集成和跨模块回归测试。

### 2.2 明确不负责

- Prompt Context Budget、prompt token estimator、prompt 压缩或摘要；
- LLM query rewriting、HyDE、多路召回 query 生成；
- managed vector RAG 的 active wiring、生产索引构建或 EvidenceProvider 组合切换；
- Character 动态样例排序算法或现有样例结果变化；
- Embedding Profile 审批、qualification 绕过或 Provider fallback；
- Persistence schema、query vector 表、Redis/SQLite/TTL/LRU cache；
- Memory scope/provenance、RAG generation lifecycle 或 Character rule 变更；
- 把 query、消息正文、向量、credential 或 query-derived hash 写入日志/metadata。

## 3. 建议模块结构

```text
app/semantic/
  __init__.py
  schemas.py          # turn input、role、request、resolution、context、safe summary
  ports.py            # SemanticQueryCoordinatorPort
  budget.py           # semantic-query-budget/v1 常量与 Unicode 安全裁剪
  query_builder.py    # turn_neutral_v0_1 的确定性构造与最近一组对话选择
  coordinator.py      # 收集、canonicalize、去重、batch、exact-signature 映射
  disabled.py         # 没有 active semantic consumer 时的 no-call 实现
  observability.py    # App Log 固定 allowlist 与 reason code 映射
```

为现有 Provider 补充进程内并发保护：

```text
app/embeddings/synchronized.py  # SynchronizedEmbeddingProvider，串行化共享 Provider.embed()
```

依赖方向：

```text
app/embeddings/ <- app/semantic/
                         ^
                         |
DialogueController -> TurnSemanticContext
                         |-- Memory
                         |-- future canonical RAG
                         `-- future Character example selector
```

约束：

- `app/semantic/` 不 import Memory、Retrieval、Character、DB、FastAPI 或 Prompt 模块。
- `query_builder.py` 只处理中立 turn data，不读取 character card、memory、RAG 文档或关系状态。
- Provider 由 composition root 注入，Coordinator 不读取 secret、不创建 Profile、不决定
  Hash fallback。
- `TurnSemanticContext` 不进入 `PromptContext.metadata`、`AgentTurnInput.metadata`、
  `RetrievalResult.metadata` 或持久化 job/result。

## 4. Schema 设计

以下为接口形状，字段名可在实现前做小幅命名修订，但语义不应改变。

### 4.1 Query role

```python
SemanticQueryRole = Literal[
    "memory_recall",
    "canonical_lore",
    "canonical_voice",
    "character_examples",
]
```

- `memory_recall`：长期 interaction Memory 的 vector recall。
- `canonical_lore`：未来 canon profile/lore/plot 检索。
- `canonical_voice`：未来 canon voice evidence 检索。
- `character_examples`：未来 Character 自己管理的动态样例选择。

role 表示消费者用途，不改变 `EmbeddingRequest.purpose="query"`，也不进入
`EmbeddingModelSignature`。v0.1 不给 role 添加 instruction 或领域前缀。

### 4.2 Turn input 与内部 request

```python
@dataclass(frozen=True)
class SemanticMessage:
    id: str
    role: Literal["user", "assistant"]
    content: str = field(repr=False)


@dataclass(frozen=True)
class SemanticTurnInput:
    turn_id: str
    user_id: str
    session_id: str
    character_id: str
    current_user_message_id: str
    current_input: str = field(repr=False)
    recent_messages: tuple[SemanticMessage, ...] = field(repr=False)


@dataclass(frozen=True)
class SemanticQueryBudget:
    version: Literal["semantic-query-budget/v1"] = "semantic-query-budget/v1"
    max_dependency_chars_per_message: int = 1024
    max_dependency_utf8_bytes_per_message: int = 3072
    max_query_chars: int = 4096
    max_query_utf8_bytes: int = 8000


SemanticQueryStrategy = Literal[
    "none",
    "current_only",
    "recent_user_and_current",
    "recent_assistant_and_current",
    "last_turn_pair_and_current",
]


@dataclass(frozen=True)
class SemanticQueryRequest:
    role: SemanticQueryRole
    construction_version: str
    budget_version: str
    query_strategy: SemanticQueryStrategy
    canonical_query: str = field(repr=False)
    dependency_message_ids: tuple[str, ...] = field(default=(), repr=False)
```

`SemanticMessage` 刻意只接受 `user` / `assistant`。Controller 的 turn-input adapter 负责从 Store
结果中先排除当前消息 ID，再只把这两个 role 转成 `SemanticMessage`；`system`、`tool` 和未知 role
不会进入 `SemanticTurnInput`。Query builder 对违反该 typed contract 的手工构造输入 fail fast，
不承担把任意消息类型静默过滤成业务输入的职责。这里的 role 过滤属于输入适配，不是查询扩写或
检索策略。

`SemanticQueryBudget` 是仓库内版本化常量，不从每个入口分别读取环境变量。修改任一阈值都必须
发布新的 budget version，并进入 safe summary；这样 CLI、FastAPI 与 workflow 不会形成不同的
construction contract。字符数按规范化后 Python Unicode code point 数计算，字节数按严格
UTF-8 编码计算，label、换行和截断 marker 都计入总 query 预算。

`SemanticQueryRequest` 是 Coordinator 内部的短命对象。它不作为日志、prompt metadata、
API DTO 或 persistence DTO 暴露。`repr=False` 只是最后一道防误打日志措施，不能替代显式
allowlist。

### 4.3 role state 与 turn context

```python
SemanticRoleStatus = Literal["not_requested", "skipped", "ready", "failed"]
SemanticContextStatus = Literal["not_requested", "skipped", "ready", "failed"]


@dataclass(frozen=True)
class SemanticQueryResolution:
    role: SemanticQueryRole
    status: SemanticRoleStatus
    reason_code: str
    request_key: str | None
    resolved_embedding: ResolvedQueryEmbedding | None = field(repr=False)
    error_code: str | None = None


@dataclass(frozen=True)
class TurnSemanticContext:
    turn_id: str
    status: SemanticContextStatus
    construction_version: str
    budget_version: str
    query_strategy: SemanticQueryStrategy
    budget_reason_codes: tuple[str, ...]
    provider: str | None
    profile_id: str | None
    signature_fingerprint: str | None
    requested_role_count: int
    unique_query_count: int
    dependency_message_count: int
    query_char_count: int
    query_utf8_bytes: int
    provider_called: bool
    duration_ms: float
    resolutions: tuple[SemanticQueryResolution, ...] = field(repr=False)

    @property
    def deduplicated_role_count(self) -> int:
        return self.requested_role_count - self.unique_query_count

    def resolution_for(self, role: SemanticQueryRole) -> SemanticQueryResolution:
        ...

    def resolved_for(self, role: SemanticQueryRole) -> ResolvedQueryEmbedding | None:
        ...

    def safe_summary(self) -> dict[str, object]:
        ...
```

上述统计是 immutable context 的一部分，而不是 `safe_summary()` 临时猜测：

- `query_strategy` 与 `dependency_message_count` 描述预算处理后实际进入 query 的历史块；
- `query_char_count` / `query_utf8_bytes` 只记录长度，不记录 token、文本或 text hash；
- `provider_called` 区分构造阶段跳过与真实调用；
- `duration_ms` 使用 monotonic clock，覆盖构造、锁等待、Provider 调用和结果校验；
- `deduplicated_role_count` 由已有计数派生，不重复存储。

`not_requested`/空输入时 strategy 为 `none`，长度和依赖数为 0；超长 current input 被预算拒绝时
strategy 为 `current_only`，但 `provider_called=False`，长度字段记录规范化 current-only 的安全
统计值，便于观察拒绝率。

状态语义：

| 状态 | 含义 |
|---|---|
| `not_requested` | composition root 没有为该运行时启用任何 semantic consumer/role |
| `skipped` | role 已启用，但当前输入为空、超出 query 安全预算或无法形成合法中立 query；没有调用 Provider |
| `ready` | Provider batch 完整成功，role 已映射到 exact-signature resolved vector |
| `failed` | 已尝试 Provider，但发生允许单轮降级的临时服务错误；整个逻辑 batch 无结果且不暴露部分结果 |

v0.1 一个 Coordinator 只持有一个 Provider，且每次只发一个逻辑 batch，因此 requested role
在返回 context 的调用中要么全部 ready，要么全部 failed/skipped；永久、契约或请求错误按
第 13.2 节重新抛出，不返回伪 failed context。不引入模糊的 partial vector 语义。未启用的
已知 role 返回 `not_requested`，消费者不靠 KeyError 推断状态。

`request_key` 仅是本轮内的 `q0`、`q1`，用于表达多个 role 是否共享同一 request；它不是
query hash，不跨轮稳定，也不得用于 cache key。

向量在交付给消费者前应复制为 C-contiguous little-endian float32，并设为只读，防止一个
消费者原地修改后污染其他消费者。`TurnSemanticContext` 不提供 JSON 序列化方法。

### 4.4 稳定 reason code

Coordinator 自有 reason code 建议固定为：

- `no_active_semantic_roles`
- `blank_current_input`
- `current_input_exceeds_semantic_budget`
- `dependency_truncated_to_semantic_budget`
- `dependency_dropped_for_total_semantic_budget`
- `query_ready`
- `embedding_transient_failure`
- `embedding_request_failed`
- `embedding_permanent_failure`
- `semantic_coordinator_unhealthy`
- `provider_signature_mismatch`
- `result_count_mismatch`
- `query_vector_invalid`
- `semantic_role_not_requested`

预算调整不把 role 改成 failed：只要仍能形成合法 query，role 最终为 `query_ready`，调整原因放在
`budget_reason_codes`；current-only 已超限则 role 为 skipped，reason 为
`current_input_exceeds_semantic_budget`。Provider 失败同时保留现有 `EMBEDDING_*` `error_code`，例如
`EMBEDDING_TIMEOUT`、`EMBEDDING_AUTH_FAILED`、`EMBEDDING_INVALID_RESPONSE`。reason code
描述共享层在哪一步失败，error code 描述底层稳定错误类别；不记录原始异常文本。

## 5. 稳定 Port

```python
class SemanticQueryCoordinatorPort(Protocol):
    def prepare_turn_context(
        self,
        *,
        turn_input: SemanticTurnInput,
    ) -> TurnSemanticContext:
        ...
```

active roles 在 composition root 创建 Coordinator 时配置，不由 Controller 每轮硬编码，也不
从用户可控 metadata 读取：

```python
SemanticQueryCoordinator(
    provider=SynchronizedEmbeddingProvider(embedding_provider),
    active_roles=("memory_recall",),
    query_builder=NeutralTurnQueryBuilderV01(),
    query_budget=SemanticQueryBudget(),
    app_logger=app_logger,
)
```

当前主运行时只有长期 Memory 启用时配置 `memory_recall`。未来 Manager 正式启用 managed
RAG 或 Character dynamic selector 时，再在 composition root 增加对应 role。Controller
只构造 `SemanticTurnInput`、调用 Port、记录 safe summary，并把返回对象显式传给消费者。

没有 active role 时注入 `DisabledSemanticQueryCoordinator`。它返回
`status="not_requested"`，不创建 Provider、不要求 Profile，也不做任何 query embedding。
因此当前默认 `AEMEATH_ENABLE_LONG_TERM_MEMORY=false` + World Info + static Character 路径
保持不依赖 Embedding 配置。

不建议 v0.1 再为 Memory/RAG/Character 各增加一个“先规划是否需要向量”的 preflight Port。
那会让 Controller 一轮调用每个模块两次，并扩大三组业务 Port 的变更面。推荐行为是：只要
运行时至少启用一个 semantic role，就为每个非空 turn 准备一次共享 query；各模块仍独立
决定是否检索或返回空结果。由此带来的“长期 Memory 已启用时，低信息问候也可能准备一个
随后未使用的 vector”是 Manager 暂时批准的成本取舍；第 15.3 节定义 produced/consumed
安全事件，用实际浪费率决定后续是否值得单独设计 demand preflight。

## 6. `turn_neutral_v0_1` 确定性查询构造

### 6.1 输入规范化

每段参与 query 的文本依次执行：

1. 必须为 `str`；
2. 使用 Foundation 的 `canonicalize_embedding_text()` 做 Unicode NFC 和 CRLF/CR 到 LF；
3. 去除段首、段尾空白；
4. 保留内部换行和内容，不做同义改写、关键词扩写、翻译、摘要或角色化；
5. 空白 current input 令全部 active role 为 `skipped`，不调用 Provider。

#### 6.1.1 `semantic-query-budget/v1`

预算在依赖选择后、Provider 调用前执行，默认值是：单条 dependency 最多 1024 个字符且
3072 UTF-8 bytes；含 label/marker/换行的最终 query 最多 4096 个字符且 8000 UTF-8 bytes。
这些阈值是 Semantic Query 的检索安全边界，不影响完整 current input 被保存、交给
`ContextBuilder` 或发送给 ChatModel 的行为。

确定性处理顺序：

1. 先单独规范化 current input，并计算 current-only 的字符数和 UTF-8 bytes。
2. 如果 current-only 已超过任一总上限，不截断、不要只嵌入它的一部分，也不调用 Provider；
   active role 返回 `skipped/current_input_exceeds_semantic_budget`。Memory 仍用完整原始 query
   做 keyword/explicit recall fallback，World Info 和 prompt 路径保持完整输入。
3. 对每个 dependency 独立应用单条上限。超限时保留头尾，并在中间插入固定 ASCII marker
   `\n[semantic_context_truncated]\n`；不在 UTF-8 code point 中间切字节。字符配额扣除 marker
   后头部取得奇数余量，尾部取得偶数余量；若仍超过 byte 上限，则每次从当前保留 UTF-8 bytes
   较多的一侧移除一个 code point，同值时先移除尾部，直到两个上限都满足。记录
   `dependency_truncated_to_semantic_budget`，不记录被裁内容或长度差值。
4. 使用第 6.5 节格式拼接并重新计算总长度。若总 query 仍超限，按时间从旧到新丢弃完整
   dependency block：正常上一组对话先丢 `recent_user`，再丢 `recent_assistant`；每次重建 label
   和换行后重新计数，并记录 `dependency_dropped_for_total_semantic_budget`。current input 永不在
   这一步截断。
5. 若移除所有 dependency 后 current-only 合法，就退回 `current_only` 并调用 Provider；若仍
   不合法则按第 2 条 skipped。context 中的 `query_strategy` 与
   `dependency_message_count` 始终反映预算处理后的实际 query，而不是候选历史。

8000 bytes 给本地 BGE-M3 的 8192-token 上限预留了 special-token 余量，但 char/byte 预算本身
不能替代 tokenizer 检查。为彻底禁止 Provider 语义漂移，实施时还需由 Embedding Foundation
复核并把 `LocalBgeM3Provider` 的 query 路径改为“先无截断编码并检查长度，超限抛
`EMBEDDING_INVALID_INPUT`，再以 `truncation=False` 推理”；不得继续静默截断。SiliconFlow
只会收到已通过同一预算的全文。该 Foundation guard 不审批 Profile、不改变 signature，也不
允许 Hash fallback。

### 6.2 当前消息排除规则

Controller 继续保持现有顺序：先保存当前 user message，再调用
`get_recent_messages(limit=settings.recent_message_limit)`，然后按
`message.id != current_user_message_id` 过滤。过滤后的同一列表同时交给 Semantic Query 和
`ContextBuilder`。

Coordinator 再做一次防御性过滤：任何 `id == current_user_message_id` 的条目都不得成为
dependency context。不能按正文相等过滤，因为用户可能合法地重复以前说过的话。不能依赖
“当前消息一定是列表最后一条”，因为不同 Store 或同时间戳排序不应改变安全规则。

### 6.3 上下文依赖判定

默认只嵌入 current input。只有当前输入命中版本化、可测试的显式依赖规则时，才加入最近
一组对话，避免每轮把历史无条件混进 Memory/RAG query。

建议 `context_dependency_rules_v0_1` 包含：

- 开头型承接词/指代：`那`、`那么`、`然后`、`所以`、`继续`、`还有`、`再说`、`这个`、
  `那个`、`它`、`他`、`她`、`他们`、`她们`、`这些`、`那些`；
- 明确上文引用：`刚才`、`前面`、`你说的`、`我说的`、`我们聊的`、`上一个`；
- 独立短追问：`为什么`、`怎么会`、`怎么办`、`后来呢`、`然后呢`、`那呢`、`还有呢`；
- 英文等价的行首/单独模式：`then`、`so`、`continue`、`what about`、`why`，以及明确的
  `you said` / `we discussed`。

规则必须以常量和 regex 测试固化；不能从 `AgentTurnInput.metadata` 接受任意 query rewrite
指令，也不能调用 LLM 判断依赖。

### 6.4 最近一组对话选择

当判定为依赖上文时：

1. 只考虑已过滤且正文非空的 `user` / `assistant` recent messages；
2. 保持 Store 返回的时间顺序，不按时间戳、消息 ID 或其他可能不稳定的字段重新排序；
3. 正常情况下，历史尾部是上一轮 `user -> assistant`。选择最近一条 assistant，再选择它
   前面最近的一条 user，组成最近一个完整对话组；
4. 两条消息先保留规范化后的完整正文，不做切句、摘要或关键词抽取；只有第 6.1.1 节的
   `SemanticQueryBudget` 可以按公开的头尾规则裁剪或删除 dependency；
5. 如果历史尾部是一条尚未得到 assistant 回复的 user 消息，例如上一轮在模型失败后只保存了
   user，则只使用这条最近 user。不能跳过它去选择更早的完整对话组，因为当前输入最可能承接
   的正是这条未完成消息；
6. 如果只有 assistant 而找不到更早的 user，则使用该 assistant；
7. 完全没有可用历史时只使用 current input。

因此预算处理前的正常候选最多是最近一组两条完整消息，异常未完成路径是一条。这个固定窗口是
semantic query construction contract，不是 Prompt Context Budget；它不删除历史、不改变
prompt 中的 recent conversation，也不把选中的消息保存到新的缓存或持久化结构。

选择完整对话组而不是每条消息的最后一句，原因是当前对话长度较短且现已有显式安全预算，
同时可以避免长回答的最后一句只是“我可以继续说明”之类的收尾语，导致主语、主题、原因或转折
被切掉。这里的裁剪只来自可审计的 budget version，不存在 Provider 自行决定的隐藏截断。

### 6.5 固定拼接格式

无 dependency 时：

```text
<normalized current input>
```

有 dependency 时使用固定、中立的 role label：

```text
[recent_user]
<normalized and budgeted recent user message>
[recent_assistant]
<normalized and budgeted recent assistant message>
[current_user]
<normalized current input>
```

未完成上一轮或只有单条历史时，只生成实际存在的一个 recent block。实际 label 顺序按所选
消息角色和时间顺序生成。不得写角色名、memory namespace、canon namespace、检索 intent 或
prompt instruction。所有 active role 在 v0.1 都得到完全相同的 canonical query。

## 7. 一个共享 query 与多个具名 role 的取舍

推荐“schema 支持多个 role，v0.1 默认一个中立 query”：

优点：

- Memory、canon lore、canon voice、Character examples 可以明确声明自己消费的用途；
- 当前同一文本只计算一次，能直接消除同轮重复 Provider 调用；
- RAG 可对同一 resolved vector 分别应用 namespace、top-k、intent weighting，检索策略仍在
  Retrieval 内；
- 未来确需不同 query 时可以按 role 升级 construction version，而无需重写 Controller 或
  Embedding Foundation。

不建议 v0.1 直接给每个 role 加不同前缀：

- 这相当于未经评测的 query rewriting；
- 容易把 Memory/RAG/Character 策略塞入共享层；
- 会从一个向量变成多个向量，增加成本和失败面；
- BGE Profile 当前明确没有 query instruction，不能用 role 前缀变相绕过该契约。

## 8. Batch-first、去重与 exact signature

Coordinator 算法：

1. 先按第 6 节构造并执行 `SemanticQueryBudget`；只有合法、有界的 canonical query 才按固定
   role 顺序创建 `SemanticQueryRequest`，同一 role 重复配置视为配置错误；
2. 以 `(provider.signature.canonical(), "query", canonical_query)` 做当前调用内的精确去重；
   实现可以使用内存字典，但不得计算或记录 query hash；
3. 保留首次出现顺序，构造一个
   `EmbeddingRequest(texts=tuple(unique_queries), purpose="query")`；
4. 只调用一次 `provider.embed(...)`；Provider 内部可按现有 runtime `batch_size` 拆 HTTP/模型
   chunk，但这仍是一个逻辑 batch；
5. 强制 `result.signature.canonical() == provider.signature.canonical()`；
6. 强制 `result.vectors.shape[0] == unique_query_count`，并通过 Foundation artifact query
   validation 逐行验证维度、finite、L2 norm 和 little-endian float32；
7. 按 batch index 创建只读 `ResolvedQueryEmbedding`，再按去重映射回每个 role；
8. 任一步失败都丢弃整个 batch，不把已经解析的行作为 partial result 返回。

同维度不同 signature 永远不能共享或评分。Coordinator 不检查某个 Memory/RAG index 是否
拥有该 signature；消费者必须继续用自己的 exact-signature store/ready-generation 契约做
资格判断。

去重表、request、向量和映射全部是局部变量。不得写 SQLite、Redis、文件、TTL cache、LRU、
进程全局 `current_context` 或 `ContextVar`。

## 9. DialogueController 调用时序

建议时序：

```text
load character profile / validate Session
  -> build current static CharacterRuntimeContext（现有行为）
  -> save current user message
  -> get recent messages
  -> filter current_user_message_id
  -> load relationship state
  -> SemanticQueryCoordinator.prepare_turn_context(...)
  -> trace semantic_context_prepared（safe summary only）
  -> MemoryReader.retrieve_query(..., semantic_context=...)
  -> EvidenceProvider.build_evidence_pack(..., semantic_context=...)
       WorldInfo: ignore semantic_context, keep raw query behavior
       future canonical RAG: consume resolved role
  -> future CharacterExampleSelectorPort（本任务不启用）
  -> ContextBuilder / PromptCompiler / Model / State / Memory write
```

Controller 只做以下动作：

- 把已经存在的 turn identity、当前 input 和过滤后的 recent messages 转成
  `SemanticTurnInput`；适配时只接收 `user` / `assistant`，丢弃 system/tool/未知 role；
- 调用一个 coordinator Port；
- 记录 `safe_summary()`；
- 将同一个 `TurnSemanticContext` 引用传给 Memory/Evidence。

Controller 不检测指代、不切句、不添加 query 前缀、不决定检索 intent/top-k/threshold，也不
访问 Embedding Provider。

## 10. Memory 接入与 fallback

建议向后兼容地扩展：

```python
class MemoryReaderPort(Protocol):
    def retrieve_query(
        self,
        *,
        memory_query: MemoryQuery,
        semantic_context: TurnSemanticContext | None = None,
    ) -> list[MemorySearchResult]:
        ...
```

`MemoryReader` 决策顺序：

1. 继续先执行现有 `_should_read(...)`；低信息输入仍可返回 `[]`，共享层不替 Memory 决定
   是否召回。
2. `semantic_context` 非空且 `memory_recall` 为 `ready`：使用共享
   `ResolvedQueryEmbedding`，不得再次调用 `DirectQueryEmbeddingSource`。
3. 对共享向量继续执行 `prepare_embedding_query(...)`，并只查询 exact signature artifact。
4. coverage 完整/不完整、vector-first、keyword supplement、hydration 后 active/status/filter
   二次验证和 `mark_memory_used` 完全保留。
5. `semantic_context` 已提供但 role 为 `failed`、`skipped` 或 `not_requested`：直接走当前
   keyword fallback；显式回忆继续走 active-memory fallback。不得在同一 turn 内再尝试一次
   Direct Provider 调用。
6. `semantic_context is None`：保留现有 Direct source 路径一个兼容周期，供独立 Memory
   测试、管理脚本或尚未迁移的调用者使用。

这保证“共享结果优先”不是 best-effort 提示，而是 Controller 主路径的唯一 query vector
来源；同时不把 keyword fallback、显式回忆、scope 或 score 合并策略移出 Memory。

这里的 `failed` 只可能是第 13.2 节列出的 timeout/rate-limit/unavailable；永久、契约或请求
错误不会返回 context，Controller 会进入现有 turn failure。预算 skipped 时 Memory 使用完整
`MemoryQuery.text` 做关键词/显式回忆判断，不使用被裁 dependency，也不自行重试 embedding。

`DisabledMemoryReader` 接受同一可选参数并忽略，返回 `[]`。

## 11. Canonical RAG 与 World Info

### 11.1 Canonical RAG

`CanonicalRagRetriever.search_resolved(...)` 已满足核心需求：它不调用 Provider，并按
resolved signature 查找 exact ready generation。T-0135 不需要改变 generation lifecycle 或
Persistence。

建议给 `EvidenceProviderPort.build_evidence_pack(...)` 增加可选
`semantic_context: TurnSemanticContext | None = None`。未来 `WuwaRagEvidenceProvider`：

- lore 路径读取 `canonical_lore`；
- voice 路径读取 `canonical_voice`；
- v0.1 两个 role 通常指向同一个 resolved vector；
- 对同一 vector 可执行两个不同 namespace search，但不能再次调用 Provider；
- role 不 ready、signature 不匹配、ready generation 不可用或 index diagnostics 非空时，按
  Retrieval 自己的 availability/fallback 策略返回空 evidence 或独立关键词结果。

本任务只验证 resolved path 可以消费共享对象，不在 `app/main.py` 构造
`CanonicalRagRetriever` 或 `WuwaRagEvidenceProvider`，不修改 active generation mapping。

### 11.2 WorldInfoEvidenceProvider

`WorldInfoEvidenceProvider` 只为兼容新可选参数做显式 ignore：

- 仍把原始 `turn_input.message` 作为 `query` 传给 `WorldInfoKeywordRetriever`；
- `uid 160`、`uid 86` 和 constant policy 规则保持不变；
- 不读取 query vector、不读取 role status、不创建 Embedding Provider；
- 同一输入在 Noop/ready/failed semantic context 下必须返回相同 `EvidencePack`。

因此“共享层已实现”不等于“生产 vector RAG 已接入”。

## 12. Character 预留边界

本任务在 `SemanticQueryRole` 中保留 `character_examples`，但 `app/main.py` 当前不激活该
role，且不修改 `CharacterProvider.build_runtime_context(...)` 的现有样例选择结果。

推荐后续由 Character Worker 设计独立 `CharacterExampleSelectorPort`，在已有静态
`CharacterRuntimeContext` 上消费 `character_examples` resolved vector，并返回新的 context
或 selected example IDs。正式接入前必须满足：

- selector 未启用或 semantic role 不 ready 时，返回与当前完全相同、顺序一致的
  `style_examples`；
- 不把 `style_examples.yaml` 当成 canon voice RAG；
- 不改变 persona、speech、boundary、forbidden claim 或 context tag 规则；
- query vector 和 query 文本不进入 Character manifest、metadata 或 prompt log；
- 动态选择策略和质量评测由 Character Worker 单独提交并经 Manager 审批。

本轮不建议为一个尚未启用的 selector 修改 `CharacterProviderPort`。共享 context 的
`character_examples` role 就是预留接点，避免先制造一个没有消费者的行为变化。

## 13. 失败传播策略

### 13.1 初始化失败与 lazy 首次调用

Provider/Profile/secret 能在 composition-time 发现的错误必须 fail fast；但现有本地 Provider
在第一次 `embed()` 才 lazy-load tokenizer/model，SiliconFlow 也在第一次请求时才创建 client
并发现认证错误。因此“出现在 turn 内”不等于“临时错误”，传播必须按稳定 error code 分类，
不能按发生阶段分类。

- 如果至少一个 active semantic role 需要 Provider，`build_controller()` /
  `build_agent_service()` 对可提前发现的 Profile、配置和 secret 错误 fail fast；
- CLI、FastAPI 或 workflow 通过现有 `map_exception(...)` 暴露稳定公共错误；
- lazy 首次调用发现的永久错误将 Coordinator 原子地标记为 `unhealthy`，当前调用重新抛出，
  后续 turn 在调用 Provider 前以 `semantic_coordinator_unhealthy` fail fast；
- unhealthy 是该 runtime/进程内对象状态，不持久化、不设 TTL，只有修复配置并重建 runtime
  才恢复；不得切 Disabled、不得换 Hash；
- 没有 active role 时不创建 Provider，使用 Disabled coordinator。这是无消费者模式，不是
  失败 fallback。

### 13.2 错误分类矩阵

| 类别 | Foundation error code / 共享层校验 | 当前 turn | 后续 turn | 允许消费者降级 |
|---|---|---|---|---|
| 永久配置/环境 | `EMBEDDING_CONFIG_INVALID`、`EMBEDDING_SECRET_MISSING`、`EMBEDDING_DEPENDENCY_MISSING`、`EMBEDDING_AUTH_FAILED`、`EMBEDDING_QUALIFICATION_FAILED` | 标记 unhealthy，记录 `embedding_permanent_failure` 后重新抛出 | `semantic_coordinator_unhealthy` fail fast | 否 |
| Provider/向量契约损坏 | `EMBEDDING_INVALID_RESPONSE`、`EMBEDDING_SIGNATURE_MISMATCH`、`EMBEDDING_VECTOR_CORRUPT`，以及共享层发现的 signature/count/dimension/NaN/Inf/norm 错误 | 标记 unhealthy，记录对应校验 reason 后重新抛出 | fail fast | 否 |
| 请求本身不可接受 | `INVALID_INPUT`、`EMBEDDING_INVALID_INPUT`、`EMBEDDING_REQUEST_REJECTED` | 记录 `embedding_request_failed` 后重新抛出；不标记整个 Provider unhealthy | 可继续处理新请求 | 否 |
| 临时服务错误 | `EMBEDDING_TIMEOUT`、`EMBEDDING_RATE_LIMITED`、`EMBEDDING_UNAVAILABLE` | 返回 `status="failed"`，所有 requested role 为 `embedding_transient_failure` | 下一 turn 可重试 | 是，仅本轮 |
| 未知编程/schema 错误 | 不属于已列明稳定错误的异常 | 原样重新抛出 | 不自动改健康状态 | 否 |

只有“临时服务错误”返回 failed context 并允许 Memory keyword/explicit recall、未来 RAG
unavailable/empty evidence、World Info 和静态 Character 继续完成该轮。预算主动跳过不是错误，
也按第 6.1.1 节走 consumer fallback。永久、契约和请求错误不得长期静默为 keyword fallback；
否则部署会在依赖缺失、密钥错误或认证失败时伪装成健康。

Coordinator 只捕获明确列出的 Foundation 异常；不使用 `except Exception` 做降级。错误日志只
记录类别、固定 reason code、稳定 `error_code`、阶段及已安全解析的 provider/profile，不记录
原始异常文本、路径、响应正文或 credential。

### 13.3 Provider 并发与健康状态同步

现有 `EmbeddingProviderPort` 没有声明线程安全，而本地 Provider 与 SiliconFlow Provider 都有
可变 lazy 状态。v0.1 不假设它们可并发调用，采用 correctness-first 的进程内串行保护：

1. composition root 把 raw Provider 只包装一次为 `SynchronizedEmbeddingProvider`；同一
   runtime 的 Coordinator 与 Memory document extraction 都注入该 wrapper，不能一边走 wrapper、
   一边绕过它调用 raw Provider。
2. wrapper 用一个 `threading.RLock` 包住完整 `embed()`，包括 lazy load/client 创建、重试、
   推理/HTTP 和结果返回；`signature` 仍是初始化后不可变的只读属性。
3. Coordinator 另用一个 state lock 包住“检查 unhealthy -> 调用 wrapper -> 分类错误并更新
   unhealthy”的完整 query 临界区。锁顺序固定为 Coordinator state lock 再 Provider wrapper
   lock；其他 document consumer 只取得 wrapper lock，避免死锁。
4. 所有 request/query/vector 仍是方法局部变量，锁和 unhealthy flag 不保存 turn 数据；串行化
   不是 batching、cache 或跨轮状态复用。

每个 Uvicorn worker 有独立 wrapper/lock。此方案牺牲同进程 Provider 并行吞吐，待 Embedding
Foundation 另行定义并验证线程安全 capability 后才可放宽，不能仅凭实现看似可重入就移除锁。

### 13.4 日志失败边界

Semantic Coordinator 自己的 App Log 通过 best-effort allowlist adapter 写入，失败不改变
Coordinator 结果。Foundation 现有 `ObservableEmbeddingProvider` 仍只记录一个逻辑 Provider
batch 的单终态事件；consumer 不伪造 Provider 调用事件。

本设计删除“Semantic Turn Trace 写入失败不影响对话”的承诺：当前
`DialogueController._record_trace()` 直接调用 `TraceLoggerPort.record()`，异常会传播。T-0135
不只为 semantic event 局部吞错，以免同一 Controller 中不同 Trace 事件语义不一致。登记独立
Observability follow-up：由 Manager 决定是否引入覆盖全部 trace event 的全局
`SafeTraceLogger` adapter；在该任务完成前，semantic trace 沿用现有传播行为。

## 14. CLI、FastAPI 与未来 workflow 生命周期

### 14.1 CLI

`run_cli()` 通过 `build_agent_service()` 创建一次 runtime：

- 一个进程内创建一个 raw Provider、一个共享的 `SynchronizedEmbeddingProvider` wrapper 和一个
  Coordinator；需要 document embedding 的同一 runtime 也只拿 wrapper；
- 每次输入创建一个新的 `TurnSemanticContext`；
- context 只由当前 `handle_user_message()` 持有，返回后释放；
- `/memory extract` 的 document embedding 可复用同一进程 Provider，但不复用 query
  context。

### 14.2 FastAPI

`create_app()` 每个 Uvicorn worker 进程创建自己的 Provider/Coordinator：

- 不按 HTTP request 创建 Provider/client 或加载本地模型；raw Provider 在 worker 内只包装一次；
- 不跨 worker 共享 Python 对象；
- 每个请求拥有独立 `TurnSemanticContext`，不得保存在 `app.state`、Controller 字段或模块
  全局变量中；
- Coordinator 只保存 active roles、budget/config、锁和 runtime unhealthy flag，不保存任何
  turn request、query 或 vector；
- 两个并发 request 的 query 调用按第 13.3 节串行进入共享 Provider，返回的 context/vector
  仍分别在各自调用栈内，不能串用 request key、长度、reason code 或向量；
- 不在本任务引入 Redis、全局 cache 或隐式后台 batching。

### 14.3 Memory Worker 与 RAG 工具

- Memory Worker 继续创建 worker-local Provider，只处理 `document` artifacts/repair，不创建
  `TurnSemanticContext`。
- Managed RAG build/query CLI 继续是独立进程；build 使用 `document`，独立 query 工具可
  继续使用 Direct source。它不是 dialogue turn 内重复调用问题的组成部分。

### 14.4 未来 workflow / LangGraph

将 `TurnSemanticContext` 作为一次 graph execution 的显式、内存态字段，由一个 semantic
node 生成、后续 Memory/RAG/Character node 只读消费。它必须从 checkpoint serializer 中
排除；若 workflow resume 发生在新的执行中，应按输入重新计算，而不是从 checkpoint、DB 或
TTL cache 恢复 vector。可以持久化的只有已有 trace safe summary，不能持久化 query 或
resolved vector。

workflow runtime 与 CLI/FastAPI 一样：每个进程/runtime 只组装一个同步 wrapper 与 Coordinator；
graph execution 只持有自己的 context。若 workflow 框架在同一进程并发执行多个 graph，仍使用
同一进程内锁，不假设 Provider 并发安全。

## 15. Trace / App Log 与脱敏边界

### 15.1 建议事件

App Log：

- `semantic.query_completed`
- `semantic.query_skipped`
- `semantic.query_failed`
- `memory.semantic_query_usage`（Memory Worker 复核后，用于统计 ready vector 是否实际消费）

Turn Trace：

- `semantic_context_prepared`

### 15.2 固定安全字段

允许字段：

- `turn_id`（现有非正文 turn correlation ID）
- `construction_version`
- `budget_version`
- `context_status`
- `query_strategy`
- `budget_reason_codes`（仅固定枚举值）
- role 名称和每个 role 的 status/reason code
- `requested_role_count`
- `unique_query_count`
- `deduplicated_role_count`
- `dependency_message_count`（只记录数量，正常为 0 或 2，未完成上一轮时可为 1）
- `query_char_count`
- `query_utf8_bytes`
- `provider_called`
- `provider`
- `profile_id`
- `signature_fingerprint`
- `duration_ms`
- `error_code`
- consumer usage 的 `semantic_role`、`semantic_vector_consumed` 和固定 usage reason code

禁止字段：

- current input、canonical query、dependency context 或任意消息正文；
- query-derived text hash、token 列表、关键词或句子片段；
- vector、维度坐标、norm 或 Provider 原始响应；
- API key、Authorization、cookie、token、secret、headers 或 credential；
- 任意未经 allowlist 的 `AgentTurnInput.metadata`；
- `TurnSemanticContext`、`SemanticQueryRequest` 或 `ResolvedQueryEmbedding` 的直接 `repr`。

事件继续经过 `redact_sensitive(...)`，但安全性首先来自 DTO 和字段 allowlist。Semantic
summary 不进入 Prompt Run Log；Prompt Run 只继续记录最终 prompt context，不应包含 semantic
query/vector。

### 15.3 实际浪费率观察

Coordinator 只能知道“生成了什么”，不能知道消费者后来是否使用，因此不能在
`safe_summary()` 中伪造 consumed 状态。v0.1 用同一 `turn_id` 关联两个安全 App Log：

1. `semantic.query_completed` 中 `provider_called=true` 且 role ready，表示该 role 产生了向量；
2. Memory 在 `_should_read(...)` 之后写一次 `memory.semantic_query_usage`：若 ready resolution
   实际进入 vector search，则 `semantic_vector_consumed=true/reason=vector_search_attempted`；若
   因低信息输入提前返回，则为 `false/reason=low_information_input`。零 vector hit 仍算已消费，
   因为 Provider 成本和 vector query 已实际发生；
3. 浪费率按观察窗口内“ready 且 consumed=false 的 role-turn / ready role-turn”计算。failed、
   skipped、not_requested 不进入分母；缺少 consumer usage 事件单列为 telemetry incomplete，不能
   直接当作浪费；
4. 未来 RAG/Character consumer 启用时各自发同形状 usage event，共享层不 import 或调用它们。

该 usage event 只增加 `turn_id`、role、bool 和固定 reason code；不记录 query、消息正文、命中
内容、vector、query hash 或 consumer 的自由文本。它需要修改 Memory observability allowlist，
由 Memory Worker 复核；不改变 `_should_read`、召回结果或 scope/provenance。

## 16. 向后兼容与现有 Port 变化

### 16.1 新 Port

- 新增 `app.semantic.ports.SemanticQueryCoordinatorPort`。
- `DialogueController` 新增可选 coordinator 依赖，默认
  `DisabledSemanticQueryCoordinator`，保证现有直接构造测试和未迁移 runtime 可运行。

### 16.2 MemoryReaderPort

`retrieve_query(...)` 增加可选 `semantic_context=None`。旧调用不传参数时继续使用当前 Direct
source；Controller 主路径传入 context 后禁止第二次 query Provider 调用。`retrieve(...)`
兼容 helper 暂不增加 semantic 参数，继续走旧 direct path，并标记为非共享调用入口。

### 16.3 EvidenceProviderPort

`build_evidence_pack(...)` 增加可选 `semantic_context=None`。所有 in-tree provider 和测试
fake 接受该参数：

- Empty/Composite/WorldInfo 默认忽略或向支持 resolved query 的内部 retriever 传递；
- `WorldInfoEvidenceProvider` 行为必须完全不变；
- 旧的独立调用不传参数仍可运行。

如 Manager 不希望在 active RAG 接线前扩大 `EvidenceProviderPort`，可在第一实施阶段只把
context 传给 Memory，并把该可选参数延后到 RAG Worker；但这样 T-0135 的主链路预留会不够
完整。本设计推荐现在增加可选参数并用回归测试锁死 World Info 行为。

### 16.4 CharacterProviderPort

本任务不修改。未来新增 `CharacterExampleSelectorPort`，而不是把 vector 塞进当前
`metadata` 参数。这样静态 Character provider、manifest loader 和样例顺序零变化。

### 16.5 Embedding/Persistence 兼容

- 不修改 `EmbeddingProviderPort`、Profile、signature 或 Factory 门禁。
- `SynchronizedEmbeddingProvider` 实现同一个现有 Port，只增加进程内串行代理；composition root
  持有 raw Provider，其他模块只拿 wrapper。
- 本地 BGE 的 query fail-loud token guard 不改变 canonicalization、模型、维度、signature 或
  document artifact；若 Foundation Worker 不批准直接改 Provider，则 T-0135 实施必须停下，
  不能以继续静默截断作为兼容方案。
- `DirectQueryEmbeddingSource` 保留给非共享兼容调用；主 Controller 不再由 Memory 独立调用。
- 不修改 legacy Memory/RAG artifact API 或任何数据库 schema。

## 17. 预计实现文件

本轮实际只新增本设计文档。Manager 批准后的预计代码范围：

新增：

- `app/semantic/__init__.py`
- `app/semantic/schemas.py`
- `app/semantic/ports.py`
- `app/semantic/budget.py`
- `app/semantic/query_builder.py`
- `app/semantic/coordinator.py`
- `app/semantic/disabled.py`
- `app/semantic/observability.py`
- `app/embeddings/synchronized.py`
- `tests/test_semantic_query.py`
- `tests/test_semantic_query_integration.py`
- `tests/embeddings/test_synchronized.py`

修改：

- `app/agent/dialogue_controller.py`
- `app/agent/ports.py`
- `app/main.py`
- `app/memory/reader.py`
- `app/memory/disabled.py`
- `app/memory/observability.py`
- `app/embeddings/providers/local_bge_m3.py`（仅 query 路径的 fail-loud token guard；Foundation 复核）
- `tests/embeddings/test_local_bge.py`
- `tests/embeddings/test_siliconflow.py`
- `app/retrieval/ports.py`
- `app/retrieval/provider.py`
- 相关 Controller/Memory/World Info/Service/API 测试 fake 与回归测试

预期不修改：

- `app/db/*`、schema、migration、repository；
- `app/retrieval/generation.py`、`index_manifest.py`、active mapping；
- `app/character/*` 的运行时与资料；
- `app/agent/context_builder.py`、`prompt_context.py`；
- `app/embeddings/ports.py`、Profile、signature、Factory 门禁及 SiliconFlow payload 语义；
- Prompt Context Budget 设计或实现。

Manager-owned `docs/architecture.md`、`docs/interfaces.md`、`docs/task_log.md` 应在实现验收后由
Manager 更新；Worker 不在审批前修改。

## 18. 测试计划

### 18.1 Query builder 单元测试

1. NFC、CRLF/CR 到 LF、段首尾空白规范化稳定。
2. independent input 只使用 current input。
3. 每个 v0.1 dependency marker 命中/不命中的边界。
4. recent messages 按给定顺序处理，正常选择最近一个完整 `user -> assistant` 对话组，并保留
   两条消息作为预算前候选。
5. 历史尾部只有未回复 user 时选择该 user，不跳到更早的完整对话组；只有 assistant 时使用
   该 assistant。
6. 当前消息按 ID 排除；正文相同但 ID 不同的历史消息不能误删。
7. blank recent message 不进入 query；Controller adapter 对 system/tool/未知 role 的过滤单独测试，
   builder 对违反 `SemanticMessage` typed contract 的输入 fail fast。
8. 没有历史的依赖型输入仍产生 current-only query。
9. dependency 在 char/byte 上限边界原样保留，任一上限加一时按固定头尾/marker 算法裁剪；中文、
   emoji、组合字符均不产生无效 UTF-8 或破坏 NFC。
10. 最终 query 在 char/byte 上限边界可调用；超限时严格按 recent_user -> recent_assistant 顺序
    整块删除并重新计算 label/newline，strategy 与 dependency count 反映最终结果。
11. current-only 分别超过 char 上限、UTF-8 byte 上限或两者时均 skipped，current input 不被
    截断/修改，Provider 调用数为 0，reason 为 `current_input_exceeds_semantic_budget`。
12. 相同输入与 budget version 重复构造得到 byte-for-byte 相同 query、strategy、长度和 reason。
13. role 不改变 v0.1 query 文本；construction version 与 budget version 固定。
14. query/request/context 的 `repr` 不包含正文或 vector。

### 18.2 Coordinator 单元测试

1. active roles 固定排序，重复 role 配置 fail fast。
2. 四个 role 同一 canonical query 时只发一个 `purpose="query"` batch row。
3. 注入 future test builder 产生多个唯一 query 时，仍只调用一次 Provider，batch row 顺序和
   role 映射正确。
4. 空 active role/空白 current input 不调用 Provider。
5. result signature 与 Provider signature 必须 canonical exact match。
6. 少行、多行、错维、NaN/Inf、非 normalized vector 全批丢弃、标记 unhealthy 并重新抛出，
   无 partial mapping；后续调用 fail fast 且 Provider 调用数不增加。
7. 共享 vector 为只读；一个消费者不能影响另一个 role。
8. timeout/rate-limit/unavailable 各返回稳定 failed context，允许本轮 consumer fallback，下一轮
   重新调用 Provider。
9. config/secret/dependency/auth/qualification 在 lazy 首次调用出现时均标记 unhealthy 并重新
   抛出；第二轮得到 `semantic_coordinator_unhealthy` 且不再触碰 Provider。
10. invalid input/request rejected 重新抛出但不标记 Provider unhealthy；下一条合法请求可成功。
11. 未知编程异常继续抛出，不被误当作可降级 Provider failure。
12. 连续两轮相同 query 仍各调用一次 Provider，证明没有跨轮 cache。
13. 两个并发 turn 在首次 lazy load 时只初始化一次；Provider 最大并发 embed 数为 1，两个
    context 的 request key、统计、reason 和向量分别对应各自输入，不串数据。
14. Coordinator query 与 Memory document embedding 并发时也经同一 wrapper 串行，不绕过锁。
15. `safe_summary()` 的 duration、dependency count、strategy、budget version、char/byte count、
    provider_called 可由 context 字段直接得到；deduplicated count 派生正确。
16. safe summary/App Log 只包含 allowlist，敏感哨兵、正文、hash、token 和 vector 均不出现。
17. 本地 BGE query guard 对 tokenizer 实际长度超限 fail loud，不以 `truncation=True` 继续；
    合法 query 以无截断全文推理。SiliconFlow fake client 收到与 builder 输出 byte-for-byte 相同的
    有界全文。

### 18.3 Memory 集成测试

1. shared `memory_recall` ready 时 Memory 使用该 vector，Direct source 调用次数为 0。
2. transient failed、budget skipped、not_requested 时走 keyword fallback，Direct source 调用
   次数仍为 0；永久/契约/请求错误没有 context，不进入静默 Memory fallback。
3. `semantic_context=None` 的旧直接调用仍可使用 Direct source。
4. exact-signature recall、coverage incomplete keyword supplement、score 不混排保持不变。
5. 显式中文回忆的 active-memory fallback 保持不变。
6. hydrate 后 archive/disable race 过滤和 `mark_memory_used` 规则不回归。
7. 低信息输入仍由 Memory 返回 `[]`；若已有 ready vector，写安全
   `memory.semantic_query_usage(consumed=false, low_information_input)`；实际进入 vector query
   时写 consumed=true，零命中仍计为 consumed。
8. usage App Log 可按 turn_id 与 semantic completed 关联，且不包含 query、正文、vector、命中
   内容或自由文本 reason。

### 18.4 RAG / World Info 回归

1. `CanonicalRagRetriever.search_resolved(...)` 使用共享 vector 时 Provider 调用次数为 0。
2. lore/voice 两次 namespace search 可复用同一 resolved vector。
3. signature mismatch、无 ready generation、index diagnostics 保持明确 unavailable/empty 结果。
4. `app/main.py` 仍构造 `WorldInfoEvidenceProvider`，没有 vector RAG active wiring。
5. World Info 对同一 query 在 semantic context 为 None/ready/failed 时返回完全相同结果，
   包括 constant policy 与顺序。

### 18.5 Character 回归

1. Character v0.1/v0.2 profile/runtime context 测试全部通过。
2. `style_examples` 内容、数量和顺序与当前基线完全一致。
3. `character_examples` role 未激活且没有 selector 调用。
4. Semantic query/vector 不进入 Character metadata、manifest 或 prompt section metadata。

### 18.6 Controller、CLI、FastAPI 与生命周期

1. Controller 对一轮只调用一次 Coordinator，并把同一个 context 对象传给 Memory/Evidence。
2. recent list 在 Semantic Query 和 Prompt Context 中都不含当前 user message ID。
3. Controller adapter 只把 user/assistant 转成 `SemanticMessage`，system/tool/未知 role 不进入
   builder；正文相同但 ID 不同的 user/assistant 历史仍保留。
4. 默认长期 Memory 关闭路径不创建 Provider，CLI/API 行为保持不变。
5. 长期 Memory 开启路径每个 runtime 只创建一个 raw Provider、一个 synchronized wrapper 和
   一个 Coordinator；不同 runtime 实例互不
   共享。
6. 可提前发现的永久错误使 CLI/FastAPI composition fail fast；lazy 永久错误使当前 turn 失败
   且 runtime unhealthy，无 Hash/Disabled/keyword 静默降级。
7. 单轮 timeout/rate-limit/unavailable 时 turn 仍可通过 Memory keyword + World Info + static
   Character 完成；请求错误和永久错误不使用该降级路径。
8. 两个并发 API turn 首次加载和稳定调用均串行 Provider，结果不串 turn；不同 worker 进程各有
   自己的 wrapper/Coordinator。
9. Semantic App Log adapter 失败不影响结果；Trace logger 失败按当前 Controller 行为传播，
   测试不得声称 T-0135 已提供全局 safe trace。
10. context 不保存在 Controller、app state、prompt log 或下一轮输入中。

### 18.7 回归命令

```text
pytest -q tests/test_semantic_query.py tests/test_semantic_query_integration.py
pytest -q tests/embeddings/test_synchronized.py tests/embeddings/test_local_bge.py tests/embeddings/test_siliconflow.py
pytest -q tests/test_memory_canonical_integration.py tests/test_world_info_evidence_provider.py
pytest -q tests/test_rag_generation_runtime.py tests/test_character_manifest.py
pytest -q tests/test_api.py tests/test_agent_service.py
pytest -q
ruff check .
python -m compileall -q app tests
git diff --check
```

若实际测试文件名与当前仓库不一致，实施时使用现有 Service/Controller 测试文件，不为满足
命令而复制测试。

## 19. 需要跨 Worker 复核的修改

### Memory Worker 必须复核

- `MemoryReaderPort.retrieve_query(..., semantic_context=None)` 的兼容形状；
- context 已提供但为 transient failed/budget skipped 时禁止 Direct retry、改走 keyword；永久、
  契约和请求错误不返回 context、不得静默降级；
- `_should_read`、explicit recall、coverage supplement、scope/provenance 和 hydrate race 不变；
- main runtime 的一个 Provider 同时服务 query coordinator 与 document extraction 是否符合
  当前生命周期预期；
- `memory.semantic_query_usage` 的安全字段、`_should_read` 后写入点和浪费率口径不改变召回行为。

### RAG Worker 必须复核

- `canonical_lore` / `canonical_voice` role 命名；
- `EvidenceProviderPort` 可选 semantic 参数；
- `search_resolved(...)` 的 exact-signature/ready-generation 行为是否足够作为 future active
  adapter 接点；
- 本任务确实没有改变 generation validation、active mapping 或 `app/main.py` provider。

### Character Worker 必须复核

- `character_examples` role 是否足够承载未来 selector；
- 后续独立 `CharacterExampleSelectorPort` 的方向；
- 本任务不修改当前静态样例内容、顺序、context tags 和 Character rules。

### Embedding Foundation Worker 必须复核

- `SemanticQueryBudget` 数值是否与已批准 Profile 的可接受输入范围一致；
- `SynchronizedEmbeddingProvider` 的 wrapper 生命周期、锁覆盖范围及 query/document 共用；
- `LocalBgeM3Provider` query 路径的 fail-loud tokenizer guard，确保不再静默截断；
- permanent/transient/request/contract error matrix 与现有 error code、retryable 语义一致；
- batch result validation、只读 vector 和 exact signature 不被 wrapper 改变。

### Observability owner 必须复核

- semantic 与 Memory usage App Log allowlist、turn_id 关联及长度字段脱敏；
- `duration_ms` 覆盖范围和 telemetry-incomplete 口径；
- 独立 follow-up：全局 `SafeTraceLogger` adapter。该 follow-up 未批准前，本任务不宣称 Trace
  写入失败安全，也不在 semantic event 局部吞错。

## 20. Manager review disposition 与待复审项

### 20.1 已批准方向

- 独立 `app/semantic/` 与 `SemanticQueryCoordinatorPort`；
- 四个具名 role，v0.1 共用一个 `turn_neutral_v0_1` 中立 query；
- `MemoryReaderPort` / `EvidenceProviderPort` 增加可选 `semantic_context`；
- `DirectQueryEmbeddingSource` 保留一个兼容周期；
- 不修改 Character Port，不接入 managed vector RAG，不改变 World Info；
- 不增加总开关、不使用 LLM rewrite、不做跨轮 cache；
- 暂时接受“存在 active role 时每个非空 turn 生成一次向量”，前提是先落实 query budget，
  并用 produced/consumed 安全日志观察实际浪费率。

### 20.2 Changes Requested 已修订

1. 新增 `semantic-query-budget/v1`：dependency 单条 1024 chars/3072 UTF-8 bytes，最终 query
   4096 chars/8000 bytes；current 超限不截断且不调 Provider，dependency 才可确定性头尾裁剪/
   从旧到新删除，并补 reason code、schema 和测试。
2. 把调用期错误按永久、Provider 契约、请求、临时和未知五类传播；只有 timeout、rate limit、
   unavailable 返回 failed context。lazy dependency/model/auth 永久错误会 unhealthy + rethrow。
3. 在 Foundation 尚无线程安全 contract 时，增加进程内 synchronized wrapper 与 Coordinator
   health state lock，覆盖并发首次加载和跨 turn 隔离测试。
4. 把 `duration_ms`、`dependency_message_count`、`query_strategy` 及 budget/length/provider-called
   安全统计加入 immutable context，deduplicated count 派生。
5. 删除 Trace 失败不影响对话的错误承诺，登记全局 safe trace adapter follow-up；Semantic
   App Log 自身仍 best effort。
6. 明确 Controller adapter 过滤 system/tool/未知 role，builder 只接收 typed user/assistant，
   并让测试职责与 schema 一致。

### 20.3 待 Manager 复审确认

1. 是否批准上述 `SemanticQueryBudget` 数值、current-input fail-closed 策略和本地 BGE query
   fail-loud token guard；该 guard 需要 Embedding Foundation Worker 复核。
2. 是否批准第 13.2 节错误分类矩阵，特别是 permanent/contract 错误使 runtime unhealthy，
   current turn 不允许 keyword 静默降级。
3. 是否批准 correctness-first 的进程内串行 wrapper；在 Foundation 提供并发能力契约前不允许
   直接共享 raw Provider 并发调用。
4. 是否批准新增 Memory usage App Log 来观察向量浪费率；该日志和 Memory 调用点需要 Memory /
   Observability owner 复核。
5. 是否同意把全局 Trace 写入安全作为独立 Observability follow-up，而不是扩大 T-0135 或在
   semantic event 上做局部异常吞噬。

Manager 审批前，Worker 不实施上述代码、不更新 Manager-owned 架构/接口/任务文档，也不接入
managed vector RAG。
