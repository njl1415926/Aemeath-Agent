from __future__ import annotations

import argparse
from pathlib import Path

from app.retrieval.world_info import (
    DEFAULT_WORLD_INFO_PATH,
    WorldInfoKeywordRetriever,
    WorldInfoLoader,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query Aemeath World Info keyword entries.")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--world-info-path", type=Path, default=DEFAULT_WORLD_INFO_PATH)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--include-constants", action="store_true")
    parser.add_argument("--constant-limit", type=int, default=None)
    parser.add_argument("--user-id", default="user_default")
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=None,
        help="Limit displayed content length. Defaults to full content.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    retriever = WorldInfoKeywordRetriever(
        loader=WorldInfoLoader(path=args.world_info_path),
    )
    metadata = {
        "include_world_info_constants": args.include_constants,
        "constant_limit": args.constant_limit,
    }

    if args.query is None:
        print("Aemeath World Info CLI")
        print("Input a query and press Enter. Use /exit or Ctrl+C to quit.")
        print(f"limit={args.limit}")
        print(f"include_constants={args.include_constants}")
        print(f"preview_chars={args.preview_chars or 'full'}")
        if args.constant_limit is not None:
            print(f"constant_limit={args.constant_limit}")
        while True:
            try:
                query = input("\nworld-info> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if query in {"/exit", "/quit", "exit", "quit"}:
                break
            if not query:
                continue
            results = retriever.retrieve(
                query=query,
                user_id=args.user_id,
                limit=args.limit,
                metadata=metadata,
            )
            print_results(
                query=query,
                results=results,
                preview_chars=args.preview_chars,
            )
        return

    results = retriever.retrieve(
        query=args.query,
        user_id=args.user_id,
        limit=args.limit,
        metadata=metadata,
    )

    print(f"query={args.query}")
    print(f"limit={args.limit}")
    print(f"include_constants={args.include_constants}")
    if args.constant_limit is not None:
        print(f"constant_limit={args.constant_limit}")

    print_results(
        query=args.query,
        results=results,
        preview_chars=args.preview_chars,
    )


def print_results(*, query: str, results, preview_chars: int | None) -> None:
    if not results:
        print(f"\nquery={query}")
        print("(no world info matches)")
        return
    for index, item in enumerate(results, start=1):
        metadata = item.metadata
        print(
            f"\n{index}. uid={metadata.get('uid')} comment={metadata.get('comment')} "
            f"namespace={item.namespace} confidence={item.confidence:.4f}"
        )
        print(f"   matched_keys={metadata.get('matched_keys')}")
        secondary = metadata.get("matched_secondary_keys") or []
        if secondary:
            print(f"   matched_secondary_keys={secondary}")
        content = item.content
        if preview_chars is not None:
            content = content[:preview_chars]
        print(f"   {content.replace(chr(10), ' ')}")


if __name__ == "__main__":
    main()
