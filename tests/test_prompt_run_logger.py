from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.observability.prompt_run_logger import JsonlPromptRunLogger
from app.observability.schemas import PromptRunRecord
from app.schemas import AgentTurnInput
from app.utils.ids import new_id


class _FailingResponseGenerator:
    def generate(self, *, messages, user_input, retrieved_memories):
        raise ValueError("provider failed token=secret123 x-api-key=secret456")


def test_jsonl_prompt_run_logger_writes_utf8_record(tmp_path: Path):
    logger = JsonlPromptRunLogger(root_dir=tmp_path / ".aemeath" / "sessions")
    logger.record(
        PromptRunRecord(
            run_id="prompt_run_test",
            turn_id="turn_test",
            session_id="session_test",
            user_id="user_test",
            character_id="aemeath",
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "小屋"}],
            prompt_context_metadata={"character_id": "aemeath"},
            sections=[
                {
                    "id": "canon_evidence",
                    "title": "Canon evidence:",
                    "content_lines": ["- 小屋"],
                    "source_ids": ["world_info:160"],
                    "metadata": {},
                }
            ],
            response="我在。",
            warnings=[],
            status="completed",
        )
    )

    log_files = list((tmp_path / ".aemeath" / "sessions").glob("*/*/*/*.jsonl"))
    assert len(log_files) == 1
    raw = log_files[0].read_text(encoding="utf-8")
    assert "小屋" in raw
    assert "\\u5c0f\\u5c4b" not in raw
    record = json.loads(raw)
    assert record["run_id"] == "prompt_run_test"
    assert record["sections"][0]["source_ids"] == ["world_info:160"]


def test_controller_writes_prompt_run_log_when_enabled(settings):
    settings = replace(
        settings,
        enable_prompt_run_log=True,
        prompt_run_log_root=settings.db_path.parent / ".aemeath" / "sessions",
    )
    from app.main import build_controller

    controller, _memory_admin, resolved_settings, _store = build_controller(settings=settings)
    session_id = new_id("session")

    output = controller.handle_user_message(
        AgentTurnInput(
            user_id=resolved_settings.default_user_id,
            session_id=session_id,
            character_id=resolved_settings.default_character_id,
            message="小屋是什么地方？",
            metadata={},
        )
    )

    log_files = list(resolved_settings.prompt_run_log_root.glob("*/*/*/*.jsonl"))
    assert len(log_files) == 1
    record = json.loads(log_files[0].read_text(encoding="utf-8"))
    assert record["turn_id"] == output.turn_id
    assert record["messages"][-1] == {"role": "user", "content": "小屋是什么地方？"}
    assert record["response"] == output.response
    assert record["status"] == "completed"
    assert any(section["id"] == "canon_evidence" for section in record["sections"])


def test_controller_does_not_create_prompt_log_when_disabled(controller_and_store):
    controller, _store, settings = controller_and_store
    session_id = new_id("session")

    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="早哇",
            metadata={},
        )
    )

    assert not settings.prompt_run_log_root.exists()


def test_controller_failed_prompt_run_log_contains_safe_error_fields(settings):
    settings = replace(
        settings,
        enable_prompt_run_log=True,
        prompt_run_log_root=settings.db_path.parent / ".aemeath" / "sessions",
    )
    from app.main import build_controller

    controller, _memory_admin, resolved_settings, _store = build_controller(settings=settings)
    controller.response_generator = _FailingResponseGenerator()

    with pytest.raises(ValueError):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=resolved_settings.default_user_id,
                session_id=new_id("session"),
                character_id=resolved_settings.default_character_id,
                message="fail after prompt",
                metadata={"authorization": "Bearer real-token"},
            )
        )

    [log_file] = list(resolved_settings.prompt_run_log_root.glob("*/*/*/*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))

    assert record["status"] == "failed"
    assert record["error_code"] == "INVALID_INPUT"
    assert record["error_type"] == "ValueError"
    assert record["error_public_message"] == "Invalid input."
    assert record["error"]
    assert "secret123" not in record["error"]
    assert "secret456" not in record["error"]
    assert "real-token" not in json.dumps(record, ensure_ascii=False)
