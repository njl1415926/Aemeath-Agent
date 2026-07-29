from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from app.observability.app_logger import JsonlAppLogger
from app.observability.safe import SafeAppLogger, SafeTraceLogger
from app.observability.schemas import AppLogRecord, TraceEvent


class _FailingLogger:
    def record(self, _record):
        raise OSError("credential=NEVER_RETAIN")


def test_safe_loggers_isolate_delegate_failure_and_keep_only_safe_state():
    app_logger = SafeAppLogger(_FailingLogger())
    trace_logger = SafeTraceLogger(_FailingLogger())
    app_logger.record(AppLogRecord(event_name="test"))
    trace_logger.record(
        TraceEvent(
            event_type="test",
            turn_id="t",
            session_id="s",
            user_id="u",
            character_id="c",
        )
    )
    assert (app_logger.failure_count, app_logger.last_error_type) == (1, "OSError")
    assert (trace_logger.failure_count, trace_logger.last_error_type) == (1, "OSError")
    assert "NEVER_RETAIN" not in repr(app_logger.__dict__)


def test_app_jsonl_is_line_safe_under_concurrent_writers(tmp_path):
    logger = JsonlAppLogger(root_dir=tmp_path)

    def write(index: int):
        logger.record(AppLogRecord(event_name="parallel", payload={"measured_count": index}))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(100)))
    records = [
        json.loads(line)
        for line in logger.log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 100
    assert {record["payload"]["measured_count"] for record in records} == set(range(100))
