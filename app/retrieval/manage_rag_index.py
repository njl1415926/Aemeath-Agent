from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from app.embeddings import (
    DirectQueryEmbeddingSource,
    EmbeddingRuntimeOptions,
    create_embedding_provider,
    load_embedding_space_profile,
)
from app.embeddings.errors import EmbeddingError, EmbeddingUnavailableError
from app.retrieval.canonical_retriever import CanonicalRagRetriever
from app.retrieval.documents import count_by_namespace, load_wuwa_gamedata_chunks
from app.retrieval.errors import RagIndexError
from app.retrieval.generation import RagGenerationBuilder, RagIndexConfig
from app.retrieval.index_manifest import RagIndexFileRepository, utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, inspect, or query an isolated canonical RAG generation.",
    )
    parser.add_argument(
        "--index-root",
        type=Path,
        help="Managed index root. Defaults to AEMEATH_RAG_INDEX_ROOT or data/rag_indexes.",
    )
    parser.add_argument("--index-id", default="wuwa_canon")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Build and optionally activate a generation.")
    _add_provider_arguments(build)
    build.add_argument(
        "--source-dir",
        type=Path,
        default=Path("source_doc/wuthering_waves_gamedata"),
    )
    build.add_argument("--write-batch-size", type=int, default=64)
    build.add_argument("--lock-timeout", type=float, default=5.0)
    build.add_argument(
        "--no-activate",
        action="store_true",
        help="Create a ready generation without switching active.json.",
    )

    status = commands.add_parser("status", help="Show safe generation and active status.")
    status.add_argument(
        "--signature-fingerprint",
        help="Only show the active mapping for this exact signature fingerprint.",
    )

    query = commands.add_parser("query", help="Query an exact-signature ready generation.")
    _add_provider_arguments(query)
    query.add_argument("query")
    query.add_argument("--namespace", action="append", dest="namespaces")
    query.add_argument("--limit", type=int, default=8)
    query.add_argument(
        "--retrieval-intent",
        choices=["query_lore", "query_voice", "query_lore_and_voice"],
        default="query_lore",
    )
    return parser


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        required=True,
        help="Approved Embedding Foundation profile id. No fallback is performed.",
    )
    parser.add_argument("--provider-batch-size", type=int, default=16)
    parser.add_argument(
        "--local-model-path",
        type=Path,
        default=Path(".cache/huggingface/BAAI/bge-m3"),
    )
    parser.add_argument("--local-device", default="auto")
    parser.add_argument("--local-max-length", type=int, default=8192)
    parser.add_argument(
        "--allow-hash",
        action="store_true",
        help="Explicitly allow deterministic_hash_v1 for development or tests.",
    )


def _config(args: argparse.Namespace) -> RagIndexConfig:
    if args.index_root is not None:
        return RagIndexConfig(index_root=args.index_root, index_id=args.index_id)
    return RagIndexConfig.from_environment(index_id=args.index_id)


def _provider_runtime(args: argparse.Namespace):
    profile = load_embedding_space_profile(args.profile)
    runtime = EmbeddingRuntimeOptions(
        batch_size=args.provider_batch_size,
        local_model_path=args.local_model_path,
        local_device=args.local_device,
        local_max_length=args.local_max_length,
        allow_hash=args.allow_hash,
    )
    return create_embedding_provider(profile, runtime)


def _run_build(args: argparse.Namespace) -> int:
    chunks = load_wuwa_gamedata_chunks(args.source_dir)
    provider_runtime = _provider_runtime(args)
    result = _with_provider_runtime(
        provider_runtime,
        lambda provider: RagGenerationBuilder(
            config=_config(args),
            embedding_provider=provider,
            write_batch_size=args.write_batch_size,
            lock_timeout_seconds=args.lock_timeout,
            allow_hash=args.allow_hash,
        ).build(chunks, activate=not args.no_activate),
    )
    print(f"generation_id={result.generation_id}")
    print(f"status={result.status} build_mode={result.build_mode}")
    print(f"activated={result.activated} resumed={result.resumed} no_op={result.no_op}")
    print(
        f"embedded_chunks={result.embedded_chunk_count} "
        f"removed_chunks={result.removed_chunk_count}"
    )
    print(f"corpus_chunks={len(chunks)} namespaces={count_by_namespace(chunks)}")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    config = _config(args)
    repository = RagIndexFileRepository(root=config.index_root, index_id=config.index_id)
    active = repository.read_active(now=utc_now())
    mappings = active.active_by_signature
    if args.signature_fingerprint:
        reference = mappings.get(args.signature_fingerprint)
        mappings = {args.signature_fingerprint: reference} if reference is not None else {}
    print(f"index_id={config.index_id}")
    print(f"index_root={repository.index_root}")
    if mappings:
        for fingerprint, reference in sorted(mappings.items()):
            print(f"active {fingerprint} -> {reference.generation_id}")
    else:
        print("active=(none)")
    scan = repository.scan_generation_manifests()
    if not scan.manifests:
        print("generations=(none)")
    for manifest in scan.manifests:
        print(
            f"generation={manifest.generation_id} status={manifest.status} "
            f"mode={manifest.build_mode} chunks={manifest.chunk_count} "
            f"signature={manifest.embedding_signature_fingerprint}"
        )
    for diagnostic in scan.diagnostics:
        print(
            f"diagnostic generation={diagnostic.generation_id} "
            f"reason={diagnostic.reason}"
        )
    return 0


def _run_query(args: argparse.Namespace) -> int:
    provider_runtime = _provider_runtime(args)
    outcome = _with_provider_runtime(
        provider_runtime,
        lambda provider: CanonicalRagRetriever(
            config=_config(args),
            query_source=DirectQueryEmbeddingSource(provider),
        ).search_with_outcome(
            query=args.query,
            namespaces=args.namespaces,
            limit=args.limit,
            metadata={"retrieval_intent": args.retrieval_intent},
        ),
    )
    print(
        f"availability={outcome.availability_reason} "
        f"generation_id={outcome.generation_id or '(none)'}"
    )
    for index, item in enumerate(outcome.hits, start=1):
        print(
            f"\n{index}. score={item.confidence:.4f} namespace={item.namespace} "
            f"source_type={item.source_type} source_id={item.metadata['source_id']}"
        )
        print(item.content)
    return 0 if outcome.availability_reason in {"available", "no_ready_generation"} else 2


def _with_provider_runtime(provider_runtime, operation):
    primary_error: BaseException | None = None
    try:
        return operation(provider_runtime.provider)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            provider_runtime.close()
        except BaseException as exc:
            if primary_error is None:
                raise EmbeddingUnavailableError(reason="provider_close_failed") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return _run_build(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "query":
            return _run_query(args)
    except (EmbeddingError, RagIndexError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", type(exc).__name__)
        print(f"error={reason}", file=sys.stderr)
        return 2
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
