# Embedding Foundation v0.1 设计方案

日期：2026-07-15（2026-07-23 收尾修订）

状态：Manager 有条件批准后已完成 Foundation 实现及收尾修复，等待 Manager Session 复审

范围：本 Worker 仅实现 Foundation、Provider、Factory、配置、兼容 shim、Provider
contract tests 和 qualification probe；Persistence、Memory、RAG 与单轮 Semantic Query
分别交给后续 Worker。

## 0. 结论摘要

Embedding Foundation v0.1 把当前位于 Retrieval 的 Embedding port、Hash Provider 和
本地 BGE-M3 Provider 提取到中立的 app/embeddings/ 模块。生产 Remote Provider 已确定为
SiliconFlow，固定调用 https://api.siliconflow.cn/v1/embeddings，固定模型 ID
Pro/BAAI/bge-m3，固定输出 1024 维，不发送 dimensions。

本地 BGE-M3 继续保留用于本地或离线部署。SiliconFlow Pro/BAAI/bge-m3 与本地
BAAI/bge-m3 必须视为不同 Provider、不同 signature 和不同向量空间，不能交叉查询。

向量空间身份由仓库内版本化EmbeddingSpaceProfile manifest定义；环境变量只选择profile、
提供SILICONFLOW_API_KEY和设置不影响signature的运行参数。Qualification相对版本化
canary baseline执行坐标级验证，只有expected_fingerprint与observed_fingerprint一致且
逐向量阈值通过才合格。

整体能力分成两个平面：

~~~text
稳定资料索引平面
  RAG 文档 / 长期记忆 / future Character 稳定样例
  -> document-purpose batch embedding
  -> 按业务实体持久化

在线查询平面
  用户本轮 query
  -> future Semantic Query 层统一收集并批量生成
  -> 单轮共享 query-purpose embedding
  -> 仅保留在本轮进程内存
  -> turn 结束后丢弃
~~~

Embedding Foundation 本身是被动、按需调用的基础能力。它不决定每个模块分别请求 API，
也不让 DialogueController 直接承担 Embedding 业务。单轮 query 的统一生成、批处理和共享
由 Foundation 之后的独立 Semantic Query / TurnSemanticContext 任务设计。本 Worker 只
保证 Port 保留 query/document purpose，并提供 batch-first 能力。

Foundation确认维度相同不足以证明向量空间兼容，并提供完整signature供后续判断。是否再以
content hash、BLOB状态和哪些数据库字段决定复用、拒绝或重建，由Persistence Worker提交
设计后审批；第6节相关矩阵仅为待审候选。

不允许 SiliconFlow/本地 BGE 失败后静默切换 Hash，也不允许仅因维度相同就跨模型查询。
Hash 只用于测试和显式开发模式。

本设计记录后续Persistence、Memory repair和RAG manifest的待审候选，但当前Worker不得
实现或替后续模块冻结这些内容。本设计也不修改DialogueController，不启用向量World Info RAG，不
实现 TurnSemanticContext、Character 样例语义选择、Context Budget、跨轮 query vector
缓存、Redis、LangGraph、MCP、PostgreSQL 或 Alembic。

## 1. 当前依赖与问题清单

### 1.1 当前依赖方向

当前 Embedding 实现位于 app/retrieval/embeddings.py：

~~~text
app/retrieval/embeddings.py
  <- app/retrieval/*
  <- app/memory/runtime.py
  <- app/memory/reader.py
  <- app/memory/extractor.py
  <- Memory Worker / main runtime
~~~

这使 Memory 直接依赖 Retrieval 的基础接口和具体 Provider，违反“业务模块依赖中立
协议，不互相借用实现”的项目原则。

目标依赖方向应为：

~~~text
app/embeddings/
  <- Memory
  <- Retrieval / RAG
  <- future Character semantic assets
~~~

### 1.2 Port 契约不足

现有 EmbeddingProviderPort 只有：

- model_name；
- dimension；
- embed_texts(list[str])；
- embed_query(str)。

当前 Port 没有约定：

- query 与 document 两种用途；
- batch 请求和 batch 结果 schema；
- 返回数量和输入顺序；
- dtype、shape、finite、normalization；
- model revision、pooling、instruction 和 preprocessing；
- usage；
- 稳定错误类型；
- Provider 生命周期和资源归属。

numpy 数组直接在多个业务和存储接口之间传递，但没有统一验证层。二维数组、NaN、
Inf、错误维度或非法 BLOB 都可能到达持久化层。

### 1.3 Provider Factory 和配置分叉

当前至少存在四套创建逻辑：

- app/retrieval/build_wuwa_rag.py；
- app/retrieval/query_wuwa_rag.py；
- app/retrieval/rag_cli.py；
- app/memory/runtime.py。

Memory 通过 Settings 读取配置；RAG 工具通过 argparse 自行创建 Provider。不同入口对
device、batch size、model path 和 Provider 别名的支持不一致。

主进程与 Memory Worker 已经共用 build_memory_embedding_provider，但这个 Factory
仍位于 Memory 模块，并且只支持 Hash 与 BGE-M3。

### 1.4 BGE 身份不可证明

现有 BgeM3EmbeddingProvider 固定声明：

~~~text
model_name = BAAI/bge-m3
dimension = 1024
~~~

但实际权重来自任意 model_path。当前持久化信息无法表达：

- 本地权重 revision；
- tokenizer revision；
- CLS pooling；
- L2 normalization；
- max_length=8192；
- query/document instruction；
- preprocessing 版本。

因此两个都写着 BAAI/bge-m3 的向量不一定处于相同向量空间。

### 1.5 query/document 语义混用

RAG 构建使用 embed_texts()，查询使用 embed_query()，但 MemoryExtractionService
当前把长期记忆内容通过 embed_query(memory.content) 写入数据库。

当未来 BGE 或 Remote API 对 query 和 document 使用不同 instruction 时，这会把长期
记忆写入错误的编码路径。稳定资料必须使用 document purpose，用户查询必须使用 query
purpose。

### 1.6 存储只看 shape，不看模型

以下三个查询实现都没有按 embedding_model 过滤：

- raw SQLite Memory：app/db/sqlite_store.py；
- SQLAlchemy Memory：app/db/repositories/memory_embeddings.py；
- RAG SQLite：app/retrieval/vector_index.py。

当前行为是：

- shape 不同：静默跳过，表现成“没有结果”；
- shape 相同、模型不同：直接点积，产生看似正常但没有意义的分数；
- BLOB 损坏或 NaN/Inf：没有稳定隔离和错误分类；
- dot product 默认假设所有向量都已 L2 normalization，但 Port 未声明该契约。

### 1.7 RAG 增量构建不能识别模型变化

RAG chunk metadata 已保存旧版 content hash，chunk ID 也间接包含 hash。构建工具只按
chunk ID 判断是否跳过：

- 同一内容改用新模型时，ID 不变，旧向量会被错误复用；
- 内容改变会生成新 ID，但旧 chunk 不会被删除；
- --rebuild-existing 只能覆盖当前 ID，不能清理已删除资料；
- 查询前没有 index-level signature 或 ready 状态检查。

### 1.8 Memory 写入存在不可恢复的部分成功

MemoryExtractionService 当前顺序为：

~~~text
create_memory
-> embed_query(memory.content)
-> upsert_memory_embedding
-> append created_memory_id
~~~

若 Embedding 在 memory 已提交后失败：

- active memory 已存在但没有向量；
- Worker 把 extraction job 标记为 failed；
- claim 只领取 pending job；
- 相同 idempotency_key 无法创建新 job；
- 即使重新调用 extraction，has_exact_active_memory 也会直接 skip，无法取得已有
  memory_id 并补写向量。

多条 memory 场景还可能出现前几条成功、后一条失败，但 job result 中没有完整的部分
成功信息。

### 1.9 运行时边界

当前主运行时 active EvidenceProvider 是关键词型 WorldInfoEvidenceProvider。向量 Wuwa
RAG 仅作为构建和查询工具存在。本任务不能顺带把向量 RAG 接入 DialogueController。

Settings 中的 memory_vector_db_path / AEMEATH_MEMORY_VECTOR_DB_PATH 当前没有运行时
消费者。Memory vectors 实际与 Memory 数据一起存放在 settings.db_path 对应 backend
中，该配置只能作为 deprecated/reserved 字段处理，不能被误解释为已有独立向量库。

## 2. 推荐模块结构和文件范围

### 2.1 新中立模块

~~~text
app/embeddings/
  __init__.py
  ports.py
  schemas.py
  config.py
  signatures.py
  validation.py
  errors.py
  factory.py
  qualify.py
  observability.py
  profiles/
    siliconflow_pro_bge_m3_v1.json
    local_bge_m3_v1.json
    deterministic_hash_v1.json
    canaries/
      bge_m3_canary_v1.json
    baselines/
      siliconflow_pro_bge_m3_v1_canary_v1.f32le
      local_bge_m3_v1_canary_v1.f32le
  providers/
    __init__.py
    hash.py
    local_bge_m3.py
    siliconflow.py
~~~

职责：

- ports.py：唯一正式 Embedding Provider Port；
- schemas.py：请求、批结果、usage 和 model descriptor；
- config.py：EmbeddingSpaceProfile 与 EmbeddingRuntimeOptions 的加载和校验；
- signatures.py：canonical signature 和 fingerprint；
- validation.py：集中验证数量、顺序、shape、dtype、finite、dimension 和 norm；
- errors.py：Embedding 专属 typed errors；
- factory.py：唯一 Provider Factory；
- qualify.py：读取版本化 canary/baseline，执行 qualification并生成安全摘要；
- observability.py：Embedding call observer、Noop 实现和现有 trace/app-log 适配边界；
- profiles/：版本控制中的向量空间 profile、固定非敏感 canary 与坐标级 baseline artifact；
- providers/：本地 BGE、SiliconFlow API、Hash 的实现细节。

app/embeddings/ 不得 import Memory、Retrieval、Character、DB、FastAPI 或 DialogueController。
它只可依赖现有中立AppLoggerPort和错误sanitizer完成observability适配。

### 2.2 迁移期兼容文件

app/retrieval/embeddings.py 暂时保留为薄 re-export shim：

~~~text
旧 import ─┐
          ├──> app/embeddings/ 唯一实现
新 import ─┘
~~~

旧文件不能保留第二份 Port 或 Provider 实现。

旧 model_name、dimension、embed_texts() 和 embed_query() 可通过 adapter 保留一个发布
周期，但新业务代码只使用 Batch-first Port。

### 2.3 当前 Embedding Worker 的实施范围

允许修改：

- app/embeddings/*；
- app/config.py 中的 Embedding 配置解析；
- app/retrieval/embeddings.py 兼容 shim；
- app/memory/runtime.py 中只负责把旧 Factory 转发到统一 Factory 的薄兼容代码；
- pyproject.toml 的 embedding-api / embedding-local optional extras；
- .env.example；
- Provider contract、Factory、配置和兼容测试；
- qualification probe代码、fake transport和离线比较测试；真实联网只用于Profile发布门禁。

当前 Worker 必须交付：

-中立 Batch-first Port 与 schemas；
- SiliconFlow Pro/BAAI/bge-m3 Provider；
-本地 BGE-M3 Provider；
- Hash Provider；
-统一 Factory 与非敏感配置；
-客户端 normalized float32 postprocessing；
- signature、profile manifest 和坐标级 canary qualification；
- Embedding observability 契约；
-旧 import、旧方法和旧配置的单一实现兼容 shim；
- Provider contract tests；
- qualification probe代码；真实联网不进入普通实现验收或CI，而是Profile从review升级为
  approved前的发布门禁。

### 2.4 明确交给后续 Worker

Persistence Worker：

- canonical signature的数据库落点与迁移；
- content hash；
- raw SQLite 与 SQLAlchemy schema/store contract；
- legacy vector识别与迁移。

Memory Worker：

- extraction 的 document embedding 接入；
- missing/incompatible vector repair；
- keyword degradation；
- stale job recovery；
- Memory 双 backend 行为审查。

RAG Worker：

- RAG index manifest；
- staging/ready/failed；
- corpus/content hash；
- mixed-signature 拒绝与重建；
- RAG build/query 接入。

Semantic Query Worker：

-单轮 query 收集；
-批量 query embedding；
-同 signature 单轮共享；
- TurnSemanticContext 或等价的 workflow state；
-不做跨轮 Redis/SQLite/TTL/LRU cache。

当前 Worker 明确不修改：

- app/db/schema.sql、sqlite_store.py、models.py、sqlalchemy_store.py 或 repositories；
- app/memory/reader.py、extractor.py、worker.py 的业务流程；
- app/retrieval/vector_index.py、provider.py 或 RAG 构建策略；
- app/agent/dialogue_controller.py；
- Context Budget；
- Character 样例选择；
- World Info active provider。

## 3. Port 与 schema 草案

### 3.1 Batch-first Port

当前项目调用链是同步的，v0.1 推荐同步 Port。Remote Provider 在内部使用同步 HTTP
client，不为本任务引入 async runtime。

~~~python
EmbeddingPurpose = Literal["query", "document"]


@dataclass(frozen=True)
class EmbeddingModelSignature:
    schema_version: str
    provider: str
    model_id: str
    dimension: int
    model_revision: str
    pooling: str
    normalization: str
    query_instruction: str
    document_instruction: str
    preprocessing_version: str
    postprocessing_version: str
    profile_id: str
    canary_set_version: str
    expected_fingerprint: str

    def canonical(self) -> str:
        ...

    def fingerprint(self) -> str:
        ...


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: tuple[str, ...]
    purpose: EmbeddingPurpose


@dataclass(frozen=True)
class EmbeddingUsage:
    input_count: int
    input_tokens: int | None = None


@dataclass(frozen=True)
class EmbeddingCallDiagnostics:
    retry_count: int
    provider_request_id: str | None


@dataclass(frozen=True)
class EmbeddingBatchResult:
    vectors: np.ndarray
    signature: EmbeddingModelSignature
    usage: EmbeddingUsage | None = None
    diagnostics: EmbeddingCallDiagnostics | None = None


class EmbeddingProviderPort(Protocol):
    @property
    def signature(self) -> EmbeddingModelSignature:
        ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingBatchResult:
        ...
~~~

### 3.2 请求语义

- purpose=query：供后续 Semantic Query 层提交用户查询或业务查询；
- purpose=document：RAG chunk、长期记忆、future Character 稳定样例；
- Foundation 不决定某个业务是否需要 Embedding；
- Request 不携带任意业务 metadata，避免 secret 或用户上下文泄漏到 Provider；
- 输入由 caller 转换为 tuple，结果保持完全相同的顺序。

### 3.3 结果契约

vectors 固定为：

- 二维矩阵；
- shape=(input_count, signature.dimension)；
- C-contiguous；
- little-endian float32；
- 所有元素 finite；
-所有 Provider 输出统一经过客户端 postprocessing，成为 L2-normalized float32；
- SiliconFlow BGE-M3 固定 1024 维。

空 batch 返回 shape=(0, dimension)，且不访问本地模型或远程 API。空白文本、零向量、
返回数量错误、远程 index 重复/缺失、错维、NaN 和 Inf 必须抛 typed error。

usage只保存Provider明确返回的聚合计数。diagnostics只允许retry_count和经过校验的
provider_request_id；失败typed error携带同样的安全诊断字段，不能携带headers、body、
输入或向量。

### 3.4 单条兼容 helper

单条 query 可由基础 helper 实现：

~~~python
embed_one(
    provider,
    text,
    purpose="query",
) -> np.ndarray
~~~

helper 内部仍构造单元素 EmbeddingRequest。Provider Port 本身不再要求每个实现分别维护
embed_query() 和 embed_texts()。

### 3.5 单轮 query vector

用户 query vector 只属于本轮工作流，但最终调用所有权由后续 Semantic Query 层统一
定义：

-收集本轮需要的 query；
-使用 batch-first Port 统一批量生成；
-在相同 signature 与 purpose 下供本轮多个消费者共享；
-不由每个业务模块各自重复请求 SiliconFlow；
-不由 DialogueController 直接调用 Provider；
-不写 Redis、SQLite、TTL 或 LRU；
- turn 结束后销毁。

当前 Worker 不实现 TurnEmbeddingScope、TurnSemanticContext、单轮共享 key 或文本摘要。
后续 Semantic Query Worker应在不持久化 query vector的前提下独立定义本轮去重与共享
语义；该临时摘要不等同于 Persistence Worker管理的稳定资料 content hash。

## 4. Provider 与配置设计

### 4.1 Local BGE-M3 Provider

本地 Provider 固定逻辑模型 ID 为 BAAI/bge-m3、固定输出 1024 维，用于本地或离线
部署。它与 SiliconFlow Pro/BAAI/bge-m3 是不同 Provider，不因模型家族相同而共享
signature。

运行参数：

- model path；
- device；
- batch size；
- lazy/eager load；

向量空间参数不从运行环境读取，而由 local_bge_m3_v1 profile manifest 固定，包括模型
ID、1024 维、CLS pooling、pre/postprocessing version、canary set 和 expected fingerprint。

Provider 保留延迟加载能力，Foundation 导入不应强制导入 torch/transformers。实际输出
dimension 必须验证为 1024。输出统一在客户端转换为 L2-normalized float32。

本地 BGE-M3 使用 CLS pooling 已有模型配置依据，profile 中固定写 cls。若实际加载路径
无法证明模型 artifact revision，则 model_revision仍写 provider_managed，不能根据目录名
猜测 commit。device、batch size和本地绝对路径不进入 signature；加载后的输出仍须通过
profile qualification，防止路径指向不同权重却冒用同一 profile。

### 4.2 SiliconFlow Pro/BAAI/bge-m3 Provider

生产 Remote Provider 已固定：

~~~text
provider: siliconflow
model_id: Pro/BAAI/bge-m3
endpoint: https://api.siliconflow.cn/v1/embeddings
dimension: 1024
query instruction: none
output: client-normalized float32
~~~

API 请求固定为：

~~~text
POST https://api.siliconflow.cn/v1/embeddings

request:
  model: Pro/BAAI/bge-m3
  input: list[str]
  encoding_format: float

must not send:
  dimensions

response:
  data[].index
  data[].embedding
  optional usage
~~~

约束：

-固定使用批量 input；`EmbeddingRuntimeOptions.batch_size`表示Provider内部单次HTTP请求的
  最大拆批大小，而不是Port逻辑请求的数量上限；
-当逻辑请求数量大于`batch_size`时，SiliconFlow Provider按连续切片依次请求
  `/v1/embeddings`，校验每个chunk后按原输入顺序合并，不能重发已经成功的chunk，也不能
  在后续chunk失败时返回部分结果；
- 根据 response index 恢复输入顺序；
-不发送 dimensions，响应必须验证为 1024 维；
- BGE-M3 不添加 query instruction；purpose仍保留在 Port中供上层表达语义；
-输出统一在客户端转换为 L2-normalized float32；
- SiliconFlow 不提供可依赖的不可变模型 revision，因此不伪造 commit；
-未知 pooling和模型 revision均写 provider_managed；
-使用版本控制的 profile manifest、expected fingerprint与qualification baseline识别行为变化；
-不支持把 API key 放进 URL；
-不持久化或记录原始 response body。

API key 只从固定环境变量 SILICONFLOW_API_KEY 读取；不提供可配置的 key 环境变量名称。
测试可向 Factory 注入内存 secret resolver，但生产 resolver始终读取该固定名称。实际 key
不进入 Settings repr、Provider repr、signature、异常、日志、job result、文档或测试输出。

### 4.3 Deterministic Hash Provider

Hash Provider 用途：

- unit tests；
- Mock；
-显式开发模式。

规则：

-算法版本固定并进入 deterministic_hash_v1 profile；
- dimension 进入 signature；
-不接受 query/document instruction；
-不得作为 SiliconFlow/本地 BGE runtime failure 的 fallback；
-只有使用相同 Hash signature 构建的索引才能由 Hash query 查询。

建议 canonical 配置同时要求：

~~~text
profile=deterministic_hash_v1
allow_hash=true
~~~

`deterministic_hash_v1`提交确定性的little-endian float32坐标baseline并标记为`approved`，
用于canonical Factory、artifact loader和qualification的完全离线smoke。批准仅表示这一固定
Hash向量空间的manifest与baseline完整，不改变其“只用于测试和显式开发”的安全边界；
SiliconFlow与本地BGE Profile仍保持`review`。

### 4.4 配置分层与版本化 profile

配置必须拆成两个对象，禁止用同一个可变 Settings 同时表达向量空间身份和运行参数。

#### 4.4.1 EmbeddingSpaceProfile

EmbeddingSpaceProfile 影响 signature，由版本控制中的只读 manifest 定义。生产
SiliconFlow profile 固定路径：

~~~text
app/embeddings/profiles/siliconflow_pro_bge_m3_v1.json
~~~

manifest 至少包含：

~~~json
{
  "schema_version": "embedding-space-profile/v1",
  "release_status": "review",
  "profile_id": "siliconflow_pro_bge_m3_v1",
  "provider": "siliconflow",
  "model_id": "Pro/BAAI/bge-m3",
  "dimension": 1024,
  "model_revision": "provider_managed",
  "pooling": "provider_managed",
  "normalization": "l2",
  "query_instruction": "",
  "document_instruction": "",
  "preprocessing_version": "aemeath-embedding-pre-v1",
  "postprocessing_version": "normalized-float32-v1",
  "canary_set_version": "bge_m3_canary_v1",
  "canary_artifact": "canaries/bge_m3_canary_v1.json",
  "canary_artifact_sha256": "sha256:<artifact digest>",
  "baseline_artifact": null,
  "baseline_artifact_sha256": null,
  "fingerprint_algorithm": "baseline-relative-mask-v1",
  "expected_fingerprint": null,
  "qualification_thresholds": null
}
~~~

这些字段不能由环境变量或普通CLI参数逐项覆盖。profile_id只映射profiles根目录内的
allowlist manifest，不接受任意文件路径；manifest中的相对artifact路径必须拒绝绝对路径
和path traversal。loader校验artifact SHA-256、canary顺序、baseline字节数、shape、
little-endian float32、finite和L2 norm。release_status只有review和approved；普通运行时
Factory只接受approved，qualification专用入口可以加载review。

修改任何影响向量空间的字段、canary、baseline、阈值或fingerprint algorithm都必须创建
新的profile_id、执行qualification并通过代码审查，不能原地重写已发布v1。review
profile可以暂缺baseline、阈值和expected_fingerprint；approved profile三者必须完整，且
expected_fingerprint必须由已提交baseline重算一致。profile manifest与baseline artifact
都不得包含API key；固定canary文本必须是非用户、非敏感内容。

本地BGE和显式开发Hash也使用独立manifest。canonical provider ID固定为siliconflow、
local_bge_m3和deterministic_hash。本地profile固定pooling=cls；未知artifact revision仍
诚实记录provider_managed。三个profile ID永不互相复用。

#### 4.4.2 EmbeddingRuntimeOptions

EmbeddingRuntimeOptions 不影响 signature，只包含：

- batch size（Provider内部单次HTTP/模型推理chunk的最大条数，不限制Port逻辑batch）；
- timeout；
- retry count/backoff；
-本地 model path；
-本地 device；
- lazy/eager load；
-显式 Hash 安全开关。

Foundation原子接入后，环境变量只负责选择profile、提供固定名称的secret和设置运行参数：

~~~text
# 只能选择 release_status=approved 的 profile
AEMEATH_EMBEDDING_PROFILE=<approved_profile_id>
SILICONFLOW_API_KEY=
AEMEATH_EMBEDDING_BATCH_SIZE=16
AEMEATH_EMBEDDING_TIMEOUT_SECONDS=30
AEMEATH_EMBEDDING_MAX_RETRIES=2
AEMEATH_EMBEDDING_RETRY_BACKOFF_SECONDS=0.25
AEMEATH_EMBEDDING_ALLOW_HASH=false

AEMEATH_EMBEDDING_LOCAL_MODEL_PATH=.cache/huggingface/BAAI/bge-m3
AEMEATH_EMBEDDING_LOCAL_DEVICE=auto
AEMEATH_EMBEDDING_LOCAL_MAX_LENGTH=8192
AEMEATH_EMBEDDING_LAZY_LOAD=true
~~~

切换本地部署时只把 profile 选择改为 local_bge_m3_v1。Provider、model ID、dimension、
pooling、normalization、instruction、pre/postprocessing version、canary set 与 expected
fingerprint均从该 profile加载，不允许服务器自行宣称版本。SiliconFlow API key只从
SILICONFLOW_API_KEY读取，不保留可配置的API-key环境变量名称。

在当前兼容阶段，.env.example继续启用旧AEMEATH_MEMORY_EMBEDDING_*变量；上述Foundation
变量仅以注释示例存在。siliconflow_pro_bge_m3_v1在真实baseline capture、人工审查和真实
qualification全部完成前保持review，不能成为.env.example默认Profile。

Factory 不因缺少 profile而隐式创建 Hash。某个业务请求创建 Provider但没有有效 profile
时返回 typed configuration error；是否因此让 Memory 或其他业务进程 fail fast，交给对应
Worker 后续设计。

以下字段仍归 Memory 或 RAG，不进入 EmbeddingRuntimeOptions：memory recall top-k、recall
threshold、worker poll interval、memory window size、RAG DB path、RAG write batch size和
Retrieval policy。

### 4.5 Factory

~~~python
profile = load_embedding_space_profile(profile_id)

create_embedding_provider(
    profile: EmbeddingSpaceProfile,
    runtime: EmbeddingRuntimeOptions,
    *,
    secret_resolver: SecretResolver = environment_secret_resolver,
    app_logger: AppLoggerPort | None = None,
) -> EmbeddingProviderPort
~~~

Factory：

-每次调用创建独立 Provider/client；
-严格校验版本化 profile，并从 profile派生 Provider与signature；普通Factory不能只检查调用方
  对象中的`release_status`字符串，而要按`profile_id`重新加载版本控制中的canonical manifest、
  canary、baseline及其digest/fingerprint，确认其完整且为`approved`，再要求调用方对象与
  canonical Profile逐字段相等，并只使用重新加载的对象创建Provider；
-不维护跨进程 singleton；
-不读取 Memory/RAG 业务配置；
-不提供隐式 Provider fallback；
- SiliconFlow secret resolver只允许读取固定名称SILICONFLOW_API_KEY；
-静态配置错误在创建时 fail fast；
-本地模型/HTTP client 的实际初始化可按 Provider 生命周期延迟进行；
-通过 provider-agnostic wrapper提供AppLoggerPort注入能力，默认使用NoopAppLogger。

本阶段只实现并测试ObservableEmbeddingProvider能力契约。当前main.py创建Memory Provider
早于AppLogger，且本Worker不修改main.py，因此CLI、API、Memory Worker和RAG工具实际传入
Logger属于各入口后续集成任务，不能把“wrapper已实现”等同于“运行时trace已接通”。

配置优先级：

~~~text
profile selection:
  显式 --embedding-profile
    > AEMEATH_EMBEDDING_PROFILE
    > legacy Memory provider 到 profile_id 的兼容映射

runtime options:
  显式 CLI 参数
    > canonical AEMEATH_EMBEDDING_* 环境变量
    > legacy Memory 环境变量
    > Provider 默认运行参数
~~~

profile manifest内容不参与优先级合并，任何入口都不能覆盖其中的向量空间字段。新旧配置
同时存在且冲突时 fail fast，不能静默选一边。

### 4.6 Embedding observability 契约

Embedding Foundation统一复用现有AppLoggerPort作为跨CLI、API、Worker、RAG build和
qualification都可用的落点，不直接写需要完整turn身份的TraceEvent，也不写Prompt Run
Log。Factory用ObservableEmbeddingProvider装饰实际Provider；Provider Port本身保持纯净。

每个逻辑batch在所有retry结束后只记录一条终态事件：

-成功：embedding.batch_completed；
-失败：embedding.batch_failed；
- qualification另用embedding.qualification_completed或embedding.qualification_failed。

Qualification CLI的每条命令也只写一条终态事件：probe返回`passed=false`、加载/创建/调用
异常均记录`embedding.qualification_failed`；成功记录
`embedding.qualification_completed`。其payload单独固定为`command`、`provider`、
`profile_id`、`release_status`、`dimension`、`canary_count`、`observation_count`、
`signature_fingerprint`和`duration_ms`，失败错误码写在AppLogRecord顶层。不得记录canary
原文、坐标、baseline路径/digest、candidate输出路径或secret；Logger自身失败不能改变probe
结果或CLI退出码。

固定 allowlist 字段：

~~~text
provider
profile_id
purpose
batch_size
input_tokens
duration_ms
retry_count
provider_request_id
error_code
~~~

语义：

- duration_ms使用单调时钟，覆盖全部attempt、backoff和response validation；
- retry_count是首次尝试之外的额外尝试次数；
- input_tokens只使用Provider响应usage，未知时为null，不在本地猜测；
- provider_request_id仅从SiliconFlow明确allowlist的trace/request-id响应字段提取，限制
  类型、字符集和长度，不复制完整headers；本地BGE/Hash为null；
- error_code写入AppLogRecord.error_code，其余字段进入固定payload；成功时为null，最终
  失败记录稳定EMBEDDING_*错误码；
- event_name使用上述completed/failed事件名，level成功为info、失败为error；event_name、
  level和error_code使用AppLogRecord顶层字段，其余allowlist字段进入payload；
- EmbeddingBatchResult.diagnostics和typed error只携带retry_count与安全的
  provider_request_id，供统一wrapper生成终态事件。

禁止记录原文、文本hash、完整向量、坐标级canary baseline、API key、Authorization、
完整headers、原始response body或异常原文。写入继续经过app.errors.sanitizer与
JsonlAppLogger二次脱敏。后续Semantic Query Worker如需turn trace，只能对共享后的一个
逻辑batch补充一条事件，不能让每个消费者重复记录Provider调用。

## 5. 本地、API Server、Memory Worker 目标拓扑

本节定义后续接入目标，不授权当前 Embedding Worker 修改 CLI/API/Memory/RAG业务流程。

### 5.1 本地 CLI

~~~text
Settings.from_env()
  -> profile_id + EmbeddingRuntimeOptions
  -> load EmbeddingSpaceProfile
  -> create_embedding_provider()
  -> future Semantic Query / Memory integration
~~~

当前 World Info 关键词检索不调用 Embedding。未来正常用户消息的 query由 Semantic Query
层统一批量生成并在本轮共享，而不是由 Controller或每个模块分别请求 API。

### 5.2 FastAPI Server

~~~text
create_app()
  -> Settings.from_env() / explicit Settings
  -> API-specific prompt-log/model-fallback replace
  -> build_agent_service()
  -> same Embedding Factory
~~~

每个 Uvicorn worker 进程独立创建 Provider：

-本地 BGE 每个进程独立加载权重；
- SiliconFlow每个进程独立创建 HTTP client；
-不按 HTTP request 创建 Provider；
-不共享 Python client/model 对象。

FastAPI 与 CLI 使用相同 Embedding 配置语义，不增加 API 专属向量空间配置。API key仍只
通过固定名称SILICONFLOW_API_KEY获取。

### 5.3 Memory Worker

~~~text
Settings.from_env()
  -> same profile_id + EmbeddingRuntimeOptions
  -> same EmbeddingSpaceProfile loader
  -> same Factory
  -> worker-local Provider
  -> future Memory document embedding / repair
~~~

后续 Memory Worker 与主进程不共享 Provider 实例。Worker在 claim job前可以初始化本地
模型，但 SiliconFlow Provider不发送计费型“预热文本”。Memory业务接入和 repair由
Memory Worker负责，不属于当前 Worker。

Foundation只保证Provider创建/调用失败返回稳定错误码且不输出secret。pending job如何
处理属于后续Memory Worker待审设计，本节不冻结其状态。

### 5.4 RAG build/query 工具

后续 RAG build、单次 query 和 interactive CLI均通过统一 config + Factory创建 Provider。
旧 --embedding、--model-path、--device等参数只作为 config overlay，具体接入由 RAG
Worker负责。

RAG builder 使用 document purpose；RAG query 使用 query purpose。工具进程退出后 query
vector 被释放，不写任何 query cache。

## 6. Foundation signature 与后续持久化待审提案

### 6.1 Signature 定义

Signature 是向量空间的“身份证”，不是密码学业务签名。它描述一批向量由哪套完整规则
生成。

EmbeddingModelSignature.canonical() 推荐使用：

- schema version；
-字段排序固定；
-字符串和换行规范化；
-无多余空格；
- UTF-8 canonical JSON；
-不包含 API key、Authorization、cookie 或完整敏感 URL。

至少包含：

- schema version；
- provider；
- model ID；
- dimension；
- model revision；
- pooling；
- normalization；
- query/document instruction；
- preprocessing version；
- postprocessing version；
- profile_id；
- canary_set_version；
- profile manifest中的expected_fingerprint。

SiliconFlow signature固定：

~~~text
provider = siliconflow
model_id = Pro/BAAI/bge-m3
dimension = 1024
model_revision = provider_managed
pooling = provider_managed
normalization = l2
query_instruction = ""
document_instruction = ""
preprocessing_version = project-managed
postprocessing_version = normalized-float32 project-managed
profile_id = siliconflow_pro_bge_m3_v1
canary_set_version = bge_m3_canary_v1
expected_fingerprint = approved profile manifest value
~~~

SiliconFlow不提供项目可依赖的不可变模型 revision，且API不公开可验证 pooling，因此不能
把未知值写成假定的 commit或 cls。provider_managed表示该项由服务端管理；版本化
profile、坐标级baseline与expected fingerprint负责识别项目可观察到的行为变化。

本地 BGE-M3使用不同provider值和独立profile/baseline，pooling固定为cls。只有实际
artifact revision可证明时才写具体revision，否则model_revision仍写provider_managed。

Foundation只定义canonical signature及其SHA-256 signature fingerprint。如何持久化、是否
复用现有embedding_model字段，由后续Persistence Worker另行设计和审批；当前文档不冻结
数据库落点。

以下摘要不得混名：baseline_artifact_sha256只验证artifact文件完整性；
expected_fingerprint是profile批准的行为基准；observed_fingerprint只属于本次probe结果；
signature_fingerprint是canonical signature的SHA-256。observed_fingerprint不进入正常
Provider signature或持久化向量元数据。

### 6.2 后续 Persistence 待审：Content hash

本节只是交给Persistence Worker评估的候选，不是已批准契约。当前Embedding Worker不增加
数据库列、不修改store，也不负责旧向量迁移。

候选算法：

~~~text
sha256(
  "aemeath-embedding-content:v1\0"
  + canonical_text_utf8
)
~~~

canonical text 至少统一 Unicode normalization 与换行。v0.1的BGE query/document
instruction均为空。未来若引入非空instruction，必须先把它纳入signature兼容语义（例如
新增字段或创建新的preprocessing version/profile_id），但仍不把它混入逐条content hash。

区别：

~~~text
content hash：嵌入了什么内容
signature：使用什么模型和规则嵌入
dimension：物理向量长度
vector：最终数值
~~~

候选方案是RAG在metadata保存完整SHA-256，并评估给long_term_memory_embeddings增加
nullable content_hash；列结构、新写入要求和旧NULL语义均未批准。

不应把 content hash 塞入 signature，因为它是逐条内容属性，会让同一向量空间的每行都
产生不同 signature。

### 6.3 后续 Persistence 待审：BLOB 协议

候选的新写入约束：

-一维；
- little-endian float32；
- C-contiguous；
- len(blob) == dimension * 4；
- finite；
-候选持久化协议要求L2 normalized。

候选校验要求vector_dim同时等于：

- Provider signature.dimension；
-实际 ndarray dimension；
- BLOB 解码后的元素数量。

### 6.4 后续 Persistence 待审：复用、拒绝、重建矩阵

以下矩阵只用于暴露风险和形成后续讨论输入，不是当前Worker验收契约：

| signature | dimension | content hash / BLOB | 行为 |
|---|---:|---|---|
| 相同 | 相同 | hash 相同且 BLOB 合法 | 复用 |
| 相同 | 相同 | hash 缺失或不同 | stale，重建 |
| 不同 | 相同 | 任意 | 禁止使用，离线/后台重建 |
| 任意 | 不同 | 任意 | 不兼容或损坏，拒绝并重建 |
| 相同 | 相同 | BLOB 非法、NaN、Inf | 数据损坏，隔离并重建 |
| legacy unknown | 任意 | 任意 | 默认拒绝并重建 |

查询不得把不兼容行伪装成“无结果”。存储层应返回稳定 compatibility/corruption
诊断，由业务模块决定降级。

### 6.5 后续 Memory/RAG 待审：不同严格度

以下是此前讨论形成的候选，不代表Manager批准：

Memory 是持续增长数据：

-查询只允许 exact signature；
-缺失、不兼容或损坏向量进入 repair；
- coverage不完整时，可选策略包括整轮关键词降级、只查明确兼容子集并披露coverage，或
  显式失败；最终策略由Memory Worker设计和复审；
-任何策略都不能把兼容子集静默伪装成完整Memory索引。

RAG 是稳定 corpus：

-引入 index manifest；
-记录 signature、dimension、corpus fingerprint、build state；
- build state 只有 building、ready、failed；
-查询只接受 ready 且 signature 完全匹配的索引；
- mixed signature 整体拒绝；
-模型变更默认全量重建。

### 6.6 后续 RAG 待审：构建和 stale 清理

候选的完整同步构建：

~~~text
旧 ready index
  保持可用

新 staging index
  构建全部 chunks
  验证数量、signature、dimension、content hash

验证通过
  标记/替换为 ready
~~~

候选方案要求构建失败不污染旧ready index；完整同步会删除新corpus中已经不存在的旧
chunk，append-only则需要显式选择。具体状态切换和删除语义由RAG Worker复审。

同一 signature 的中断构建可以续跑；signature 改变时不能沿用“ID 已存在就跳过”的逻辑。

### 6.7 后续 Persistence/RAG 待审：Legacy vectors

以下兼容行为由后续迁移设计决定：

旧 Hash：

-算法名带 deterministic_hash_v1；
-实现、tokenization、lowercase、SHA-1 bucket、sign 和 normalization 已知；
- dimension 可由 vector_dim 获取；
-可以映射为 canonical legacy Hash signature；
-也可以低成本重建。

旧 BGE：

-只有裸 BAAI/bge-m3；
- model_path 可指向任意本地权重；
- revision 不可知；
-候选默认行为是视为不可证明兼容并重建。

可以设计显式 trust-legacy 工具供人工承担风险，但不得成为默认启动行为。

## 7. 失败、重试、降级与幂等策略

### 7.1 Typed errors

建议新增独立错误分类：

- EMBEDDING_INVALID_INPUT；
- EMBEDDING_CONFIG_INVALID；
- EMBEDDING_SECRET_MISSING；
- EMBEDDING_DEPENDENCY_MISSING；
- EMBEDDING_REQUEST_REJECTED；
- EMBEDDING_AUTH_FAILED；
- EMBEDDING_RATE_LIMITED；
- EMBEDDING_TIMEOUT；
- EMBEDDING_UNAVAILABLE；
- EMBEDDING_INVALID_RESPONSE；
- EMBEDDING_SIGNATURE_MISMATCH；
- EMBEDDING_VECTOR_CORRUPT；
- EMBEDDING_QUALIFICATION_FAILED。

错误应携带：

- retryable；
- provider；
- profile_id；
- signature fingerprint；
- retry_count；
-经过类型、字符集和长度校验的provider_request_id；
-公开安全消息；
-经过 sanitizer 的 internal message。

错误不得携带：

- API key；
- Authorization header；
- cookie；
-完整headers；
-完整 Remote response body；
-未经sanitizer处理的异常原文；
-用户输入全文；
-完整 vector。

不建议复用 MODEL_*，因为 Chat Model 和 Embedding 的降级、持久化和重试语义不同。

### 7.2 SiliconFlow API 重试

Provider 内只重试：

- timeout；
-连接中断；
- 429；
-明确可重试的 5xx。

规则：

-有限次数指数退避；
-尊重 Retry-After；
-认证失败、普通 4xx、非法 response、错维不重试；
-请求 batch 在返回完整并通过验证前不写入持久化；
-不在错误中保存 response body。

Local BGE 的缺依赖、模型路径错误、manifest声明具体revision但实际artifact不匹配，以及
稳定加载错误不进行无意义的逐条重试；manifest明确写provider_managed不属于revision
缺失错误。

### 7.3 后续业务降级待审矩阵

本矩阵是Memory/RAG/Character后续接入时的候选讨论输入，不属于当前Embedding Worker
的实现范围，也不预先冻结各业务的降级策略。

| 场景 | 业务行为 |
|---|---|
| RAG 构建 Provider 失败 | 终止当前构建，manifest=failed，保留旧 ready index |
| RAG CLI query 失败或不兼容 | 显式报错并返回非零状态，不切 Hash |
| future active vector RAG 失败 | 返回空 EvidencePack 或独立关键词结果；本轮不接入 |
| Memory query Provider 失败 | 由Memory Worker在关键词降级、显式失败等候选中决定 |
| Memory coverage 不完整 | 由Memory Worker决定兼容子集、整轮降级或失败，并独立设计repair |
| Memory document embedding 失败 | 保留 memory，记录 pending，稍后 repair |
| future Character index 失败 | 禁用语义选择或保留已验证旧 index；本轮不实现 |

Hash 只有在 operator 显式选择 Hash 且使用同一 Hash signature 构建/查询索引时才可使用。

### 7.4 Memory extraction 待审提案（交给 Memory Worker）

候选流程：

~~~text
1. LLM 一次提炼出全部 memories
2. 对每条 exact memory 执行 create-or-get，取得稳定 memory_id
3. 收集缺失、stale 或 incompatible 的 memory records
4. 一次 document-purpose batch embedding
5. 严格验证完整 batch
6. 按 memory_id 幂等 upsert vectors
7. job 记录 created/reused/embedded/pending ids
~~~

以下job状态和result_json只是一种候选，Memory Worker复审可以改用不同状态机；当前文档
不批准“使用现有completed状态”这一语义：

~~~json
{
  "created_memory_ids": [],
  "reused_memory_ids": [],
  "embedded_memory_ids": [],
  "embedding_pending_memory_ids": [],
  "embedding_error_code": null
}
~~~

这样不会为修复 Embedding 重跑 LLM。failed 状态继续用于提炼模型失败、非法输出、来源
校验失败或 canonical memory 本身无法持久化。

如果 Manager 要求 UI/管理工具显式显示 partial 状态，可以另行批准
completed_with_pending_embedding；v0.1 推荐先使用 completed + result metadata，避免
扩展 job status schema。

### 7.5 MemoryEmbeddingRepairService 待审提案（交给 Memory Worker）

Repair 的事实来源是数据库，而不是 job result：

~~~text
active memories
LEFT JOIN long_term_memory_embeddings
WHERE
  embedding missing
  OR signature mismatch
  OR dimension mismatch
  OR content_hash mismatch
  OR BLOB invalid
~~~

Repair：

-按分页和 bounded batch 扫描；
-使用当前统一 Factory 的 Provider；
-使用 purpose=document；
-重新计算 memory content hash；
-按 memory_id upsert；
-重复执行为 no-op；
- Provider 继续不可用时保留 pending；
-不创建新 memory；
-不调用 extraction LLM。

Memory Worker 可以在启动后和空闲轮询时执行有限数量 repair，避免全量扫描阻塞正常 job。
若未来允许多个 Worker 并发，重复计算可能浪费远程调用，但 memory_id upsert 保证数据
正确；是否增加专用 repair lease/job 可在确有多 Worker 需求时另行设计。

### 7.6 Worker crash 和 stale job 待审提案（交给 Memory Worker）

当前 running job 没有 lease recovery。Worker 在 Embedding 阶段崩溃会令 job 永久
running，且 window messages 永远被视为 covered。

后续 Memory Worker 实施阶段应补充最小 stale-running 恢复：

- locked_at 超过配置阈值；
- attempt_count 未超过上限；
-重置为 pending 或标记可重试；
- canonical memory 已存在时不重新提炼，而由 repair sweep 补向量。

job 完成记录丢失也不影响 repair，因为数据库 LEFT JOIN 能独立发现缺失 vector。

## 8. 分阶段迁移方案

### Phase 0：Worker 修订设计

本次修订纳入以下已确认的Foundation方向：

-生产 Provider为 SiliconFlow Pro/BAAI/bge-m3；
-本地 BGE-M3保留且属于不同向量空间；
- Hash只用于测试和显式开发；
-固定批量 /v1/embeddings；
-固定 1024维且不发送 dimensions；
-不添加 BGE query instruction；
-客户端统一 normalized float32；
- signature使用版本化profile_id + expected fingerprint；
-未知 pooling/model commit写 provider_managed；
-当前 Worker范围收紧。

### Phase 1：Manager 复审接口与范围

Manager复审：

- Batch-first Port/schema；
- SiliconFlow固定请求契约；
- signature字段与canonical格式；
- profile manifest、baseline artifact与expected/observed fingerprint治理；
-配置和secret边界；
- Embedding observability落点与字段；
- qualification probe；
-当前 Worker文件范围；
- Persistence、Memory、RAG、Semantic Query四个后续任务的输入契约。

复审通过前不实现 Provider。

### Phase 2：当前 Worker实现 Embedding Foundation

仅实现：

- app/embeddings/中立模块；
- SiliconFlow Provider；
-本地 BGE-M3 Provider；
- Hash Provider；
-客户端 normalized float32验证；
-版本化profile/canary/baseline artifacts与严格loader；
- EmbeddingSpaceProfile/EmbeddingRuntimeOptions分层、统一Factory；
- ObservableEmbeddingProvider与AppLoggerPort契约；
- optional embedding-api / embedding-local extras；
-旧 import/method/config兼容 shim；
- Provider contract tests；
- qualification probe代码和离线测试；有真实key时运行联网smoke。

本阶段不修改 DB、Memory reader/extractor/worker、RAG vector store/provider或
DialogueController。

### Phase 3：Persistence Worker设计复审后接入

先提交并审批存储/迁移设计，再单独实施和审查：

- canonical signature落库；
- content hash；
- vector BLOB协议；
- raw SQLite migration；
- SQLAlchemy fresh schema与兼容失败；
- legacy vector识别；
-双 backend contract。

### Phase 4：Memory Worker设计复审后接入

先提交并审批coverage/repair/job设计，再单独实施和审查：

- document-purpose batch embedding；
- pending/missing/incompatible repair；
-关键词降级；
- extraction幂等；
- stale-running恢复；
- Memory Worker独立 Provider实例。

### Phase 5：RAG Worker设计复审后接入

先提交并审批manifest/staging/rebuild设计，再单独实施和审查：

- document indexing；
- index manifest；
- staging/ready/failed；
- mixed-signature拒绝；
- corpus/content hash；
- legacy rebuild。

### Phase 6：Semantic Query / TurnSemanticContext

Foundation稳定后再设计：

-本轮 query统一收集；
- batch生成；
-相同 signature单轮共享；
-消费者访问边界；
- turn结束销毁；
-不做跨轮缓存。

### Phase 7：兼容层移除

一个发布周期后再移除旧 import、旧单条方法和旧 Memory配置 alias。内部实现始终只有
app/embeddings/一份；legacy DB解析由 Persistence路线决定。

## 9. 测试与验收计划

### 9.1 Provider contract tests

所有 Provider 共享：

- batch 数量和顺序；
- shape=(N,D)；
- little-endian float32；
- dimension；
- finite；
- L2 normalization；
-空 batch；
-空白文本；
-零向量；
- query/document purpose；
- signature canonical/fingerprint 稳定性；
- profile manifest schema、路径和不可覆盖字段校验；
- ObservableEmbeddingProvider成功/最终失败各只写一条AppLog；
- retry_count、provider_request_id和EMBEDDING_* error_code正确；
- AppLog不包含输入、文本hash、向量、key、headers、body或异常原文。

Hash：

-算法确定性；
- dimension 改变时 signature 改变；
-不同进程输出一致；
-未显式 allow_hash 时 Factory 拒绝。

Local BGE：

- lazy load；
-内部 batching；
- tokenizer/model fake；
- CLS pooling；
- max length；
-实际输出 dimension 校验；
-缺 torch/transformers 映射为 dependency missing；
- CI 不下载真实模型；
-真实本地权重仅作为可选 smoke。

SiliconFlow：

- fake HTTP transport；
-固定请求 URL为 /v1/embeddings；
-固定 model=Pro/BAAI/bge-m3；
- input必须为批量数组；
-请求体不得包含 dimensions；
- response index 乱序后恢复；
-响应必须为 1024维；
-输出统一转为 normalized float32；
-缺项、重复项、错维、NaN/Inf；
- timeout、429、5xx 重试；
- auth/4xx 不重试；
- usage 映射；
- `batch_size`只作为Provider内部拆批上限，覆盖`N > batch_size`、各chunk局部乱序恢复、
  全局原顺序合并、usage/retry累计、缺失usage整体为unknown、后续chunk失败不返回部分结果；
-多chunk diagnostics保留逐请求的安全request id序列；旧的单值
  `provider_request_id`仅在恰好一个HTTP chunk时填写；
- model_revision/pooling固定记录 provider_managed；
- API key 不出现在 repr、异常、stdout、日志或 snapshot。

### 9.2 配置和拓扑 tests

-同一profile在不同进程中生成完全相同的canonical signature；
-每次 Factory 调用得到独立 Provider/client；
- canonical/legacy 配置冲突 fail fast；
-旧配置 alias 行为；
- SiliconFlow与本地 BGE signature必定不同；
- runtime options变化不改变signature；
- profile字段不能被env/CLI逐项覆盖；
-固定1024维、空instruction、profile_id和expected fingerprint校验；
-缺失或非法expected fingerprint时profile loader fail fast；
-只从固定名称SILICONFLOW_API_KEY读取secret；
- memory_vector_db_path 不被错误消费。
-普通Factory拒绝伪造`approved`以及任何与canonical manifest不一致的Profile，并确保拒绝发生在
  secret解析、HTTP client或本地模型初始化之前；
- `deterministic_hash_v1`的已提交baseline能由Provider逐字节重算、离线qualification通过，
  且canonical Factory不访问网络或secret；SiliconFlow与本地BGE manifest仍为`review`且无
  baseline。

### 9.3 Qualification probe（代码/离线测试必交付，真实联网是Profile发布门禁）

Qualification probe代码、fake transport测试、baseline读取和比较逻辑是当前Worker必交付
内容。真实SiliconFlow联网运行不进入普通pytest、不在应用启动时自动调用，也不得要求CI
持有真实key。实现任务可以在Profile仍为review时完成，但siliconflow_pro_bge_m3_v1要成为
正式可选Profile，必须至少完成一次显式真实API baseline capture、人工审查，并用审定后的
baseline和阈值通过真实qualification；随后才可单独将release_status改为approved。

当前`deterministic_hash_v1`使用版本化非敏感canary和确定性离线baseline完成同一套门禁并
标记为`approved`，作为无网络canonical Factory smoke；这不构成SiliconFlow baseline，
也不能替代SiliconFlow正式启用前的真实capture、人工审查与verify。

建议入口：

~~~text
python -m app.embeddings.qualify capture-candidate --profile siliconflow_pro_bge_m3_v1
python -m app.embeddings.qualify verify --profile siliconflow_pro_bge_m3_v1
~~~

Probe从版本化profile读取固定、非用户、非敏感canary set和坐标级baseline artifact，通过
固定环境变量SILICONFLOW_API_KEY读取secret，验证：

- /v1/embeddings连接和认证；
- Pro/BAAI/bge-m3可用；
-批量 input顺序；
-每条向量维度为 1024；
-客户端转换后dtype=float32且L2 norm在容差内为1；
-向量finite且非零；
-相同canary重复调用的稳定性；
- batched与单条调用的对应关系；
-延迟、批大小和重试次数；
-认证/非法请求等错误能映射为稳定错误码。

兼容性判断必须逐条对比本次observed vector与版本化baseline中的对应坐标，不能只比较
canary之间的内部余弦矩阵。内部相似度矩阵可以作为诊断，但共同正交旋转会保持该矩阵却
破坏新query对旧document向量的检索，因此不能作为通过依据。

baseline artifact按固定canary ID顺序保存row-major、little-endian、L2-normalized
float32坐标，不进入日志。为容忍已测量的远端微小数值抖动，同时保持坐标级检测，v0.1
使用版本化baseline-relative-mask-v1算法：

~~~text
B = committed baseline, shape=(N, 1024)
X = current postprocessed observation, shape=(N, 1024)
t = profile.max_abs_coordinate_delta

mask[i,j] = 0  if abs(float64(X[i,j]) - float64(B[i,j])) <= t
            = 1  otherwise
~~~

按固定domain separator、algorithm ID、profile_id、canary artifact SHA-256、baseline artifact
SHA-256、shape、threshold的canonical表示和row-major packed mask计算SHA-256：

~~~text
expected_fingerprint
  = 对全零mask计算的profile批准digest

observed_fingerprint
  = 对本次实际mask计算的digest
~~~

每条canary同时计算：

- cosine(observed, baseline)；
- max_abs_coordinate_delta(observed, baseline)；
- baseline-relative mask的整体observed_fingerprint。

只有以下条件全部成立才通过：

~~~text
expected_fingerprint == observed_fingerprint
AND every cosine >= profile.min_cosine_per_vector
AND every max_abs_coordinate_delta <= profile.max_abs_coordinate_delta
~~~

重复调用以及single/batch对照产生的每一组observation都必须独立满足上述条件，不允许先
平均多次结果再掩盖单次越界。

阈值内扰动保持全零mask，因此expected/observed仍可精确相等；任一坐标越界都会改变mask
和digest。供应商发生统一正交旋转时，逐坐标差异与digest都会失败。threshold必须通过
多次candidate capture统计后人工审查并固化；verify时不得自适应放宽，也不得因为余弦
“看起来接近”就接受digest不一致。

Probe输出只包含：

- pass/fail；
- provider/model/profile_id；
- signature fingerprint；
- expected_fingerprint与observed_fingerprint；
-逐canary cosine/max coordinate delta的安全数值摘要；
- dimension/norm摘要；
- batch size；
-延迟摘要；
-错误码。

Probe不得输出：

- API key或Authorization；
-完整请求headers；
-原始响应body；
-完整向量、原始坐标或coordinate mask；
- baseline artifact内容；
-用户数据。

Probe失败返回非零状态，绝不修改.env、profile或baseline。生成新baseline只能通过显式
maintainer命令写入独立candidate路径，由人工审查后以新的profile_id提交版本控制；不得
覆盖已批准artifact，也不得把baseline向量输出到stdout或App Log。

无需真实key的确定性测试至少覆盖：阈值内扰动保持fingerprint一致；单坐标越界令
fingerprint不同；batch行置换失败；canary/baseline artifact hash、shape、norm或路径
traversal失败；给所有baseline向量施加同一正交旋转时内部余弦矩阵近似不变，但坐标级
qualification必须失败。

### 9.4 后续 Persistence Worker 待审测试提案：双 backend Store contract

以下测试清单只有在Persistence设计获批后才转成实施验收；候选继续使用
tests/store_contract参数化fixture，对SQLiteStore与SqlAlchemyStore同时验证：

- signature + dimension + content hash 全匹配才复用；
-同维不同 signature 拒绝；
-同 signature 不同维拒绝；
- content hash stale；
- missing vector；
-非法 BLOB 字节数；
- NaN/Inf；
- cross-user/orphan upsert 拒绝；
- created_at 保留、updated_at 更新；
- scope visibility 不回归；
- repair upsert 幂等；
-兼容搜索结果一致。

现有 shape mismatch 被静默 skip 的测试应改为显式 compatibility/corruption 契约。

### 9.5 后续 Memory Worker 待审测试提案：extraction/repair

以下用例需由Memory设计复审后确认；当前候选覆盖：

1. memory 已提交后 Provider 失败，memory 保留并进入 pending；
2. Provider 恢复后 repair 补写同一 memory_id；
3. 重复 repair 不新增 memory、不改变已兼容 vector；
4. exact memory 已存在但 vector 缺失时能够 repair；
5. 多 memory batch 中存储部分提交后崩溃可以恢复；
6. job complete 写入失败时 sweep 仍可发现缺失 vector；
7. running job stale lock恢复；
8.相同 extraction idempotency_key 不重复提炼；
9. query Provider failure时按Memory Worker批准的显式降级或失败策略执行；
10. coverage不完整时按Memory Worker批准的策略处理，且不静默伪装完整coverage；
11.降级不自动创建 Hash Provider；
12. worker error/job result 不包含 secret 或用户文本。

### 9.6 后续 RAG Worker 待审测试提案

- signature 改变时不能按相同 chunk ID跳过；
-同一 signature 中断后可续建；
- mixed signature index拒绝查询；
- manifest 非 ready 拒绝查询；
-完整 corpus rebuild 后切换 ready；
-构建失败保留旧 ready；
- source 内容删除/修改后的 stale row清理；
- legacy Hash mapping；
- legacy BGE默认拒绝；
- query vector 不持久化。

### 9.7 后续 Persistence Worker 待审 migration tests

raw SQLite：

-从旧 schema 增加 content_hash；
- migration 重跑幂等；
-旧 Hash行映射；
-旧 BGE行保持 legacy unknown；
- migration 失败回滚；
-不丢失 memories/jobs/embeddings。

SQLAlchemy：

- fresh DB schema包含新字段；
- create_all 不被误认为旧库迁移；
-旧 SQLAlchemy schema给出明确重建/不兼容提示；
- SQLAlchemy worker single-job 和双 store claim smoke继续通过。

### 9.8 当前 Worker验收命令

~~~text
pytest
ruff check .
python -m compileall -q app tests
git diff --check
~~~

可选本地 smoke：

-本地 BGE小批量 document/query；
- SiliconFlow fake transport；
-正式启用SiliconFlow Profile前，显式完成真实capture、人工审查和qualification发布门禁。

## 10. Manager 已确认的 Foundation 决策

### 10.1 Worker 范围与向量空间

-当前Worker只负责Foundation、SiliconFlow API Provider、本地BGE、Hash、统一Factory、
  配置、兼容shim、Provider contract tests、observability契约和qualification probe代码；
-生产远程空间固定为SiliconFlow Pro/BAAI/bge-m3、批量/v1/embeddings、1024维且不发送
  dimensions；
-本地BGE-M3与SiliconFlow是不同向量空间；本地pooling固定为cls；
- Hash只用于测试和显式开发模式，任何runtime failure都不自动切换Hash。
- `deterministic_hash_v1`具有确定性离线baseline并作为唯一当前`approved` Profile提供
  canonical Factory无网络smoke；SiliconFlow与本地BGE继续保持`review`。

### 10.2 Port、输出与单轮所有权

- Port为同步batch-first，保留query/document purpose；
-所有Provider输出统一验证并转换为L2-normalized little-endian float32；
-单轮query最终由后续Semantic Query层统一批量生成和共享，不由Controller或各模块重复
  请求Provider；Foundation本轮不实现TurnSemanticContext或跨轮cache。

### 10.3 Profile、Signature 与 qualification

-向量空间由版本控制的EmbeddingSpaceProfile manifest定义，env只选择profile；
- SiliconFlow secret固定读取SILICONFLOW_API_KEY；
- Signature canonical schema由Foundation定义，包含profile_id、space字段、
  canary_set_version与expected_fingerprint，但当前不决定数据库存储字段；
- SiliconFlow未知pooling/model revision写provider_managed，不伪造commit；
- qualification必须相对版本化坐标baseline逐条比较，并区分expected_fingerprint与
  observed_fingerprint；只比较canary内部相似度矩阵不合格；
- qualification probe代码、fake transport与离线比较测试是当前Worker必交付；真实联网
  capture、人工审查和qualification是SiliconFlow Profile从review升级为approved的发布门禁。

### 10.4 配置、Factory 与 Observability

- EmbeddingSpaceProfile影响signature，EmbeddingRuntimeOptions不影响signature；
-每个进程使用同一profile/config语义，但独立创建Provider/client；
-统一Factory无隐式fallback，并提供通过现有AppLoggerPort记录一个逻辑batch一条终态事件
  的ObservableEmbeddingProvider；本阶段不修改main.py，实际CLI/API/Memory Worker Logger
  注入留给对应入口后续接入；
-固定记录provider、profile_id、purpose、batch_size、input_tokens、duration_ms、retry_count、
  provider_request_id和error_code；不记录原文、文本hash、向量、secret、headers或原始body。
-普通Factory重新加载并完整校验canonical approved Profile，拒绝仅篡改
  `release_status`或其他字段的调用方对象；`batch_size`统一解释为Provider内部拆批大小，
  SiliconFlow自动拆批并保持逻辑结果顺序；qualification CLI实现独立的completed/failed
  单终态事件。

### 10.5 Dependencies、错误与兼容 shim

~~~text
embedding-api:
  httpx

embedding-local:
  torch
  transformers
  required local model dependencies
~~~

-基础安装不强制安装HTTP/Torch依赖；Hash测试可直接注入，显式开发Hash还需安全开关；
- Foundation新增独立EMBEDDING_* typed errors/ErrorCode，不混入MODEL_*；
-旧app.retrieval.embeddings import、model_name/dimension、embed_query()/embed_texts()和旧
  AEMEATH_MEMORY_EMBEDDING_*配置保留一个发布周期，内部实现只有一份；
-旧配置与canonical配置冲突时fail fast。数据库兼容与迁移期限不由当前Worker决定。

## 11. 后续 Persistence / Memory / RAG 待审提案

本节仅保留候选方向，帮助后续Worker发现风险；不表示Manager已经批准，也不构成当前
Embedding Worker的实现要求。每个后续Worker必须先提交自己的设计并通过复审，再冻结
schema、业务降级或job状态语义。

### 11.1 Persistence 待审提案

- canonical signature存入新字段还是复用embedding_model；
- Memory content hash是否新增列、nullable/NULL语义与迁移方式；
- BLOB协议、复用/拒绝/重建矩阵；
- legacy BGE是否重建、legacy Hash是否映射；
- raw SQLite和SQLAlchemy两个backend的schema/store contract。

### 11.2 Memory 待审提案

-缺失、混合或损坏向量时查询兼容子集、整轮关键词降级或其他显式策略；
- extraction已提交但embedding失败后的pending事实状态；
- repair使用sweep还是专用job、pending IDs和最终job状态语义；
-启动失败、重试、stale-running恢复和幂等upsert边界；
-业务启用Memory但profile无效时是否让Worker进程fail fast。

### 11.3 RAG 待审提案

- mixed signature是整体拒绝、选择兼容build还是其他显式策略；
- manifest、staging/ready/failed与切换语义；
-增量、全量重建和旧ready index保留策略；
- corpus/content hash及legacy index迁移。

### 11.4 Semantic Query 待审提案

-单轮query收集、去重、batch和共享对象；
-消费者访问边界、失败传播和turn结束销毁；
-与App Log/turn Trace的关联方式；
-不做跨轮Redis/SQLite/TTL/LRU cache。

## 12. 实施和审查顺序

固定流程：

1. 当前Worker完成本次小范围设计修订；
2. Manager复审Foundation接口、profile/qualification、observability与Worker范围；
3. 批准后，Worker实现Embedding Foundation、SiliconFlow、本地BGE、Hash、Factory、配置、
   shim、contract tests和qualification probe；
4. Persistence Worker先提交存储/迁移设计，审批后再接入signature/content hash并执行双
   backend审查；
5. Memory Worker先提交coverage/repair/job设计，审批后再接入batch document embedding、
   repair和降级；
6. RAG Worker先提交manifest/staging/rebuild设计，审批后再实施；
7. Semantic Query Worker独立设计并实现单轮query批量生成与共享；
8. Manager更新architecture、interfaces、task_log和发布迁移说明。

在各下游任务独立完成并通过审查前，不启用主运行时向量World Info RAG，不把query
embedding职责放入DialogueController，也不删除旧import和配置兼容层。

## 13. T-0138 发布状态更新（2026-07-28）

本设计中的发布门禁已由 T-0138 对 SiliconFlow Profile 独立执行：基于当前 canonical canary
重新 capture 候选，人工确认逐向量 cosine 与坐标偏差，提交版本化 baseline，正式 verify
通过后再将 `siliconflow_pro_bge_m3_v1` 晋升为 `approved`。本地 BGE Profile 仍保持
`review`。该状态更新不改变 Foundation Port、signature、Factory 门禁、日志脱敏或禁止隐式
Hash fallback 的设计。
