from __future__ import annotations

import argparse
from pathlib import Path

from app.retrieval.embedding_retriever import SqliteEmbeddingRetriever
from app.retrieval.embeddings import BgeM3EmbeddingProvider, DeterministicHashEmbeddingProvider
from app.retrieval.provider import WuwaRagEvidenceProvider
from app.retrieval.vector_index import SqliteVectorStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query Aemeath Wuthering Waves RAG index.")
    parser.add_argument("query")
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
    parser.add_argument(
        "--retrieval-intent",
        choices=["auto", "none", "lore", "voice", "lore_and_voice"],
        default="auto",
    )
    parser.add_argument("--user-id", default="user_default")
    parser.add_argument("--character-id", default="aemeath")
    return parser.parse_args()


def build_embedding_provider(args: argparse.Namespace):
    if args.embedding == "hash":
        return DeterministicHashEmbeddingProvider()
    return BgeM3EmbeddingProvider(model_path=args.model_path)


def print_results(title: str, items) -> None:
    print(f"\n## {title}")
    if not items:
        print("(none)")
        return
    for item in items:
        metadata = item.metadata
        source = metadata.get("original_source_file") or metadata.get("state_key")
        print(
            f"- {item.id} score={item.confidence:.4f} "
            f"namespace={item.namespace} source_type={item.source_type} source={source}"
        )
        print(item.content[:500].replace("\n", " "))


def main() -> None:
    args = parse_args()
    embedding_provider = build_embedding_provider(args)
    store = SqliteVectorStore(args.db_path)
    retriever = SqliteEmbeddingRetriever(
        store=store,
        embedding_provider=embedding_provider,
    )
    provider = WuwaRagEvidenceProvider(retriever=retriever)
    pack = provider.build_evidence_pack(
        user_id=args.user_id,
        character_id=args.character_id,
        query=args.query,
        metadata=(
            {"retrieval_intent": args.retrieval_intent}
            if args.retrieval_intent != "auto"
            else None
        ),
    )
    print_results("Lore Results", pack.lore_results)
    print_results("Voice Results", pack.voice_results)


if __name__ == "__main__":
    main()
