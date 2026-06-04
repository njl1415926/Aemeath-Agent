# RAG Worker World Info Integration Report

## Summary

`WorldInfoKeywordRetriever` is now packaged as the active retrieval-layer RAG source through
`WorldInfoEvidenceProvider`.

The main runtime wiring now uses:

```python
evidence_provider=WorldInfoEvidenceProvider()
```

`DialogueController` and `CharacterProvider` remain decoupled from World Info internals. The
controller still calls only `EvidenceProviderPort.build_evidence_pack(...)`.

## Current Runtime Scope

- Runtime source: `app/retrieval/data/Aemeath_World_Info.json`.
- Runtime loader reads top-level `entries`.
- Disabled or empty entries are skipped.
- Matching is keyword-based, with optional secondary-key logic.
- Results enter prompt through `EvidencePack.lore_results`.
- `EvidencePack.voice_results` remains empty.
- Embedding/vector RAG remains available as tooling, but it is not the active main-runtime
  provider.

## Provider Behavior

- Default keyword limit is `5`.
- Marker-like SillyTavern constants are not injected by default.
- `uid 160` home/cabin evidence is a normal key-triggered entry with keys for cabin/home.
- `uid 86` base worldview evidence is appended only when a key-triggered result uid is in the
  approved base-worldview trigger set.
- No-match daily input returns an empty `EvidencePack`.
- Metadata can still request raw constant retrieval for debugging, but normal runtime behavior is
  provider-policy driven.

## Trace Metadata

Each `RetrievalResult.metadata` includes non-sensitive fields:

- `retriever`
- `uid`
- `comment`
- `matched_keys`
- `matched_secondary_keys`
- `constant`
- `position`
- `order`
- `depth`
- `probability`
- `source_path`

## Validation

Added or updated tests cover:

- `app/main.py` injects `WorldInfoEvidenceProvider`.
- key-triggered World Info evidence returns as lore results.
- unmatched casual input returns an empty evidence pack.
- uid 160 home/cabin evidence is returned only by keyword match.
- marker constants are not included by default.
- base worldview is added only for approved trigger uids.
- constants do not consume keyword limit.
- World Info evidence appears in `ContextBuilder` Canon evidence.

Recommended verification:

```bash
ruff check .
pytest -q
python -m app.retrieval.world_info_cli "IRIS"
```
