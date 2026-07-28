# T-0135 Semantic Query v0.1 交付报告

日期：2026-07-27

Worker：Worker Session 15 / Semantic Query

状态：`done`

## 1. 交付结论

T-0135 已按批准设计完成实现。主对话的一次 turn 现在可以构造非持久化的
`TurnSemanticContext`，对 active role 的同一中立 query 做一次 `purpose="query"` embedding，
再把同一只读 `ResolvedQueryEmbedding` 传给 Memory 和 Evidence 边界。

当前 composition root 只激活 `memory_recall`；managed canonical RAG 仍未接入，
`WorldInfoEvidenceProvider` 继续使用原始 query 做关键词检索，Character 的样例选择和 Port 均未改变。
没有实现 Prompt Context Budget、跨轮 cache 或 Persistence schema 变更。

## 2. 实现内容

### 2.1 Semantic 共享层

新增 `app/semantic/`：

- `budget.py`：canonical `semantic-query-budget/v1`，阈值固定为 dependency 每条
  1024 chars / 3072 UTF-8 bytes、总 query 4096 chars / 8000 UTF-8 bytes；相同版本篡改任一
  阈值会在 `__post_init__` 拒绝；
- `schemas.py`：四个具名 role、请求/状态/resolution、`SemanticTurnInput`、
  `TurnSemanticContext` 和 content-free `safe_summary()`；
- `query_builder.py`：确定性上下文依赖判定、按 message ID 排除当前消息、读取最后一组
  user/assistant 历史、dependency head/tail 截断、超总预算时从最旧 dependency 开始丢弃，
  current input 超预算 fail-closed；
- `coordinator.py`：固定 role 顺序、单轮精确去重、一次 batch-first Provider 调用、exact signature、
  count/dimension/finite/L2 norm 校验、只读向量映射、错误分类和 runtime unhealthy 状态；
- `ports.py` / `disabled.py`：稳定 `SemanticQueryCoordinatorPort` 和不创建 Provider 的 disabled
  实现；
- `errors.py` / `observability.py`：稳定 unhealthy 异常及安全 App Log terminal event。

共享层不保存 query、历史正文或 vector；所有这些值只存在于当前调用的局部对象中。

### 2.2 Embedding Foundation 接点

新增 `SynchronizedEmbeddingProvider`，其 `embed()` 锁覆盖 delegate 的完整调用，包括内部
retry/backoff。wrapper 不捕获异常、不复制结果，也不修改 vectors、signature、usage、
diagnostics、retry count 或 request ID；异常退出后锁由 context manager 释放。

主 composition root 的顺序固定为：

```text
SynchronizedEmbeddingProvider(
    ObservableEmbeddingProvider(concrete_provider)
)
```

`app/main.py` 只包装 `build_memory_embedding_provider()` 返回的公开 Factory 结果，没有调用
`_create_provider()`。这里的单一 wrapper 只约束同一 runtime/composition root；qualification、
Memory 管理工具和 RAG CLI 仍可拥有各自独立 Provider 实例。

Local BGE 的 query 路径现在使用 `truncation=False`，读取 tokenizer 实际 `input_ids` 长度并在
超过 `local_max_length` 时原样抛出
`EmbeddingInvalidInputError("query_token_limit_exceeded")`。合法 query 直接复用这次 tokenization
产生的 encoded tensors 推理。document 路径继续使用
`truncation=True, max_length=8192`（或 runtime 配置的同一 max length），原有推理和向量语义未改。

Foundation Observable 的 duration 从进入内层 Observable 后开始，因此不含外层锁等待；Semantic
Coordinator 从 query construction 前计时，因此包含 state lock 和 Provider wrapper 的等待时间。

### 2.3 Controller、Memory 与 Evidence

`DialogueController` 在保存当前用户消息后：

1. 先按 `message.id != current_user_message_id` 排除当前已保存消息，不按正文去重；
2. 只把过滤后的 user/assistant 消息适配成 `SemanticMessage`；
3. 调用 Coordinator；
4. 把同一个 `TurnSemanticContext` 对象传给 `MemoryReaderPort` 和 `EvidenceProviderPort`。

Controller 只做输入适配、编排和传递，不做依赖判定、query rewriting 或检索策略。

`MemoryReaderPort.retrieve_query()` 新增可选 `semantic_context`：

- 先保留原 `_should_read()` 判断；
- `memory_recall=ready` 时优先使用共享 resolved vector，并继续执行 exact-signature artifact 查询；
- `failed` / `skipped` / `not_requested` 时只走现有关键词和显式回忆 fallback，不在同一 turn
  再调用 Direct Provider；
- `semantic_context is None` 时保留 Direct source 一个兼容周期；
- ready vector 是否实际进入 vector search 通过 content-free
  `memory.semantic_query_usage` 记录 consumed 口径。

`EvidenceProviderPort.build_evidence_pack()` 新增可选 `semantic_context`。Empty、Composite、legacy
和 WorldInfo adapters 均保持现有结果；本任务没有激活 `CanonicalRagRetriever.search_resolved()`。

同一 runtime 内的 Coordinator、Memory Reader Direct source 和 Memory Extraction Service 均收到
同一个 synchronized wrapper，对象 identity 已测试。

## 3. 失败契约

| 类型 | 当前 turn | 后续 turn |
| --- | --- | --- |
| timeout / rate limit / unavailable | 返回 `failed` context；消费者可做本地 fallback | 继续尝试 |
| config / secret / dependency / auth / qualification | 保存安全 unhealthy reason 与 ErrorCode，重新抛出原 typed error | 抛 `SemanticCoordinatorUnhealthyError` / `SEMANTIC_COORDINATOR_UNHEALTHY` |
| signature / result / vector 契约错误 | 标记 unhealthy，重新抛出 typed contract error | 同上 |
| invalid input / request rejected | 原样重新抛出，不映射为 unavailable，不标记 unhealthy | 下一合法请求可继续 |
| current input 超 SemanticQueryBudget | 不调用 Provider，返回 `skipped` | 不影响 runtime 健康状态 |

unhealthy state 只保存固定 reason 和原稳定 ErrorCode，不保存异常对象、正文、vector、credential
或 Provider response body。修复配置后必须重建 runtime；没有 TTL 或隐式 Hash/Disabled fallback。

## 4. 文件清单

新增：

- `app/embeddings/synchronized.py`
- `app/semantic/__init__.py`
- `app/semantic/budget.py`
- `app/semantic/coordinator.py`
- `app/semantic/disabled.py`
- `app/semantic/errors.py`
- `app/semantic/observability.py`
- `app/semantic/ports.py`
- `app/semantic/query_builder.py`
- `app/semantic/schemas.py`
- `tests/embeddings/test_synchronized.py`
- `tests/test_semantic_query.py`
- `tests/test_semantic_query_integration.py`
- `docs/agents_communication/semantic_query_v0_1_delivery.md`

修改：

- `app/embeddings/providers/local_bge_m3.py`
- `app/embeddings/__init__.py`
- `app/agent/dialogue_controller.py`
- `app/agent/ports.py`
- `app/main.py`
- `app/memory/reader.py`
- `app/memory/disabled.py`
- `app/memory/observability.py`
- `app/retrieval/ports.py`
- `app/retrieval/provider.py`
- `app/errors/codes.py`
- `app/errors/http.py`
- `tests/embeddings/test_local_bge.py`
- `tests/test_memory_canonical_integration.py`
- `tests/test_errors.py`
- `docs/agents_communication/semantic_query_v0_1_design.md`

未修改 Embedding Port、Profile、signature、Factory 门禁、Persistence schema、Character 文件、
canonical RAG generation lifecycle、`docs/architecture.md`、`docs/interfaces.md` 或
`docs/task_log.md`。后三份 Manager 文档待复核接受后由 Manager 更新。

## 5. 验证结果

通过：

```text
ruff check .
All checks passed

pytest -q tests/test_semantic_query.py tests/test_semantic_query_integration.py \
  tests/embeddings/test_synchronized.py tests/embeddings/test_local_bge.py \
  tests/test_memory_canonical_integration.py tests/test_errors.py
67 passed

pytest -q tests/test_world_info_evidence_provider.py \
  tests/test_world_info_main_integration.py tests/test_world_info_retrieval.py \
  tests/test_character_registry.py tests/test_character_manifest.py \
  tests/test_rag_generation_runtime.py tests/test_rag_generation_store_contract.py
55 passed
```

覆盖包括：

- canonical budget 防篡改和 current-input fail-closed；
- current message ID 排除、正文不去重、最后一组 user/assistant 和 deterministic budget；
- 四 role 共用一次 query batch、exact signature、结果数量、NaN/Inf/norm 和只读 vector；
- 五类错误传播、永久/契约 unhealthy、固定后续异常和不保留原异常；
- Local BGE 合法 query 等价、实际 token 超限、原样 invalid-input 传播和 document 回归；
- wrapper 结果/异常 identity、完整临界区、异常释放、并发首次 lazy load、terminal event 恰好一次；
- 两个并发 turn 不串数据；并发测试使用 Barrier/Event/可观测锁，不依赖 sleep；
- 同 runtime 三个消费者的 wrapper 对象 identity；
- Memory ready consumed/unused、failed context keyword fallback 且不 Direct retry；
- WorldInfo 在 disabled/ready/failed context 下结果不变；Character 与 RAG generation 回归。

全量 `pytest -q` 收集 420 项，其中 411 项通过，9 项被当前仓库的 BGE canary artifact digest
基线不一致阻断：Profile 期望
`sha256:6de0f6d98fc5049c6b914856fe19c0aefc9a53ef93fb5d1855eb123c8636f474`，当前未修改的
`app/embeddings/profiles/canaries/bge_m3_canary_v1.json` 实际为
`sha256:cbde65c5acffa89df7a1b4db06c819b9dca480113e3491ba64af55b593666ec3`。
失败集中在 `test_factory_config.py`、`test_hash_approved_profile.py` 和
`test_optional_imports.py` 的 9 个 qualification/profile 读取用例。T-0135 没有修改该 canary 或任何
Profile；按本任务门禁没有改写摘要、批准 Profile 或绕过校验。

### Manager 预验收补充（2026-07-27）

上述 9 项失败已由 Manager 定位为 T-0135 之外的 checkpoint 收束问题：
`bge_m3_canary_v1.json` 是按原始字节校验的受控 artifact，先前提交前的格式清理误删了
Profile 摘要所包含的末尾 LF。恢复该 artifact 到 Profile 声明的
`sha256:6de0f6d98fc5049c6b914856fe19c0aefc9a53ef93fb5d1855eb123c8636f474`
后，Manager 复跑结果为：

```text
pytest -q
420 passed

ruff check .
All checks passed

python -m compileall -q app tests
passed
```

同时新增仓库级 `.gitattributes` 约束，固定 Embedding Profile/Canary JSON 使用 LF，
并将 `.f32le` 标记为 binary，防止 Windows `core.autocrlf` 再次改写受哈希资源。
该修复未修改 Profile 内容、签名、审批状态或 Factory 门禁。原始 411/420 记录保留用于说明
Worker 当时观察到的环境状态；它不再构成 T-0135 的验收阻塞。

### Manager 最终格式治理（2026-07-28）

Embedding cross-worker review 随后确认，恢复后的 canary 依赖两个末尾 LF 才能匹配旧摘要，
会使 `git diff --check` 报告 `new blank line at EOF`。Manager 因此将
`bge_m3_canary_v1.json` 规范为 UTF-8、无 BOM、LF 且恰好一个末尾 LF，并同步把
`local_bge_m3_v1` 与 `siliconflow_pro_bge_m3_v1` 的 artifact 摘要更新为：

```text
sha256:cbde65c5acffa89df7a1b4db06c819b9dca480113e3491ba64af55b593666ec3
```

两个 BGE Profile 仍保持 `review`，没有批准 Profile、生成 baseline 或绕过 Factory 门禁。
新增回归测试固定 canary 无 BOM、无 CR、仅一个末尾 LF，并继续通过原始字节摘要门禁。
当前全量 `pytest`、`ruff check .`、`python -m compileall -q app tests` 与
`git diff --check` 均通过。此前基于旧原始字节摘要生成的 qualification candidate 不应继续
用于未来审批；BGE Profile 进入 approval 前需要重新 capture/verify。

## 6. 复核请求

- Embedding Foundation Worker：复核 Local BGE query token guard、document 不变性、wrapper
  透明性、包装顺序、锁范围和 Observable terminal/duration 边界。
- Memory Worker：复核 ready 优先、非 ready 不 Direct retry、关键词/显式回忆与 scope/provenance
  不变，以及 `memory.semantic_query_usage` 口径。
- RAG Worker：复核 Evidence optional 参数兼容、WorldInfo 行为不变、canonical RAG 仍未 active。
- Character Worker：复核 Character Port/规则/静态样例结果未变；`character_examples` 仅为未激活 role。
- Observability Worker：复核 Semantic safe summary、failure reason、Memory usage allowlist 和脱敏边界；
  全局 Safe Trace adapter 仍是独立 follow-up。

复核期间无需修改 T-0135 设计边界。若五方复核通过，Manager 可更新 architecture/interfaces/task log
并将任务状态改为完成。

## 7. Manager 最终验收（2026-07-28）

Embedding Foundation、Memory、RAG、Character 和 Observability 五方复核均已通过。Embedding
复核指出的 canary 末尾空行问题已按原始字节治理：文件现在固定为 UTF-8、无 BOM、LF 且恰好
一个末尾 LF，Profile 摘要同步更新，但两个 BGE Profile 仍保持 `review`。旧 qualification
candidate 因原始字节身份变化而失效，后续批准前必须重新 capture/verify。

Manager 已同步 `docs/architecture.md`、`docs/interfaces.md` 和 `docs/task_log.md`。T-0135 的
运行时边界保持不变：当前只激活 `memory_recall`；World Info 仍是 active evidence provider；
canonical RAG、Character example selection、Context Budget 和跨轮 cache 均未在本任务接入。

最终验证：全量 `pytest` 422 项通过；`ruff check .`、
`python -m compileall -q app tests` 和 `git diff --check` 均通过。diff check 仅输出 Windows
checkout 的预期 CRLF conversion warning，没有 whitespace error。
