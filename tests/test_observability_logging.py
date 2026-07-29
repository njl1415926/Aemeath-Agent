from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.observability.app_logger import JsonlAppLogger
from app.observability.schemas import AppLogRecord, TraceEvent
from app.observability.trace_logger import JsonlTraceLogger
from app.schemas import AgentTurnInput
from app.utils.ids import new_id


class _FailingResponseGenerator:
    def generate(self, *, messages, user_input, retrieved_memories):
        raise ValueError("provider failed token=secret123 x-api-key=secret456")


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_trace_logger_writes_utf8_one_line_and_redacts_payload(tmp_path: Path):
    logger = JsonlTraceLogger(root_dir=tmp_path / ".aemeath" / "traces")

    logger.record(
        TraceEvent(
            event_type="turn_started",
            turn_id="turn_test",
            session_id="session_test",
            user_id="user_test",
            character_id="aemeath",
            payload={
                "message": "小屋",
                "api-key": "api-secret",
                "x-api-key": "x-secret",
                "token": "token-secret",
                "authorization": "Bearer auth-secret",
                "cookie": "cookie-secret",
                "nested": {"note": "token=inline-secret"},
            },
        )
    )

    [log_file] = list((tmp_path / ".aemeath" / "traces").glob("*/*/*/*.jsonl"))
    raw = log_file.read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    assert "小屋" in raw
    assert "\\u5c0f\\u5c4b" not in raw
    assert "api-secret" not in raw
    assert "x-secret" not in raw
    assert "token-secret" not in raw
    assert "auth-secret" not in raw
    assert "cookie-secret" not in raw
    assert "inline-secret" not in raw

    [record] = _load_jsonl(log_file)
    assert record["event_type"] == "turn_started"
    assert record["payload"]["api-key"] == "[redacted]"
    assert record["payload"]["x-api-key"] == "[redacted]"
    assert record["payload"]["token"] == "[redacted]"
    assert record["payload"]["authorization"] == "[redacted]"
    assert record["payload"]["cookie"] == "[redacted]"
    assert record["payload"]["nested"]["note"] == "token=[redacted]"


def test_jsonl_app_logger_writes_structured_sanitized_record(tmp_path: Path):
    logger = JsonlAppLogger(root_dir=tmp_path / ".aemeath" / "app_logs")

    logger.record(
        AppLogRecord(
            event_name="worker_started",
            level="info",
            user_id="user_test",
            payload={
                "worker_id": "worker_test",
                "authorization": "Bearer app-secret",
                "cookie": "session=secret-cookie",
                "status": "running",
            },
        )
    )

    [log_file] = list((tmp_path / ".aemeath" / "app_logs").glob("*/*/*/*.jsonl"))
    raw = log_file.read_text(encoding="utf-8")
    assert "app-secret" not in raw
    assert "secret-cookie" not in raw

    [record] = _load_jsonl(log_file)
    assert record["event_name"] == "worker_started"
    assert record["payload"]["authorization"] == "[redacted]"
    assert record["payload"]["cookie"] == "[redacted]"
    assert record["payload"]["status"] == "running"


def test_controller_writes_trace_log_when_enabled(settings):
    settings = replace(
        settings,
        enable_trace_log=True,
        trace_log_root=settings.db_path.parent / ".aemeath" / "traces",
    )
    from app.main import build_controller

    runtime = build_controller(settings=settings)
    controller, resolved_settings = runtime.agent_service.controller, runtime.settings
    output = controller.handle_user_message(
        AgentTurnInput(
            user_id=resolved_settings.default_user_id,
            session_id=new_id("session"),
            character_id=resolved_settings.default_character_id,
            message="小屋是什么地方？",
            metadata={"client": "cli"},
        )
    )

    [log_file] = list(resolved_settings.trace_log_root.glob("*/*/*/*.jsonl"))
    records = _load_jsonl(log_file)
    event_types = [record["event_type"] for record in records]

    assert "turn_started" in event_types
    assert "turn_completed" in event_types
    assert all(record["turn_id"] == output.turn_id for record in records)


def test_controller_failed_turn_trace_contains_safe_error_fields(settings):
    settings = replace(
        settings,
        enable_trace_log=True,
        trace_log_root=settings.db_path.parent / ".aemeath" / "traces",
    )
    from app.main import build_controller

    runtime = build_controller(settings=settings)
    controller, resolved_settings = runtime.agent_service.controller, runtime.settings
    controller.response_generator = _FailingResponseGenerator()

    with pytest.raises(ValueError):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=resolved_settings.default_user_id,
                session_id=new_id("session"),
                character_id=resolved_settings.default_character_id,
                message="fail after prompt",
                metadata={"authorization": "Bearer real-token", "cookie": "cookie-secret"},
            )
        )

    [log_file] = list(resolved_settings.trace_log_root.glob("*/*/*/*.jsonl"))
    records = _load_jsonl(log_file)
    failed = [record for record in records if record["event_type"] == "turn_failed"]

    assert len(failed) == 1
    assert failed[0]["payload"]["error"]["code"] == "INVALID_INPUT"
    raw = log_file.read_text(encoding="utf-8")
    assert "secret123" not in raw
    assert "secret456" not in raw
    assert "real-token" not in raw
    assert "cookie-secret" not in raw


def test_disabled_trace_logging_does_not_create_files(controller_and_store):
    controller, _store, settings = controller_and_store

    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=new_id("session"),
            character_id=settings.default_character_id,
            message="早",
            metadata={},
        )
    )

    assert not settings.trace_log_root.exists()
