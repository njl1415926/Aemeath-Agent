# SiliconFlow BGE-M3 Profile Approval Delivery

Date: 2026-07-28

Task: T-0138

Status: Manager accepted

## Result

`siliconflow_pro_bge_m3_v1` is now an approved Embedding Foundation Profile for normal
API-backed runtime. The local `.env` selects it with Hash disabled, so enabled long-term Memory
uses the same synchronized SiliconFlow Provider for turn-level Semantic Query, Memory recall,
Memory extraction artifacts, and Memory repair.

This task did not activate managed vector RAG. `WorldInfoEvidenceProvider` remains the dialogue
runtime evidence provider. `local_bge_m3_v1` remains `review`, and no provider failure can switch
implicitly to deterministic Hash.

## Qualification Evidence

The candidate was captured from the current version-controlled `bge_m3_canary_v1` raw-byte
artifact. Older candidates were rejected as stale.

- Provider/model: `siliconflow` / `Pro/BAAI/bge-m3`
- Dimension: 1024
- Canary count: 6
- Baseline size: 24,576 bytes (`6 * 1024 * float32`)
- Baseline SHA-256:
  `sha256:2636ec030d9d8bf123ad0e3ab016b20f36bca8de259756d93c26b1a3f0762b63`
- Qualification fingerprint:
  `sha256:9117a6ed90373361c1374c4b87e065c451ac4e38e9ee3abc8735317b70b1d64e`
- Canonical signature fingerprint:
  `sha256:ac8b1ffb3e1a2e43115a9f7d71c4279072f9d0e14508c63b3ab7f0d4ad76aea2`
- Minimum cosine threshold: `0.9999`
- Maximum coordinate-delta threshold: `0.003`

Fresh capture passed. An independent check using runtime batch size 16 also passed. The formal
`verify` command passed every repeated batch and single-order observation; its minimum observed
cosine was `0.9999483512870173` and maximum observed coordinate delta was
`0.0024224817752838135`.

The Profile remained `review` while the candidate baseline was copied and verified. It was changed
to `approved` only after the formal real-API verify succeeded.

## Runtime Validation

The normal approved-only Factory, rather than the qualification-only Factory, completed real
`purpose=query` and `purpose=document` calls. Both returned finite L2-normalized 1024-dimensional
vectors with the expected canonical signature.

A temporary SQLite smoke used the real Provider through Memory runtime composition:

1. `MemoryExtractionService` atomically created a Memory fact.
2. A SiliconFlow `purpose=document` call wrote its exact-signature artifact.
3. `MemoryReader` generated a `purpose=query` vector and recalled the fact through
   `embedding_similarity`.

The main `data/aemeath.db` was not modified by this smoke. Existing Hash artifacts were not deleted;
they remain valid derived data in a separate exact-signature space. On subsequent Memory Worker
runs, repair may create missing SiliconFlow artifacts for existing active Memory facts and therefore
consume API quota.

## Configuration

Local untracked `.env` now uses:

```env
AEMEATH_EMBEDDING_PROFILE=siliconflow_pro_bge_m3_v1
AEMEATH_EMBEDDING_ALLOW_HASH=false
```

`SILICONFLOW_API_KEY` remains local secret configuration and was not copied into source, logs,
reports, tests, or command output. `.env.example` keeps deterministic Hash as the no-secret explicit
development default and documents SiliconFlow as the approved real-runtime alternative.

## Validation

```text
pytest -q tests/embeddings tests/test_memory_canonical_integration.py \
  tests/test_semantic_query.py tests/test_semantic_query_integration.py
174 passed

pytest -q
437 passed

ruff check .
All checks passed

python -m compileall -q app tests
passed

git diff --check
passed (CRLF conversion warnings only)

python -m pip wheel . --no-deps --wheel-dir .pytest_tmp/wheels
passed
```

The built wheel contains both the approved Profile JSON and the baseline binary, and the packaged
baseline digest matches the Profile manifest.

## Remaining Boundaries

- Local BGE-M3 still needs its own qualification and approval.
- Managed vector RAG needs a production corpus build review and a separate active-provider wiring
  decision.
- API cost, rate limiting, and concurrency controls remain separate deployment tasks.
- T-0137 records future Character/Memory data-evolution work; no schema change was made here.
