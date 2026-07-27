# RAG Embedding Design

## 2026-06-03 Manager Update

Global response modes have been removed from the active runtime. RAG retrieval
decisions now belong inside `EvidenceProvider`.

`DialogueController` calls:

```python
EvidenceProviderPort.build_evidence_pack(
    user_id=...,
    character_id=...,
    query=...,
    metadata=...,
)
```

The provider should return an empty `EvidencePack()` when retrieval is not useful.
`metadata.retrieval_intent` may be used only as a debug/test override, not as a normal
controller-side mode.

## Current Runtime Scope

As of 2026-06-04, the main CLI runtime uses `WorldInfoEvidenceProvider` as the active
`EvidenceProviderPort` implementation. It is a keyword-triggered World Info provider that reads
`app/retrieval/data/Aemeath_World_Info.json` and returns matching entries as
`EvidencePack.lore_results`.

The previous embedding/vector implementation remains available under `app.retrieval` tooling, but
it is not the active provider wired in `app/main.py`.

## Canonical Managed Generation Runtime

The managed vector runtime was implemented on 2026-07-26, but remains isolated tooling. It does
not replace the active `WorldInfoEvidenceProvider` and does not change `app/main.py`.

Each index generation owns one SQLite database and immutable sidecar records:

```text
data/rag_indexes/<index_id>/
  active.json
  build.lock
  generations/g000001-<random>/
    manifest.json
    corpus_inventory.jsonl
    checkpoint.json
    index.sqlite
```

`AEMEATH_RAG_INDEX_ROOT` changes the managed root; its default is `data/rag_indexes`. `index_id`
must match `^[a-z][a-z0-9_-]{0,63}$`. Runtime index files are ignored by Git.

### Inventory Identity

The corpus fingerprint is computed from a sorted canonical inventory. Every inventory entry has
`chunk_id`, `namespace`, `source_type`, `canonical_content_hash`, and
`retrieval_metadata_fingerprint`.

The metadata fingerprint uses an explicit retrieval allowlist. Absolute paths, generation times,
credentials, and other machine-local values are excluded. Managed chunk metadata is rebuilt from
that allowlist before persistence.

### Lifecycle And Activation

Generation states are `building`, `staging`, `ready`, and `failed`.

- `building`: batch-first document embedding and committed checkpoints are in progress.
- `staging`: all writes and stale-chunk deletion are complete.
- `ready`: WAL checkpoint, SQLite integrity check, and exact-signature coverage all passed.
- `failed`: a safe failure reason was recorded; the previous active ready generation is retained.

An OS-level cross-process file lock protects generation number allocation and activation. The
active mapping is reread while the lock is held, flushed to a same-directory temporary file,
`fsync`ed, atomically replaced, and reopened for verification. Windows sharing violations use a
finite retry sequence.

Ready generations are opened read-only and never initialized or migrated on the query path. A
query can only use the ready generation mapped to its exact canonical signature fingerprint.
Mixed vector spaces and implicit provider fallback are prohibited.

`ready` is a validated invariant rather than a trusted manifest label. Registry resolution,
no-op reuse, orphan-ready reuse, activation, and future rollback activation all call the same
`validate_ready_generation()` path. It verifies the persisted inventory, read-only SQLite
integrity, empty WAL state, exact-signature coverage, and absence of artifact diagnostics.

Invalid non-active generation directories are isolated and exposed as safe status diagnostics.
They do not block valid generations. Generation numbers are allocated from every valid managed
directory name, including incomplete directories, so an isolated directory number is never reused.

### Rebuild And Recovery

For the same exact signature and unchanged parser/chunker contracts, corpus changes use a
consistent clone of the previous ready generation. Changed/new chunks are embedded, stale chunks
are transactionally deleted, and unchanged artifacts are retained. Signature, parser, chunker,
chunk-id scheme, namespace policy, or index-format changes cause a full rebuild.

Checkpoints advance only after an embedding batch commits. A matching `building` generation is
resumed after interruption. A normal build exception marks only the new generation failed; the old
ready generation remains active. Version 1 does not automatically delete old generations.

`write_batch_size` is part of build-plan identity because the checkpoint stores a batch index.
Changing batch size therefore starts a different generation instead of interpreting an old index
against a new partition. Staging resume validates inventory existence, ordering, and fingerprint
before any ready transition.

If active-file reopen verification fails after atomic replacement, the repository restores and
verifies the exact previous bytes. Failure to restore is surfaced as a distinct safe rollback
error.

The first managed generation is always rebuilt from authoritative source chunks. Legacy RAG rows
are diagnostic/compatibility inputs only and are never promoted into a managed generation.

### Canonical Query Adapter

`CanonicalRagRetriever` accepts the provider-neutral `QueryEmbeddingSourcePort`, resolves a
`ResolvedQueryEmbedding`, and queries `search_embedding_artifacts()` with the exact signature.
`search_resolved()` is the future replacement point for `TurnSemanticContext` without changing the
shared `RetrieverPort`.

Internally, `CanonicalRagSearchOutcome` reports hits, generation id, artifact diagnostics, and an
availability reason. The public `RetrieverPort` adapter still returns `list[RetrievalResult]`.
Result metadata is constructed from scratch and contains only `generation_id`, `retriever`,
`raw_score`, `namespace`, `source_type`, `signature_fingerprint`, and `source_id`.

## Vector RAG Scope

T-0102 first implementation indexes only official Wuthering Waves game data:

- `source_doc/wuthering_waves_gamedata/aemeath_favor_extract.json`
- `source_doc/wuthering_waves_gamedata/flow_texts_tid_sorted.json`

`source_doc/world_info/world_info.json` is kept out of the vector pipeline. The release copy
`app/retrieval/data/Aemeath_World_Info.json` is handled by the separate keyword-triggered
World Info provider.

## Boundaries

`DialogueController` only calls `EvidenceProviderPort.build_evidence_pack(...)`.
It does not know about embeddings, SQLite, chunking, source paths, or namespace routing.

`CharacterProvider` does not do RAG. Character v0.2 remains responsible for resident identity,
speech rules, boundaries, and stable examples. Dynamic lore, plot, and voice evidence enters
through `EvidencePack`.

Interaction memory remains on `MemoryReaderPort`. Canon lore, plot, profile, and voice evidence
remain on `EvidenceProviderPort`.

## Namespaces

- `canon:profile`: official Aemeath profile and ability records.
- `canon:lore`: official Aemeath stories, goods, and favor-word lore view.
- `canon:plot`: full flow-text scene windows.
- `canon:voice`: Aemeath voice references from favor words and flow voice contexts.

All namespaces are stored in one physical SQLite database and selected at retrieval time.

## Source Preprocessing

### Favor Extract

`favor_role_infos.json` records are split by profile fields:

- `Info`
- `TalentDoc`
- `TalentCertification`

`favor_stories.json` records are chunked by paragraphs.

`favor_goods.json` records are one chunk per record.

`favor_words.json` records generate two views:

- `canon:lore` with `source_type=favor_word_lore`
- `canon:voice` with `source_type=favor_word`

The file-level `counts` field is informational only. Importers use the actual list lengths under
`files`.

### Flow Texts

`flow_texts_tid_sorted.json` is processed in original JSON array order. `source_order` is the only
ordering authority. Fields such as `TalkItemId`, `PlotLineId`, `TextId`, and `ActionIndex` are
metadata only and must not be used for sorting.

Plot view:

- Full flow text is grouped by `StateKey`.
- Each group is split into scene windows.
- Target size is about 900 characters, max about 1200 characters, with 2-line overlap.
- Output namespace is `canon:plot`.

Voice view:

- Only lines where the normalized speaker contains `爱弥斯` are used.
- Consecutive Aemeath lines form a voice run.
- Each run includes one line of context before and after when available.
- Low-information single-line runs such as only `……` are skipped.
- Output namespace is `canon:voice`.

## Embeddings And Storage

The managed builder uses the Embedding Foundation batch-first `EmbeddingProviderPort` with
`purpose=document`. The query path consumes a provider-neutral `ResolvedQueryEmbedding` with
`purpose=query` when using `DirectQueryEmbeddingSource`.

Only approved exact profiles can build or query a managed generation. At the time of this design,
`deterministic_hash_v1` is the only approved profile and additionally requires explicit
`--allow-hash`. Local BGE-M3 and SiliconFlow profiles remain under review and cannot enter the
managed ready path until separately approved. There is no implicit Hash fallback.

The old `data/aemeath_rag.sqlite`, `upsert_chunks()`, and `search()` path remains a one-release
legacy compatibility shim. It is not a managed generation and is not reused by the new builder.

## Retrieval Policy

`WuwaRagEvidenceProvider` owns evidence-need policy and retrieval planning.

The active path does not depend on external response modes. The provider uses an internal
daily-conversation `EvidenceNeedPolicy` that scores each query:

- `lore_score`: canon facts, plot, worldbuilding, relationships, and explanations.
- `voice_score`: Aemeath-like phrasing, comfort, companionship, and line-generation requests.
- `no_retrieval_score`: short greetings and low-information daily utterances.

The default threshold is:

- `lore_score >= 3` and `voice_score >= 3`: `query_lore_and_voice`
- `lore_score >= 3`: `query_lore`
- `voice_score >= 3`: `query_voice`
- otherwise: no retrieval

Daily no-retrieval examples:

- `早呀`
- `我好累`
- `今天有点困`

Lore examples:

- `爱弥斯为什么想成为救世主？`
- `黎那汐塔是什么地方？`

Voice examples:

- `我好累，安慰我`
- `用爱弥斯的语气安慰我`

Lore plus voice example:

- `爱弥斯知道漂泊者过去后会怎么安慰他？`

Debug/test overrides can be passed through `metadata.retrieval_intent`:

- `none`: return `EvidencePack()` without calling the retriever
- `lore`: retrieve `canon:profile`, `canon:lore`, and `canon:plot`, final top 5
- `voice`: retrieve `canon:voice`, final top 2
- `lore_and_voice`: retrieve lore final top 5 and voice final top 3, with overlap removal

The provider writes trace-friendly metadata into retrieved results:

- `retrieval_intent`
- `policy_reason`
- `lore_score`
- `voice_score`
- `no_retrieval_score`

## Overlap Removal

Favor overlap is detected by `original_source_file + record_id`.

Flow overlap is detected by identical `source_path + state_key` and at least 50% range overlap in
`source_order`.

This lets the same source produce both lore and voice views while avoiding duplicate prompt
injection in a single turn.

## Managed Build And Query

Build and activate an explicitly allowed development Hash generation:

```bash
python -m app.retrieval.manage_rag_index build --profile deterministic_hash_v1 --allow-hash
```

Inspect active and retained generations without loading an embedding model:

```bash
python -m app.retrieval.manage_rag_index status
```

Query the exact-signature ready generation:

```bash
python -m app.retrieval.manage_rag_index query --profile deterministic_hash_v1 --allow-hash "Aemeath lore query"
```

Use `--index-root` before the subcommand or set `AEMEATH_RAG_INDEX_ROOT` to select another managed
root. These commands do not enable vector RAG in the dialogue runtime.

## Legacy Build And Query

Build with hash embeddings for smoke tests:

```bash
python scripts/build_wuwa_rag.py --embedding hash --db-path data/aemeath_rag_hash.sqlite --reset
```

Build with local bge-m3:

```bash
python scripts/build_wuwa_rag.py --embedding bge-m3 --db-path data/aemeath_rag.sqlite --reset
```

Query:

```bash
python scripts/query_wuwa_rag.py "爱弥斯为什么想成为救世主？" --retrieval-intent lore
```
