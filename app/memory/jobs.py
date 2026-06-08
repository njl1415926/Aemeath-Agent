from __future__ import annotations

import hashlib


def build_memory_job_idempotency_key(
    *,
    user_id: str,
    character_id: str,
    session_id: str,
    trigger_type: str,
    source_message_ids: list[str],
) -> str:
    source_hash = hashlib.sha256("|".join(sorted(source_message_ids)).encode("utf-8")).hexdigest()
    return (
        f"memory_extract:v1:{user_id}:{character_id}:"
        f"{session_id}:{trigger_type}:{source_hash}"
    )
