from __future__ import annotations

from pathlib import Path

from app.observability.app_logger import JsonlAppLogger
from app.observability.noop import NoopAppLogger
from app.observability.ports import AppLoggerPort


def create_app_logger(*, enabled: bool, root_dir: Path) -> AppLoggerPort:
    if enabled:
        return JsonlAppLogger(root_dir=root_dir)
    return NoopAppLogger()
