from __future__ import annotations

import argparse
from pathlib import Path

from app.retrieval.embedding_retriever import SqliteEmbeddingRetriever
from app.retrieval.embeddings import BgeM3EmbeddingProvider, DeterministicHashEmbeddingProvider
from app.retrieval.provider import WuwaRagEvidenceProvider
from app.retrieval.vector_index import SqliteVectorStore

VALID_INTENTS = {"auto", "lore", "lore_and_voice", "none", "voice"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive Wuthering Waves RAG query CLI.")
    parser.add_argument("--db-path", type=Path, default=Path("data/aemeath_rag.sqlite"))
    parser.add_argument("--embedding", choices=["hash", "bge-m3"], default="bge-m3")
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path(".cache/huggingface/BAAI/bge-m3"),
    )
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument(
        "--retrieval-intent",
        default="auto",
        choices=sorted(VALID_INTENTS),
    )
    parser.add_argument("--user-id", default="user_default")
    parser.add_argument("--character-id", default="aemeath")
    parser.add_argument("--preview-chars", type=int, default=500)
    return parser.parse_args()


def resolve_device(requested: str) -> str:
    if requested in {"cuda", "cpu"}:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def build_embedding_provider(args: argparse.Namespace):
    if args.embedding == "hash":
        return DeterministicHashEmbeddingProvider()
    device = resolve_device(args.device)
    print(f"embedding=bge-m3 device={device}")
    return BgeM3EmbeddingProvider(model_path=args.model_path, device=device)


def print_help() -> None:
    print(
        "\nCommands:\n"
        "  /intent <intent>       switch retrieval intent\n"
        "                         auto, none, lore, voice, lore_and_voice\n"
        "  /i <intent>            switch retrieval intent\n"
        "  /intents               list retrieval intents\n"
        "  /counts                show db namespace counts\n"
        "  /help                  show this help\n"
        "  /quit                  exit\n"
    )


def print_results(title: str, items, *, preview_chars: int) -> None:
    print(f"\n## {title}")
    if not items:
        print("(none)")
        return
    for index, item in enumerate(items, start=1):
        metadata = item.metadata
        source = metadata.get("original_source_file") or metadata.get("state_key")
        title_text = metadata.get("title") or ""
        print(
            f"{index}. score={item.confidence:.4f} "
            f"namespace={item.namespace} source_type={item.source_type}"
        )
        print(f"   id={item.id}")
        if source:
            print(f"   source={source}")
        if title_text:
            print(f"   title={title_text}")
        preview = item.content[:preview_chars].replace("\n", " ")
        print(f"   {preview}")


def main() -> None:
    args = parse_args()
    store = SqliteVectorStore(args.db_path)
    print(f"db={args.db_path}")
    print(f"counts={store.count_by_namespace()}")
    embedding_provider = build_embedding_provider(args)
    retriever = SqliteEmbeddingRetriever(store=store, embedding_provider=embedding_provider)
    provider = WuwaRagEvidenceProvider(retriever=retriever)
    intent = args.retrieval_intent
    print(f"retrieval_intent={intent}")
    print_help()

    while True:
        try:
            raw = input(f"\nrag[{intent}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw in {"/quit", "/q", "quit", "exit"}:
            break
        if raw in {"/help", "/h"}:
            print_help()
            continue
        if raw == "/intents":
            print("\n".join(sorted(VALID_INTENTS)))
            continue
        if raw == "/counts":
            print(store.count_by_namespace())
            continue
        if raw.startswith("/intent ") or raw.startswith("/i "):
            next_intent = raw.split(maxsplit=1)[1].strip()
            if next_intent not in VALID_INTENTS:
                print(f"Unknown retrieval intent: {next_intent}")
                print("Use /intents to list valid intents.")
                continue
            intent = next_intent
            print(f"retrieval_intent={intent}")
            continue

        pack = provider.build_evidence_pack(
            user_id=args.user_id,
            character_id=args.character_id,
            query=raw,
            metadata=(
                {"retrieval_intent": intent}
                if intent != "auto"
                else None
            ),
        )
        print_results("Lore Results", pack.lore_results, preview_chars=args.preview_chars)
        print_results("Voice Results", pack.voice_results, preview_chars=args.preview_chars)


if __name__ == "__main__":
    main()
