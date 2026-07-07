from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_LOG_ROOT = Path(".aemeath/sessions")


def find_prompt_log_files(root: Path = DEFAULT_LOG_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("*/*/*/*.jsonl"), key=lambda path: path.stat().st_mtime)


def load_prompt_run_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return records


def select_prompt_run_records(
    *,
    root: Path = DEFAULT_LOG_ROOT,
    log_file: Path | None = None,
    turn_id: str | None = None,
    limit: int = 1,
) -> tuple[Path, list[dict[str, Any]]]:
    if limit < 1:
        raise ValueError("limit must be >= 1")

    selected_file = log_file
    if selected_file is None:
        files = find_prompt_log_files(root)
        if not files:
            raise FileNotFoundError(f"No prompt log files found under {root}")
        selected_file = files[-1]

    records = load_prompt_run_records(selected_file)
    if turn_id:
        records = [record for record in records if record.get("turn_id") == turn_id]
    if not records:
        raise LookupError("No prompt run records matched the requested filters")
    return selected_file, records[-limit:]


def format_prompt_run_record(
    record: dict[str, Any],
    *,
    include_system_prompt: bool = False,
    include_sections: bool = True,
) -> str:
    messages = record.get("messages") or []
    system_message = _first_message_content(messages, role="system")
    user_message = _last_message_content(messages, role="user")
    metadata = record.get("prompt_context_metadata") or {}
    evidence_ids = metadata.get("evidence_ids") or {}
    memory_ids = metadata.get("memory_ids") or []

    lines = [
        f"turn_id: {record.get('turn_id', '')}",
        f"run_id: {record.get('run_id', '')}",
        f"status: {record.get('status', '')}",
        f"created_at: {record.get('created_at', '')}",
        f"session_id: {record.get('session_id', '')}",
        f"user_id: {record.get('user_id', '')}",
        f"character_id: {record.get('character_id', '')}",
        f"model: {record.get('model', '')}",
        f"lore_evidence_ids: {evidence_ids.get('lore', [])}",
        f"voice_evidence_ids: {evidence_ids.get('voice', [])}",
        f"memory_ids: {memory_ids}",
        "",
        "User input:",
        user_message or "(none)",
        "",
        "Response:",
        record.get("response") or "(none)",
    ]

    warnings = record.get("warnings") or []
    if warnings:
        lines.extend(["", f"Warnings: {warnings}"])

    if record.get("status") == "failed":
        lines.extend(
            [
                "",
                f"Error code: {record.get('error_code') or '(unknown)'}",
                f"Error type: {record.get('error_type') or '(unknown)'}",
                f"User message: {record.get('error_public_message') or '(none)'}",
                f"Error: {record.get('error') or '(none)'}",
            ]
        )

    if include_sections:
        lines.extend(["", "Sections:"])
        for section in record.get("sections") or []:
            content_lines = section.get("content_lines") or []
            source_ids = section.get("source_ids") or []
            preview = _preview_content(content_lines)
            lines.append(
                f"- {section.get('id', '')}: lines={len(content_lines)} "
                f"sources={source_ids} preview={preview}"
            )

    if include_system_prompt:
        lines.extend(["", "System prompt:", system_message or "(none)"])

    return "\n".join(lines)


def _first_message_content(messages: list[dict[str, str]], *, role: str) -> str:
    for message in messages:
        if message.get("role") == role:
            return message.get("content", "")
    return ""


def _last_message_content(messages: list[dict[str, str]], *, role: str) -> str:
    for message in reversed(messages):
        if message.get("role") == role:
            return message.get("content", "")
    return ""


def _preview_content(content_lines: list[str], *, max_chars: int = 120) -> str:
    text = " ".join(line.strip() for line in content_lines if line.strip())
    if not text:
        return "(empty)"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect Aemeath prompt run JSONL logs.")
    parser.add_argument("--root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--turn-id", default=None)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--show-system", action="store_true")
    parser.add_argument("--hide-sections", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_file, records = select_prompt_run_records(
        root=args.root,
        log_file=args.file,
        turn_id=args.turn_id,
        limit=args.limit,
    )
    print(f"log_file: {log_file}")
    for index, record in enumerate(records, start=1):
        if len(records) > 1:
            print(f"\n=== Record {index}/{len(records)} ===")
        print(
            format_prompt_run_record(
                record,
                include_system_prompt=args.show_system,
                include_sections=not args.hide_sections,
            )
        )


if __name__ == "__main__":
    main()
