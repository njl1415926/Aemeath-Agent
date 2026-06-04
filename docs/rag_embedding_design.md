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

Production/local embedding target:

- `BAAI/bge-m3`
- local path: `.cache/huggingface/BAAI/bge-m3`
- provider: `BgeM3EmbeddingProvider`

Test and smoke-test embedding:

- `DeterministicHashEmbeddingProvider`

Vector storage:

- SQLite file, default `data/aemeath_rag.sqlite`
- exact cosine/dot search over normalized vectors
- one table stores chunk content, metadata JSON, and embedding blobs

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

## Build And Query

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
