from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.observability.schemas import PromptRunRecord


class JsonlPromptRunLogger:
    def __init__(self, *, root_dir: Path):
        now = datetime.now()
        self.log_path = (
            root_dir
            / f"{now:%Y}"
            / f"{now:%m}"
            / f"{now:%d}"
            / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{uuid4().hex[:12]}.jsonl"
        )
        self._write_lock = Lock()

    def record(self, record: PromptRunRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False) + "\n"
        with self._write_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as file:
                file.write(line)
