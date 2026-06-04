from __future__ import annotations

import argparse
import time
from pathlib import Path

from app.retrieval.documents import count_by_namespace, load_wuwa_gamedata_chunks
from app.retrieval.embeddings import BgeM3EmbeddingProvider, DeterministicHashEmbeddingProvider
from app.retrieval.vector_index import SqliteVectorStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Aemeath Wuthering Waves RAG index.")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("source_doc/wuthering_waves_gamedata"),
    )
    parser.add_argument("--db-path", type=Path, default=Path("data/aemeath_rag.sqlite"))
    parser.add_argument(
        "--embedding",
        choices=["hash", "bge-m3"],
        default="bge-m3",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path(".cache/huggingface/BAAI/bge-m3"),
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--write-batch-size",
        type=int,
        default=64,
        help="Number of chunks to embed and write per checkpoint batch.",
    )
    parser.add_argument(
        "--device",
        choices=["auto", "cuda", "cpu"],
        default="auto",
        help="Device for bge-m3 embeddings. Ignored by hash embeddings.",
    )
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--rebuild-existing",
        action="store_true",
        help="Re-embed chunks even if their ids already exist in the SQLite DB.",
    )
    return parser.parse_args()


def build_embedding_provider(args: argparse.Namespace):
    if args.embedding == "hash":
        return DeterministicHashEmbeddingProvider()
    device = resolve_device(args.device)
    print(f"bge-m3 device: {device}")
    return BgeM3EmbeddingProvider(
        model_path=args.model_path,
        batch_size=args.batch_size,
        device=device,
    )


def resolve_device(requested: str) -> str:
    if requested in {"cuda", "cpu"}:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def iter_batches(items: list, batch_size: int):
    for start in range(0, len(items), batch_size):
        yield start, items[start : start + batch_size]


def main() -> None:
    args = parse_args()
    chunks = load_wuwa_gamedata_chunks(args.source_dir)
    print(f"loaded chunks: {len(chunks)}")
    print(f"chunks by namespace: {count_by_namespace(chunks)}")

    embedding_provider = build_embedding_provider(args)
    store = SqliteVectorStore(args.db_path)
    if args.reset:
        store.reset()

    if args.rebuild_existing:
        pending_chunks = chunks
        skipped = 0
    else:
        existing_ids = store.existing_ids(ids=[chunk.id for chunk in chunks])
        pending_chunks = [chunk for chunk in chunks if chunk.id not in existing_ids]
        skipped = len(chunks) - len(pending_chunks)

    print(f"existing chunks skipped: {skipped}")
    print(f"pending chunks: {len(pending_chunks)}")
    if not pending_chunks:
        print(f"db counts by namespace: {store.count_by_namespace()}")
        return

    print(f"embedding model: {embedding_provider.model_name}")
    started_at = time.perf_counter()
    processed = 0
    for batch_start, batch_chunks in iter_batches(pending_chunks, args.write_batch_size):
        batch_started_at = time.perf_counter()
        texts = [chunk.content for chunk in batch_chunks]
        embeddings = embedding_provider.embed_texts(texts)
        store.upsert_chunks(
            chunks=batch_chunks,
            embeddings=embeddings,
            embedding_model=embedding_provider.model_name,
        )
        processed += len(batch_chunks)
        elapsed = time.perf_counter() - started_at
        batch_elapsed = time.perf_counter() - batch_started_at
        rate = processed / elapsed if elapsed else 0.0
        remaining = len(pending_chunks) - processed
        eta_seconds = remaining / rate if rate else 0.0
        print(
            "batch "
            f"{batch_start // args.write_batch_size + 1}: "
            f"wrote={processed}/{len(pending_chunks)} "
            f"batch_seconds={batch_elapsed:.1f} "
            f"rate={rate:.2f}/s "
            f"eta_minutes={eta_seconds / 60:.1f}"
        )
    print(f"wrote sqlite rag db: {args.db_path}")
    print(f"db counts by namespace: {store.count_by_namespace()}")


if __name__ == "__main__":
    main()
