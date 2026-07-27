# T-0133 Memory Canonical Embedding Integration Delivery

## Status

Memory 运行时集成已实现并完成本地验证。Manager 于 2026-07-26 完成最终验收，T-0133
状态已更新为 `done`。

最终验收包含两项 review follow-up：Direct query 在调用 Provider 前执行与 document 一致的
NFC/LF canonicalization；vector hit hydrate 后重新验证 active/status 与查询过滤条件。对应
NFC、CRLF/LF、archive 和 disable 竞态回归均已通过。

## Scope

- `MemoryReader` 切换为 exact-signature canonical artifact 查询。
- `MemoryExtractionService` 切换为 fact-first、batch-first document artifact 写入。
- 新增 derived-pending repair service，并接入独立 Memory Worker 调度。
- Memory 与 Embedding 终态摘要接入 `AppLoggerPort`。
- 保留旧 Reader provider 参数、旧 Memory embedding 环境变量和 Persistence compatibility API
  一个版本。
- 未修改 `DialogueController`、RAG、`EvidencePack`、Session workflow 或 Memory
  scope/provenance 语义，也未修改 Persistence schema。

## Interface Changes

- Embedding Foundation 新增通用 `QueryEmbeddingSourcePort`：
  `resolve(query_text) -> ResolvedQueryEmbedding(signature, vector)`。
- 第一版 `DirectQueryEmbeddingSource` 调用统一 batch-first Provider 的 `query` purpose；
  `MemoryReader` 以后可无接口变化地接收共享单轮 query embedding。
- `MemoryExtractionResult` 和 extraction job `result_json` 固定区分：
  `created_memory_ids`、`existing_memory_ids`、`artifact_written_memory_ids`、
  `embedding_pending_memory_ids`。
- 新增 `MemoryEmbeddingRepairResult` 与 `MemoryEmbeddingRepairService.repair_batch(...)`。
- `build_controller(...)` 仅新增可选 `AppLoggerPort` 注入参数；Controller 类及其依赖 port
  没有变化。

## Reader Behavior

- 使用 `MemoryArtifactEligibility`，由 Persistence 在同一组 user/scope/active/status/
  namespace/type/confidence 条件下返回结果与 `eligible_memory_count`。
- query signature 和 vector 先通过 canonical 校验，再只查询 exact signature；同维度的其他
  vector space 不会参与评分。
- `compatible_count == eligible_memory_count` 才视为 coverage 完整。
- coverage 不完整且已有 vector 命中时，保持 vector 顺序，只用 keyword 去重补足剩余位置，
  不混合 cosine 与 keyword score。
- query embedding 失败、损坏或无有效向量命中时执行 keyword fallback。
- `forced`、`memory_focus`，以及包含“记得/还记得/回忆/之前”的中文查询，在 keyword
  无结果时继续使用 scope-visible active-memory fallback。

## Extraction Behavior

- 每条 LLM 结果先调用原子 `create_or_get_exact_active_memory(...)` 提交事实；复用时不覆盖
  既有 importance、confidence 或 provenance。
- 同一批次的唯一 Memory 内容通过一次 `document` purpose 请求批量生成 artifact。
- artifact upsert 使用完整 canonical signature、canonical content hash 与统一向量校验。
- Provider 或 artifact 写入失败不会回滚 Memory 事实；失败项进入
  `embedding_pending_memory_ids`，extraction job 仍可 completed。

## Repair And Worker

- Repair 只消费 `list_memory_embedding_artifact_gaps(...)` 返回的当前 exact-signature
  `missing/content_stale/vector_corrupt/signature_corrupt/legacy_only` 缺口。
- 每次最多处理 `AEMEATH_MEMORY_REPAIR_BATCH_SIZE`，按稳定 cursor 前进；失败批次保留 cursor，
  交由后续 repair 重新发现。
- Repair 使用独立 interval 和指数退避，上限分别由
  `AEMEATH_MEMORY_REPAIR_INTERVAL_SECONDS` 与
  `AEMEATH_MEMORY_REPAIR_MAX_BACKOFF_SECONDS` 控制。
- 第一版保持单 Worker；未增加 extraction job 自动重试，也不宣称已有该能力。
- 其他 signature artifact 合法共存，repair 不覆盖也不删除它们。
- 移除了虚构 query 预热；Provider 仍由 approved profile gate 和统一 Factory 创建，配置错误
  fail-fast，禁止隐式 Hash fallback。

## Observability

新增或接入以下安全摘要事件：

- `memory.recall_completed`
- `memory.recall_degraded`
- `memory.extraction_completed`
- `memory.repair_completed`
- `memory.repair_failed`
- `memory.worker_job_completed`
- `memory.worker_job_failed`

Memory 日志使用字段白名单，只记录 provider/profile、数量、coverage、fallback、trigger、
gap reason 和 repair delay 等终态摘要。不记录 memory content、query、vector、hash、provider
body、数据库路径、API key 或其他秘密；日志失败不改变业务结果。

## Modified Files

- Embedding query contract：`app/embeddings/ports.py`、`schemas.py`、`query.py`、`__init__.py`
- Memory runtime：`app/memory/reader.py`、`extractor.py`、`repair.py`、`worker.py`、
  `runtime.py`、`schemas.py`、`observability.py`、`admin.py`
- Runtime wiring/config：`app/main.py`、`app/config.py`、`app/observability/runtime.py`、
  `.env.example`、`README.md`
- Documentation：`docs/architecture.md`、`docs/interfaces.md`、`docs/task_log.md`
- Tests：`tests/test_memory_canonical_integration.py`、
  `tests/store_contract/test_memory_runtime_canonical_contract.py`、
  `tests/test_memory_worker.py`、`tests/test_memory_v0_3.py`、
  `tests/store_contract/test_sqlalchemy_memory_worker_smoke.py`、
  `tests/embeddings/test_factory_config.py`

## Validation

- Focused canonical integration：46 passed。
- Embedding + 双 backend Persistence + Memory + 中文行为回归：214 passed。
- 全量 `python -m pytest -q`：349 passed。
- `python -m ruff check .`：All checks passed。
- `git diff --check`：无 whitespace error，仅 Windows CRLF 提示。

## Remaining Risks

- 当前只有 `deterministic_hash_v1` profile 获批，适合测试和显式开发，但不代表真实语义召回
  质量；BGE-M3 / SiliconFlow 在单独批准前会 fail-fast。
- Repair 第一版按单 Worker 设计；人工启动多个 Worker 时 Persistence upsert 仍幂等，但可能
  发生重复 embedding 计算，尚无 repair lease。
- Extraction failed job 仍无自动重试；本轮只实现 derived artifact repair。
- 当前 query source 是 Direct，每轮由 Memory 发起一次 query embedding；共享
  `TurnSemanticContext` 属于后续集成。
- Legacy provider 参数、环境变量和 embedding compatibility API 仅承诺保留一个版本，后续需
  单独安排移除任务。
