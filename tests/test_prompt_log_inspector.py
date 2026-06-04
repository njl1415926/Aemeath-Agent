from __future__ import annotations

import json
from pathlib import Path

from app.observability.prompt_log_inspector import (
    find_prompt_log_files,
    format_prompt_run_record,
    select_prompt_run_records,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            json.dump(record, file, ensure_ascii=False)
            file.write("\n")


def _record(turn_id: str, *, user_input: str = "小屋是什么地方？") -> dict:
    return {
        "run_id": f"prompt_run_{turn_id}",
        "turn_id": turn_id,
        "session_id": "session_test",
        "user_id": "user_default",
        "character_id": "aemeath",
        "created_at": "2026-06-04T10:00:00+00:00",
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "Canon evidence:\n- [world_info:160] 小屋"},
            {"role": "user", "content": user_input},
        ],
        "prompt_context_metadata": {
            "character_id": "aemeath",
            "evidence_ids": {"lore": ["world_info:160"], "voice": []},
            "memory_ids": ["memory_1"],
        },
        "sections": [
            {
                "id": "canon_evidence",
                "title": "Canon evidence:",
                "content_lines": ["- [world_info:160] 小屋"],
                "source_ids": ["world_info:160"],
                "metadata": {},
            },
            {
                "id": "interaction_memories",
                "title": "Long-term user memories:",
                "content_lines": ["- [memory_1] 用户喜欢看 prompt 日志。"],
                "source_ids": ["memory_1"],
                "metadata": {},
            },
        ],
        "response": "那里像是一个可以稍微停一下的地方。",
        "warnings": [],
        "status": "completed",
        "error_type": None,
        "error": None,
    }


def test_prompt_log_inspector_selects_latest_records(tmp_path: Path):
    root = tmp_path / ".aemeath" / "sessions"
    older = root / "2026" / "06" / "03" / "rollout-old.jsonl"
    newer = root / "2026" / "06" / "04" / "rollout-new.jsonl"
    _write_jsonl(older, [_record("turn_old", user_input="早哇")])
    _write_jsonl(newer, [_record("turn_1"), _record("turn_2")])

    files = find_prompt_log_files(root)
    assert files == [older, newer]

    selected_file, records = select_prompt_run_records(root=root, limit=1)
    assert selected_file == newer
    assert records[0]["turn_id"] == "turn_2"


def test_prompt_log_inspector_formats_high_value_fields(tmp_path: Path):
    path = tmp_path / ".aemeath" / "sessions" / "2026" / "06" / "04" / "rollout.jsonl"
    _write_jsonl(path, [_record("turn_1")])
    _selected_file, records = select_prompt_run_records(log_file=path)

    output = format_prompt_run_record(records[0])

    assert "turn_id: turn_1" in output
    assert "User input:" in output
    assert "小屋是什么地方？" in output
    assert "lore_evidence_ids: ['world_info:160']" in output
    assert "memory_ids: ['memory_1']" in output
    assert "- canon_evidence: lines=1 sources=['world_info:160']" in output
    assert "那里像是一个可以稍微停一下的地方。" in output


def test_prompt_log_inspector_filters_by_turn_id(tmp_path: Path):
    path = tmp_path / ".aemeath" / "sessions" / "2026" / "06" / "04" / "rollout.jsonl"
    _write_jsonl(path, [_record("turn_1"), _record("turn_2", user_input="早哇")])

    _selected_file, records = select_prompt_run_records(log_file=path, turn_id="turn_1")

    assert len(records) == 1
    assert records[0]["turn_id"] == "turn_1"
