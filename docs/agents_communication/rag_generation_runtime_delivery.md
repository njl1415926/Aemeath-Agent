# RAG Managed Generation Runtime Delivery

## 状态

RAG 对 Embedding Foundation / canonical artifact store 的运行时适配已实现。当前交付只提供
独立的 generation builder、registry、canonical retriever 和管理 CLI，不修改
`app/main.py`，不替换主运行时的 `WorldInfoEvidenceProvider`。

向量 RAG 仍未接入对话主链路；正式启用需要 Manager 后续单独批准。

## 修改范围

- `app/retrieval/index_manifest.py`
  - canonical corpus inventory 与 fingerprint
  - generation / active / checkpoint manifest
  - metadata 白名单和安全 `source_id`
  - 同目录临时文件、flush、fsync、有限重试、`os.replace`、reopen verify
- `app/retrieval/index_lock.py`
  - Windows `msvcrt.locking` / POSIX `fcntl.flock` 跨进程锁
  - 有限超时，不依赖普通 lock 文件是否存在
- `app/retrieval/generation.py`
  - `building / staging / ready / failed` 生命周期
  - full / incremental build、断点续建、stale chunk 删除、失败保留旧 ready
  - ready 前 WAL checkpoint、SQLite integrity check、exact-signature coverage
  - active generation 原子切换
- `app/retrieval/canonical_retriever.py`
  - provider-neutral `QueryEmbeddingSourcePort` / `ResolvedQueryEmbedding`
  - exact-signature ready generation 查询
  - 内部 `CanonicalRagSearchOutcome`
  - 对外继续实现现有 `RetrieverPort` 列表返回形式
- `app/retrieval/manage_rag_index.py`
  - 独立 `build / status / query` CLI
- `app/retrieval/errors.py`
  - 不含路径和敏感数据的稳定 managed-index 错误码
- `.gitignore`
  - 忽略 `data/rag_indexes/`
- `tests/test_rag_generation_runtime.py`
  - generation runtime、门禁、安全 metadata、CLI 和恢复测试

没有修改 Persistence schema，也没有在 RAG 层写 artifact/chunk 表的私有 SQL。SQLite
artifact 操作只通过 Persistence Worker 交付的 `SqliteVectorStore` 窄接口完成。RAG 层仅在
ready seal 阶段执行 SQLite 的 WAL checkpoint 与 integrity PRAGMA。

## 核心不变量

1. 每个 managed generation 只声明一个 canonical embedding signature。
2. 文档构建使用 batch-first `EmbeddingProviderPort`，purpose 固定为 `document`。
3. 查询只使用 exact-signature 且状态为 ready 的 active generation。
4. ready coverage 必须完整覆盖 canonical inventory，且不得包含 legacy/incompatible artifact。
5. ready SQLite 只读打开，不自动初始化或迁移。
6. 构建失败只标记新 generation；旧 ready generation 和 active 映射保持不变。
7. 同 signature 且 parser/chunker 契约不变时允许 clone + delta；契约变化时 full rebuild。
8. 首个 managed generation 一律从权威语料重建，不复用 legacy RAG vector。
9. Hash 必须使用 approved `deterministic_hash_v1`，并显式传 `--allow-hash`；无隐式 fallback。
10. Local BGE-M3 / SiliconFlow profile 在 approved 前无法进入 managed build/query 路径。

## Fingerprint 与安全边界

Corpus inventory 的每项包含：

- `chunk_id`
- `namespace`
- `source_type`
- `canonical_content_hash`
- `retrieval_metadata_fingerprint`

metadata fingerprint 只纳入影响检索结果的白名单字段。绝对路径、构建时间和任意原始
metadata 不进入 fingerprint，也不写入 managed chunk。Canonical retriever 不复制 chunk
metadata，只从零构造以下 trace 字段：

- `generation_id`
- `retriever`
- `raw_score`
- `namespace`
- `source_type`
- `signature_fingerprint`
- `source_id`

App Log 只记录 index id、generation id、signature fingerprint、状态、计数和安全错误码；不
记录 query 文本、文档正文、源路径、provider credential 或 secret。

## 配置与使用

默认 index root：`data/rag_indexes`

可选环境变量：`AEMEATH_RAG_INDEX_ROOT`

开发 Hash build：

```bash
python -m app.retrieval.manage_rag_index build --profile deterministic_hash_v1 --allow-hash
```

状态查看：

```bash
python -m app.retrieval.manage_rag_index status
```

开发 Hash query：

```bash
python -m app.retrieval.manage_rag_index query --profile deterministic_hash_v1 --allow-hash "query"
```

`--index-root` 与 `--index-id` 是全局参数，应放在子命令之前。

## Legacy 与回滚

- 旧 `data/aemeath_rag.sqlite` 不会自动导入 managed generation。
- `upsert_chunks()` / `search()` 保留为一个版本的 compatibility shim，本次未删除。
- 旧 generation v1 暂不自动清理，避免误删仍被进程读取或用于人工回滚的 DB。
- 回滚通过在 build lock 内把对应 exact-signature 的 `active.json` 映射切回已验证 ready
  generation；本次没有提供自动 rollback 命令，避免未经审核的运维操作。

## 验证

当前定向验证：

```text
pytest -q tests/test_rag_generation_runtime.py tests/test_vector_artifact_store.py tests/test_rag_generation_store_contract.py
25 passed
```

完整验收结果：

```text
pytest -q
374 passed

ruff check .
All checks passed!

python -m compileall -q app tests
passed

git diff --check
passed; only pre-existing/worktree CRLF conversion warnings

python -m app.retrieval.manage_rag_index --index-root .aemeath/rag_cli_smoke status
active=(none)
generations=(none)
```

## 未启用项与后续门禁

- 未修改 `app/main.py`，当前 active provider 仍为 World Info。
- 未把 `CanonicalRagRetriever` 包装进 active `EvidenceProviderPort`。
- 未批准 BGE-M3 或 SiliconFlow profile。
- 未引入 legacy provenance importer、自动 retention cleanup 或自动 rollback。
- 正式接线前仍需 Manager 批准 provider profile、index build 产物和 EvidenceProvider 组合策略。

## 2026-07-27 Manager Review Revision

初次验收状态为不通过，generation 管理层的五项阻断问题已完成修订：

1. 统一 `validate_ready_generation()` 覆盖 registry、no-op、ready 复用和激活；损坏 ready
   不再重新激活，而是跳过并创建新 generation。
2. 空目录或损坏 manifest 的非 active generation 被隔离为安全诊断，不再阻断 build/status；
   generation number 仍从合法目录名计算，避免编号复用。
3. `write_batch_size` 纳入 build-plan fingerprint，不同分批配置不会续用旧 batch index。
4. staging 恢复在任何 ready 转换前强制验证 inventory 存在性、顺序和 fingerprint。
5. `active.json` 替换后 reopen verify 失败时恢复并核对原始字节。

同时，Canonical Retriever 在 artifact diagnostics 非空时返回 `index_corrupt` 和空结果，
不再报告 `available`。

修订后验证：

```text
Focused RAG generation/Persistence suite: 31 passed
Full pytest suite: 380 passed
```

Ruff、compileall 和 diff check 已再次通过；diff check 仅有工作区 CRLF 转换提示。主运行时
边界未变化。

## Manager Final Acceptance

最终验收于 2026-07-27 通过，T-0134 状态更新为 `done`。

- Manager 独立复跑定向 31 项与全量 380 项测试，结果通过。
- `ruff check .`、`python -m compileall -q app tests` 与 `git diff --check` 均通过；后者仅报告
  既有 CRLF 转换提示。
- 六组故障注入场景与五项原阻断问题均已闭合，未发现新的正确性问题。
- `app/main.py` 未接入 managed vector RAG，`WorldInfoEvidenceProvider` 仍是主运行时 provider。

非阻断后续项：正式接入对话热路径前，应基准测试 registry resolution 的完整 ready 校验开销，
必要时按不可变 active-generation 身份增加不削弱正确性的 validated-handle cache。BGE-M3 或
SiliconFlow Profile 批准、生产语料构建验收及 `EvidenceProviderPort` 组合接线仍需单独审批。
