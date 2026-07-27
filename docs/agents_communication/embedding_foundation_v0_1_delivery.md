# Worker Session 13：Embedding Foundation v0.1 交付报告

日期：2026-07-23

状态：Foundation 实现与收尾修复完成，等待 Manager Session 复审

## 1. 交付结论

本轮完成中立 `app/embeddings/` Foundation，并按 Manager 有条件批准后的四项要求完成
收口：

1. 普通运行时 Factory 不再信任调用方 Profile 的 `release_status` 字符串。Factory 会按
   `profile_id`重新加载仓库内 canonical manifest，完整校验manifest、canary、baseline、
   artifact digest和expected fingerprint，确认其为`approved`，再逐字段比对调用方对象，
   最后只使用重新加载的canonical Profile创建Provider。
2. `EmbeddingRuntimeOptions.batch_size`统一定义为Provider内部拆批大小，不是Port逻辑batch
   上限。SiliconFlow会自动把大逻辑batch拆成多个`/v1/embeddings`请求，逐chunk校验并按
   原输入顺序合并。
3. Qualification CLI实现`embedding.qualification_completed`与
   `embedding.qualification_failed`单终态App Log；日志使用固定allowlist，Logger失败不
   改变qualification结果或退出码。
4. `deterministic_hash_v1`提交确定性离线坐标baseline并成为当前唯一`approved` Profile，
   用作canonical Factory无网络smoke。SiliconFlow与本地BGE仍为`review`且没有baseline。

未修改Persistence schema、Memory召回/repair/job业务策略、RAG索引或
`DialogueController`。

## 2. Foundation 能力

新增的中立模块提供：

- batch-first `EmbeddingProviderPort`、请求/结果/signature/runtime schema；
- SiliconFlow `Pro/BAAI/bge-m3` Provider，固定1024维、批量`/v1/embeddings`、不发送
  `dimensions`、客户端输出统一为L2-normalized little-endian float32；
- 本地BGE-M3 Provider，lazy load、CLS pooling和内部拆批；
- Deterministic Hash Provider，仅用于测试和显式开发，不作为任何运行失败fallback；
- 版本化`EmbeddingSpaceProfile`、strict artifact loader、坐标级qualification；
- 统一Factory、固定`SILICONFLOW_API_KEY` secret边界、optional dependency extras；
- Retrieval旧import/单条方法和旧Memory配置的一发布周期兼容shim；
- `EMBEDDING_*` typed errors与AppLogger observability wrapper。

## 3. 本轮收尾契约

### 3.1 Factory 门禁

`create_embedding_provider(...)`仅接受由当前仓库资源完整验证的canonical approved Profile：

- 拒绝伪造`review -> approved`；
- 拒绝对approved Profile任何向量空间、artifact或qualification字段的篡改；
- 拒绝发生在secret解析、HTTP client创建或本地模型加载之前；
- `create_qualification_provider(...)`仍是唯一允许review Profile的显式probe入口。

### 3.2 batch_size 与 SiliconFlow

- 一个Port调用可以包含任意数量的文本；`batch_size`只限制Provider内部chunk；
- 每个chunk独立执行有限retry，已成功chunk不会重发；
- 后续chunk最终失败时停止，不返回部分结果；错误中的`retry_count`包含此前成功chunk和当前
  失败chunk的总额外尝试次数；
- 所有chunk usage都存在时求和，任一chunk缺失usage时逻辑结果为unknown；
- 多chunk diagnostics按顺序保留`provider_request_ids`，兼容单值
  `provider_request_id`仅在恰好一个HTTP chunk时填写。

### 3.3 Qualification 终态日志

每次`capture-candidate`或`verify`恰好写一条qualification终态：

- 成功：`embedding.qualification_completed`；
- probe不通过或任意受控异常：`embedding.qualification_failed`；
- payload仅包含`command`、`provider`、`profile_id`、`release_status`、`dimension`、
  `canary_count`、`observation_count`、`signature_fingerprint`和`duration_ms`；
- 错误码写入`AppLogRecord.error_code`；
- 不记录canary原文、向量坐标、baseline/candidate路径、artifact digest、API key、headers或
  response body。

### 3.4 Hash approved Profile

`deterministic_hash_v1`当前治理信息：

```text
release_status: approved
dimension: 384
canary_artifact_sha256:
  sha256:f30cab7931f4d6e0fa3102a292bb10b12e26e98085bee6bda3ae434514435fed
baseline_artifact_sha256:
  sha256:c22c762b36075fe2bd7144fd00b5fa6bd9ef6819c79473c7fe47166a463c5e8e
expected_fingerprint:
  sha256:84e6deb3fbf4221241817a821e9651e863465202ef9eabba8e19ba00a5541c4b
```

测试会通过canonical Factory重新计算全部canary向量并与提交的`f32le` baseline逐字节比较，
随后执行重复batch和single-order离线qualification。

Profile发布状态保持为：

| Profile | 状态 | Baseline | 运行时用途 |
|---|---|---|---|
| `deterministic_hash_v1` | `approved` | 已提交，离线确定性 | 仅测试/显式开发 |
| `siliconflow_pro_bge_m3_v1` | `review` | 无 | qualification专用，不能运行时选择 |
| `local_bge_m3_v1` | `review` | 无 | qualification专用，不能运行时选择 |

没有提交、生成或启用SiliconFlow baseline。其正式启用仍需独立完成真实API capture、人工审查
和verify。

## 4. 主要文件范围

新增核心目录：

- `app/embeddings/`：Port、schema、Factory、配置、validation、errors、observability、
  qualification和兼容能力；
- `app/embeddings/providers/`：SiliconFlow、本地BGE和Hash Provider；
- `app/embeddings/profiles/`：三个manifest、非敏感canary和Hash离线baseline；
- `tests/embeddings/`：Provider、Factory、profile安全、qualification、observability、optional
  dependencies和legacy shim专项测试。

最小集成修改：

- `app/config.py`：canonical runtime options及旧配置兼容；
- `app/memory/runtime.py`：仅将Provider创建转发到统一Factory，不改变Memory业务策略；
- `app/retrieval/embeddings.py`：一发布周期旧import shim；
- `app/errors/`：新增稳定`EMBEDDING_*`错误映射；
- `pyproject.toml`：`embedding-api`、`embedding-local` extras和Profile artifact package data；
- `.env.example`：继续保留兼容配置，SiliconFlow canonical示例不启用。

## 5. 验证结果

```text
python -m pytest tests/embeddings -q
  114 passed

python -m pytest -q
  282 passed

python -m ruff check .
  All checks passed!

git diff --check
  无 whitespace error（仅Git的CRLF提示）

python -m app.embeddings.qualify verify \
  --profile deterministic_hash_v1 --allow-hash --batch-size 1
  passed=true；3个canary的cosine=1.0、max coordinate delta=0.0；
  expected_fingerprint == observed_fingerprint
```

专项测试包含：

- forged Profile和逐字段tamper在Provider初始化前被拒绝；
- canonical Hash Factory不解析secret、不访问网络；
- SiliconFlow `N=5, batch_size=2`拆批、chunk乱序恢复、全局顺序、usage/retry聚合和失败停止；
- qualification成功、probe false、早期异常、capture嵌套probe及Logger失败路径的单终态事件；
- 日志敏感哨兵与精确allowlist检查；
- SiliconFlow与本地BGE持续保持review/无baseline断言。

测试仅出现项目既有的Starlette/httpx、SQLite datetime adapter以及旧Memory embedding配置
deprecation warning，无失败。

## 6. 明确未做

- 未修改数据库schema、repository或vector持久化格式；
- 未决定Memory缺失/混合向量时的降级策略，未实现Memory repair；
- 未修改RAG manifest、staging或索引；
- 未启用World Info向量RAG；
- 未实现TurnSemanticContext、query扩写或跨轮缓存；
- 未修改`DialogueController`；
- 未执行或提交真实SiliconFlow qualification baseline。

## 7. 请求 Manager 复审

请重点确认：

1. canonical reload +完整校验+逐字段相等的Factory门禁是否满足runtime Profile治理；
2. `batch_size`内部chunk语义，以及多chunk usage/retry/request-id聚合契约；
3. qualification completed/failed事件的单终态与allowlist；
4. Hash approved Profile仅作为离线smoke、SiliconFlow/本地BGE继续review的发布边界。

复审通过前不继续实施Persistence、Memory、RAG或Semantic Query接入。
