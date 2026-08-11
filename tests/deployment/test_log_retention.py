from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.deployment.log_retention import (
    LogRetentionPolicy,
    apply_jsonl_retention,
    plan_jsonl_retention,
)
from scripts.deployment.prune_jsonl import main as prune_main

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _write(path: Path, *, size: int, age_days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    timestamp = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_age_retention_dry_run_and_apply_preserve_recent_and_non_jsonl(tmp_path: Path):
    root = (tmp_path / "logs" / "app").resolve()
    old = root / "2026" / "07" / "01" / "old.jsonl"
    recent = root / "2026" / "08" / "11" / "active.jsonl"
    unrelated = root / "notes.txt"
    _write(old, size=20, age_days=40)
    _write(recent, size=30, age_days=0)
    _write(unrelated, size=40, age_days=100)
    policy = LogRetentionPolicy(retention_days=30, max_total_bytes=1024)

    plan = plan_jsonl_retention(root=root, policy=policy, now=NOW)

    assert [entry.path for entry in plan.entries] == [old]
    assert old.exists()
    result = apply_jsonl_retention(plan=plan, policy=policy, now=NOW)
    assert result.target_satisfied is True
    assert result.deleted_file_count == 1
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_size_retention_deletes_oldest_safe_files_but_not_active_file(tmp_path: Path):
    root = (tmp_path / "logs" / "trace").resolve()
    oldest = root / "oldest.jsonl"
    older = root / "older.jsonl"
    active = root / "active.jsonl"
    _write(oldest, size=10, age_days=3)
    _write(older, size=10, age_days=2)
    _write(active, size=20, age_days=0)
    policy = LogRetentionPolicy(
        retention_days=30,
        max_total_bytes=25,
        active_grace_seconds=3600,
    )

    plan = plan_jsonl_retention(root=root, policy=policy, now=NOW)

    assert [entry.path for entry in plan.entries] == [oldest, older]
    assert {entry.reason for entry in plan.entries} == {"size"}
    result = apply_jsonl_retention(plan=plan, policy=policy, now=NOW)
    assert result.target_satisfied is True
    assert active.exists()
    assert not oldest.exists()
    assert not older.exists()


def test_active_files_can_make_size_target_explicitly_unmet(tmp_path: Path):
    root = (tmp_path / "logs" / "app").resolve()
    active = root / "active.jsonl"
    _write(active, size=20, age_days=0)
    policy = LogRetentionPolicy(retention_days=30, max_total_bytes=10)

    plan = plan_jsonl_retention(root=root, policy=policy, now=NOW)

    assert plan.entries == ()
    assert plan.target_satisfied is False
    assert active.exists()


def test_apply_skips_file_changed_after_plan(tmp_path: Path):
    root = (tmp_path / "logs" / "app").resolve()
    old = root / "old.jsonl"
    _write(old, size=20, age_days=40)
    policy = LogRetentionPolicy(retention_days=30, max_total_bytes=1024)
    plan = plan_jsonl_retention(root=root, policy=policy, now=NOW)
    old.write_bytes(b"changed")

    result = apply_jsonl_retention(plan=plan, policy=policy, now=NOW)

    assert result.deleted_file_count == 0
    assert result.skipped_changed_file_count == 1
    assert old.exists()


def test_symlink_candidate_is_never_followed(tmp_path: Path):
    root = (tmp_path / "logs" / "app").resolve()
    root.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    _write(outside, size=20, age_days=40)
    linked = root / "linked.jsonl"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    plan = plan_jsonl_retention(
        root=root,
        policy=LogRetentionPolicy(retention_days=30, max_total_bytes=1024),
        now=NOW,
    )

    assert plan.entries == ()
    assert plan.skipped_symlink_count == 1
    assert outside.exists()


def test_cli_defaults_to_dry_run_and_outputs_only_aggregate_fields(
    tmp_path: Path, capsys
):
    base = (tmp_path / "logs").resolve()
    old = base / "app" / "secret-name.jsonl"
    _write(old, size=20, age_days=40)

    exit_code = prune_main(
        [
            "--kind",
            "app",
            "--log-root",
            str(base),
            "--retention-days",
            "30",
            "--max-total-bytes",
            "1024",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["mode"] == "dry_run"
    assert payload["planned_file_count"] == 1
    assert "secret-name" not in json.dumps(payload)
    assert old.exists()


def test_relative_root_and_symlink_root_are_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="log_root_must_be_absolute"):
        plan_jsonl_retention(
            root=Path("relative"),
            policy=LogRetentionPolicy(retention_days=1, max_total_bytes=1),
            now=NOW,
        )

    real = (tmp_path / "real").resolve()
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="log_root_symlink_rejected"):
        plan_jsonl_retention(
            root=linked,
            policy=LogRetentionPolicy(retention_days=1, max_total_bytes=1),
            now=NOW,
        )
