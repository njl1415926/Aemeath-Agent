from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
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

    def record(self, record: PromptRunRecord) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as file:
            json.dump(asdict(record), file, ensure_ascii=False)
            file.write("\n")
