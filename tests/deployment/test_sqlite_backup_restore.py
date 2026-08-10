from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.sqlite_store import SQLiteStore
from app.deployment import sqlite_backup
from app.deployment.sqlite_backup import (
    BACKUP_DB_NAME,
    MANIFEST_NAME,
    BackupCommittedDurabilityUncertain,
    D6SqliteError,
    _move_old_set_to_quarantine,
    create_sqlite_backup,
    restore_sqlite_backup,
    sha256_file,
    verify_sqlite_backup,
)

VALID_APP_DIGEST = "sha256:" + "a" * 64
VALID_OPS_DIGEST = "sha256:" + "b" * 64
VALID_DEPLOY_LOCK_SHA256 = "c" * 64
VALID_COMPOSE_CONFIG_SHA256 = "d" * 64
VALID_GIT_COMMIT = "e" * 40


def _seed_db(path: Path) -> None:
    store = SQLiteStore(path)
    store.ensure_session(session_id="session_a", user_id="user_a", character_id="aemeath")
    store.save_message(
        session_id="session_a",
        user_id="user_a",
        role="user",
        content="hello",
    )
    store.save_message(
        session_id="session_a",
        user_id="user_a",
        role="assistant",
        content="hi",
    )


def _backup(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)
    result = create_sqlite_backup(
        db_path=db_path,
        backup_root=backup_root,
        backend="sqlite_raw",
        label="test",
        max_duration_seconds=30,
        app_image_digest=VALID_APP_DIGEST,
        ops_image_digest=VALID_OPS_DIGEST,
        deploy_lock_sha256=VALID_DEPLOY_LOCK_SHA256,
        compose_config_sha256=VALID_COMPOSE_CONFIG_SHA256,
        git_commit=VALID_GIT_COMMIT,
    )
    return db_path, result.backup_dir


def _read_manifest(backup_dir: Path) -> dict[str, object]:
    return json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def _db_set_snapshot(live_db: Path) -> dict[str, bytes | None]:
    return {
        suffix: Path(f"{live_db}{suffix}").read_bytes()
        if Path(f"{live_db}{suffix}").exists()
        else None
        for suffix in ("", "-wal", "-shm")
    }


def test_backup_publishes_generation_directory_and_verify_passes(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)

    manifest = _read_manifest(backup_dir)
    assert (backup_dir / BACKUP_DB_NAME).is_file()
    assert (backup_dir / MANIFEST_NAME).is_file()
    assert manifest["artifact"]["generation_dir"] == backup_dir.name
    assert manifest["artifact"]["sha256"] == sha256_file(backup_dir / BACKUP_DB_NAME)
    assert manifest["deployment"]["ops_image_digest"] == VALID_OPS_DIGEST
    assert manifest["deployment"]["app_image_digest"] == VALID_APP_DIGEST
    assert manifest["deployment"]["deploy_lock_sha256"] == VALID_DEPLOY_LOCK_SHA256
    assert manifest["deployment"]["compose_config_sha256"] == VALID_COMPOSE_CONFIG_SHA256
    assert "offsite" not in manifest
    assert manifest["offsite_interface"] == "documented_only"

    verified = verify_sqlite_backup(backup_dir=backup_dir, backend="sqlite_raw")

    assert verified.backup_dir == backup_dir


def test_backup_deadline_fails_without_published_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)
    values = iter([0.0, 999.0])
    monkeypatch.setattr(
        sqlite_backup.time,
        "monotonic",
        lambda: next(values, 999.0),
    )

    with pytest.raises(D6SqliteError) as raised:
        create_sqlite_backup(
            db_path=db_path,
            backup_root=backup_root,
            backend="sqlite_raw",
            label="deadline",
            max_duration_seconds=0.001,
            app_image_digest=VALID_APP_DIGEST,
            ops_image_digest=VALID_OPS_DIGEST,
            deploy_lock_sha256=VALID_DEPLOY_LOCK_SHA256,
            compose_config_sha256=VALID_COMPOSE_CONFIG_SHA256,
            git_commit=VALID_GIT_COMMIT,
        )

    assert raised.value.reason == "backup_deadline_exceeded"
    assert not list((backup_root / "backups").rglob(MANIFEST_NAME))


def test_backup_deadline_includes_post_copy_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)
    clock = {"value": 0.0}
    original_integrity = sqlite_backup._require_integrity_clean

    def delayed_integrity(connection: sqlite3.Connection) -> None:
        original_integrity(connection)
        clock["value"] = 1.0

    monkeypatch.setattr(sqlite_backup.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(sqlite_backup, "_require_integrity_clean", delayed_integrity)

    with pytest.raises(D6SqliteError) as raised:
        create_sqlite_backup(
            db_path=db_path,
            backup_root=backup_root,
            backend="sqlite_raw",
            label="deadline-verify",
            max_duration_seconds=0.01,
            app_image_digest=VALID_APP_DIGEST,
            ops_image_digest=VALID_OPS_DIGEST,
            deploy_lock_sha256=VALID_DEPLOY_LOCK_SHA256,
            compose_config_sha256=VALID_COMPOSE_CONFIG_SHA256,
            git_commit=VALID_GIT_COMMIT,
        )

    assert raised.value.reason == "backup_deadline_exceeded"
    assert not list((backup_root / "backups").rglob(MANIFEST_NAME))


@pytest.mark.parametrize("failure_target", ["date_dir", "backup_root"])
def test_backup_post_commit_fsync_failure_is_unambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
):
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)
    original_fsync_directory = sqlite_backup._fsync_directory

    def fail_after_commit(path: Path) -> None:
        original_fsync_directory(path)
        committed = list((backup_root / "backups").rglob(MANIFEST_NAME))
        if not committed:
            return
        if failure_target == "date_dir" and path.name == "07":
            raise OSError("post-commit date fsync failed")
        if failure_target == "backup_root" and path.resolve() == backup_root.resolve():
            raise OSError("post-commit root fsync failed")

    monkeypatch.setattr(sqlite_backup, "_fsync_directory", fail_after_commit)

    with pytest.raises(BackupCommittedDurabilityUncertain) as raised:
        create_sqlite_backup(
            db_path=db_path,
            backup_root=backup_root,
            backend="sqlite_raw",
            label="post-commit",
            max_duration_seconds=30,
            app_image_digest=VALID_APP_DIGEST,
            ops_image_digest=VALID_OPS_DIGEST,
            deploy_lock_sha256=VALID_DEPLOY_LOCK_SHA256,
            compose_config_sha256=VALID_COMPOSE_CONFIG_SHA256,
            git_commit=VALID_GIT_COMMIT,
            now=datetime(2026, 8, 7, tzinfo=timezone.utc),
        )

    assert raised.value.reason == "backup_committed_durability_uncertain"
    assert raised.value.exit_code == 24
    assert (raised.value.backup_dir / BACKUP_DB_NAME).is_file()
    assert (raised.value.backup_dir / MANIFEST_NAME).is_file()
    assert verify_sqlite_backup(backup_dir=raised.value.backup_dir, backend="sqlite_raw")


def test_production_backup_rejects_unknown_identity(tmp_path: Path):
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)

    with pytest.raises(D6SqliteError) as raised:
        create_sqlite_backup(
            db_path=db_path,
            backup_root=backup_root,
            backend="sqlite_raw",
            label="unknown",
        )

    assert raised.value.reason == "deployment_identity_invalid"


def test_verify_rejects_incomplete_generation_directory(tmp_path: Path):
    backup_dir = tmp_path / "backups" / "backups" / "2026" / "08" / "07" / "aemeath-bad"
    backup_dir.mkdir(parents=True)
    (backup_dir / BACKUP_DB_NAME).write_bytes(b"not sqlite")

    with pytest.raises(D6SqliteError) as raised:
        verify_sqlite_backup(backup_dir=backup_dir, backend="sqlite_raw")

    assert raised.value.reason == "backup_generation_incomplete"
    assert raised.value.exit_code == 21


def test_verify_rejects_malformed_manifest_without_internal_error(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)
    (backup_dir / MANIFEST_NAME).write_text(json.dumps({"format_version": "wrong"}))

    with pytest.raises(D6SqliteError) as raised:
        verify_sqlite_backup(backup_dir=backup_dir, backend="sqlite_raw")

    assert raised.value.reason == "manifest_format_invalid"
    assert raised.value.exit_code == 21


def test_verify_rejects_nested_manifest_missing_fields(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)
    manifest = _read_manifest(backup_dir)
    manifest["source"].pop("backend")
    (backup_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(D6SqliteError) as raised:
        verify_sqlite_backup(backup_dir=backup_dir, backend="sqlite_raw")

    assert raised.value.reason == "manifest_missing_fields"


def test_verify_rejects_manifest_with_unknown_identity(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)
    manifest = _read_manifest(backup_dir)
    manifest["deployment"]["ops_image_digest"] = "unknown"
    (backup_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(D6SqliteError) as raised:
        verify_sqlite_backup(backup_dir=backup_dir, backend="sqlite_raw")

    assert raised.value.reason == "manifest_deployment_identity_invalid"


def test_verify_rejects_truncated_backup(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)
    db_file = backup_dir / BACKUP_DB_NAME
    db_file.write_bytes(db_file.read_bytes()[:128])

    with pytest.raises(D6SqliteError) as raised:
        verify_sqlite_backup(backup_dir=backup_dir, backend="sqlite_raw")

    assert raised.value.reason == "backup_hash_mismatch"


def test_restore_quarantines_old_set_and_retires_pointer(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())
    Path(f"{live_db}-wal").write_bytes(b"old wal bytes")
    Path(f"{live_db}-shm").write_bytes(b"old shm bytes")

    result = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=data_root / ".restore-journal",
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        restore_id="rst_test_one",
    )

    assert result.stage == "retired"
    assert live_db.exists()
    assert not Path(f"{live_db}-wal").exists()
    assert not Path(f"{live_db}-shm").exists()
    journal_root = data_root / ".restore-journal"
    assert not (journal_root / "active.pointer.json").exists()
    journal = json.loads(
        (journal_root / "journals" / "rst_test_one" / "journal.json").read_text(
            encoding="utf-8"
        )
    )
    old_set = {item["name"]: item for item in journal["old_set"]}
    assert old_set["aemeath.db-wal"]["state"] == "absent"
    assert old_set["aemeath.db-shm"]["state"] == "absent"


def test_restore_old_set_sidecars_are_moved_with_per_file_journal(tmp_path: Path):
    data_root = tmp_path / "restore-data"
    journal_root = data_root / ".restore-journal"
    quarantine = journal_root / "journals" / "rst_sidecars" / "quarantine" / "old"
    data_root.mkdir()
    journal_root.mkdir()
    paths = [data_root / "aemeath.db", data_root / "aemeath.db-wal", data_root / "aemeath.db-shm"]
    payloads = [b"main", b"wal", b"shm"]
    for path, payload in zip(paths, payloads, strict=True):
        path.write_bytes(payload)
    journal = {
        "format_version": "aemeath.sqlite_restore.journal.v1",
        "restore_id": "rst_sidecars",
        "updated_at_utc": "2026-08-07T00:00:00Z",
        "stage": "old_quarantine_in_progress",
        "paths": {
            "data_root": str(data_root),
            "db_name": "aemeath.db",
            "journal_root": str(journal_root),
            "journal_dir": str(journal_root / "journals" / "rst_sidecars"),
            "quarantine_dir": str(quarantine),
        },
        "old_set": [
            {
                "name": path.name,
                "expected_present": True,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "live_path": str(path),
                "quarantine_path": str(quarantine / path.name),
                "state": "live",
                "last_intent": None,
                "completed_at_utc": None,
            }
            for path in paths
        ],
    }

    _move_old_set_to_quarantine(journal, fault_hook=None)

    assert journal["stage"] == "old_quarantined"
    for entry in journal["old_set"]:
        assert entry["state"] == "move_completed"
        assert not Path(entry["live_path"]).exists()
        assert Path(entry["quarantine_path"]).exists()


def test_restore_id_rejects_path_traversal(tmp_path: Path):
    _original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()

    with pytest.raises(D6SqliteError) as raised:
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="../rst_escape",
        )

    assert raised.value.reason == "restore_id_invalid"


@pytest.mark.parametrize(
    "crash_event",
    [
        "pointer_started",
        "pointer_prepared",
        "pointer_inventory",
        "completed_aemeath.db",
    ],
)
def test_explicit_rollback_from_early_stage_rejects_without_live_mutation(
    tmp_path: Path,
    crash_event: str,
):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())

    def fault(event: str) -> None:
        if event == crash_event:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_reject_early_rollback",
            fault_hook=fault,
        )
    before = _db_set_snapshot(live_db)

    with pytest.raises(D6SqliteError) as raised:
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            rollback_restore_id="rst_reject_early_rollback",
        )

    assert raised.value.reason == "rollback_stage_not_safe"
    assert _db_set_snapshot(live_db) == before


def test_explicit_rollback_from_old_quarantined_restores_old_set(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())

    def fault(event: str) -> None:
        if event == "journal_old_quarantined":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_old_quarantined_rollback",
            fault_hook=fault,
        )
    assert not live_db.exists()

    result = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=data_root / ".restore-journal",
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        rollback_restore_id="rst_old_quarantined_rollback",
    )

    assert result.stage == "retired"
    assert live_db.read_bytes() == original_db.read_bytes()


def test_restore_lock_contention_reports_stable_error(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())
    journal_root = data_root / ".restore-journal"
    journal_root.mkdir()
    locker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "import sys, time\n"
                "from app.deployment.sqlite_backup import _restore_operation_lock\n"
                "root = Path(sys.argv[1])\n"
                "with _restore_operation_lock(root):\n"
                "    print('ready', flush=True)\n"
                "    time.sleep(30)\n"
            ),
            str(journal_root),
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert locker.stdout is not None
        assert locker.stdout.readline().strip() == "ready"

        with pytest.raises(D6SqliteError) as raised:
            restore_sqlite_backup(
                data_root=data_root,
                db_name="aemeath.db",
                backup_dir=backup_dir,
                journal_root=journal_root,
                expected_backend="sqlite_raw",
                confirm_writers_stopped=True,
                restore_id="rst_lock_loser",
            )

        assert raised.value.reason == "restore_in_progress"
        assert not (journal_root / "journals" / "rst_lock_loser").exists()
        assert live_db.exists()
    finally:
        locker.terminate()
        try:
            locker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            locker.kill()
            locker.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="requires Linux process semantics")
def test_linux_two_restore_processes_are_serialized(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    (data_root / "aemeath.db").write_bytes(original_db.read_bytes())
    journal_root = data_root / ".restore-journal"
    repo = Path(__file__).resolve().parents[2]
    script = (
        "from pathlib import Path\n"
        "import json, sys, time\n"
        "from app.deployment.sqlite_backup import D6SqliteError, restore_sqlite_backup\n"
        "mode, data_root, backup_dir, journal_root, restore_id = sys.argv[1:]\n"
        "def fault(event):\n"
        "    if mode == 'winner' and event == 'journal_started':\n"
        "        print('entered', flush=True)\n"
        "        time.sleep(3)\n"
        "try:\n"
        "    result = restore_sqlite_backup(\n"
        "        data_root=Path(data_root), db_name='aemeath.db', backup_dir=Path(backup_dir),\n"
        "        journal_root=Path(journal_root), expected_backend='sqlite_raw',\n"
        "        confirm_writers_stopped=True, restore_id=restore_id, fault_hook=fault,\n"
        "    )\n"
        "    print(json.dumps({'status': 'ok', 'stage': result.stage}), flush=True)\n"
        "except D6SqliteError as exc:\n"
        "    print(json.dumps({'status': 'failed', 'reason': exc.reason}), flush=True)\n"
        "    raise SystemExit(exc.exit_code)\n"
    )
    winner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            "winner",
            str(data_root),
            str(backup_dir),
            str(journal_root),
            "rst_linux_winner",
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert winner.stdout is not None
        assert winner.stdout.readline().strip() == "entered"
        loser = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                "loser",
                str(data_root),
                str(backup_dir),
                str(journal_root),
                "rst_linux_loser",
            ],
            cwd=repo,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        winner_stdout, winner_stderr = winner.communicate(timeout=20)
    finally:
        if winner.poll() is None:
            winner.kill()
            winner.wait(timeout=5)

    assert loser.returncode == 31
    assert json.loads(loser.stdout.splitlines()[-1]) == {
        "status": "failed",
        "reason": "restore_in_progress",
    }
    assert winner.returncode == 0, winner_stderr
    assert json.loads(winner_stdout.splitlines()[-1]) == {"status": "ok", "stage": "retired"}
    assert not (journal_root / "journals" / "rst_linux_loser").exists()


@pytest.mark.parametrize(
    "event",
    ["candidate_temp_prepared", "candidate_published", "candidate_publish_fsynced"],
)
def test_restore_resume_after_candidate_install_crash_window(tmp_path: Path, event: str):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())

    def fault(seen: str) -> None:
        if seen == event:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_candidate_window",
            fault_hook=fault,
        )

    result = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=data_root / ".restore-journal",
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        resume_restore_id="rst_candidate_window",
    )

    assert result.stage == "retired"
    assert live_db.exists()
    assert not list(data_root.glob(".aemeath.db.rst_candidate_window.candidate.tmp"))


def test_restore_resume_rejects_candidate_sidecar_after_publish(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())

    def fault(event: str) -> None:
        if event == "candidate_published":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_candidate_sidecar",
            fault_hook=fault,
        )
    Path(f"{live_db}-wal").write_bytes(b"candidate sidecar")

    with pytest.raises(D6SqliteError) as raised:
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            resume_restore_id="rst_candidate_sidecar",
        )

    assert raised.value.reason == "old_sidecar_still_live"


def test_restore_rollback_reentry_after_old_db_restored(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())
    journal_root = data_root / ".restore-journal"

    def crash_after_candidate_journal(event: str) -> None:
        if event == "journal_candidate_installed":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=journal_root,
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_rollback_reentry",
            fault_hook=crash_after_candidate_journal,
        )

    live_db.write_bytes(b"not sqlite")

    def crash_after_old_db_restored(event: str) -> None:
        if event == "completed_aemeath.db":
            raise RuntimeError("simulated rollback crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=journal_root,
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            resume_restore_id="rst_rollback_reentry",
            fault_hook=crash_after_old_db_restored,
        )

    result = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=journal_root,
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        resume_restore_id="rst_rollback_reentry",
    )

    assert result.stage == "retired"
    assert live_db.read_bytes() == original_db.read_bytes()


def test_restore_resume_uses_journal_over_stale_pointer_stage(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())
    seen: list[str] = []

    def fault(event: str) -> None:
        seen.append(event)
        if event == "journal_old_quarantined":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_stale_pointer",
            fault_hook=fault,
        )

    journal_root = data_root / ".restore-journal"
    pointer = json.loads((journal_root / "active.pointer.json").read_text(encoding="utf-8"))
    journal = json.loads(
        (journal_root / "journals" / "rst_stale_pointer" / "journal.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["stage"] == "old_quarantined"
    assert pointer["stage"] != journal["stage"]

    result = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=journal_root,
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        resume_restore_id="rst_stale_pointer",
    )

    assert result.stage == "retired"
    assert not (journal_root / "active.pointer.json").exists()


def test_restore_rejects_split_set_with_duplicate_digest(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())

    def fault(event: str) -> None:
        if event == "completed_aemeath.db":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            restore_id="rst_duplicate",
            fault_hook=fault,
        )
    quarantine_db = (
        data_root
        / ".restore-journal"
        / "journals"
        / "rst_duplicate"
        / "quarantine"
        / "old"
        / "aemeath.db"
    )
    live_db.write_bytes(quarantine_db.read_bytes())

    with pytest.raises(D6SqliteError) as raised:
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
            resume_restore_id="rst_duplicate",
        )

    assert raised.value.reason == "restore_file_duplicate"


def test_two_independent_restores_after_pointer_retirement(tmp_path: Path):
    original_db, backup_dir = _backup(tmp_path)
    data_root = tmp_path / "restore-data"
    data_root.mkdir()
    live_db = data_root / "aemeath.db"
    live_db.write_bytes(original_db.read_bytes())
    journal_root = data_root / ".restore-journal"

    first = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=journal_root,
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        restore_id="rst_first",
    )
    second = restore_sqlite_backup(
        data_root=data_root,
        db_name="aemeath.db",
        backup_dir=backup_dir,
        journal_root=journal_root,
        expected_backend="sqlite_raw",
        confirm_writers_stopped=True,
        restore_id="rst_second",
    )

    assert first.stage == "retired"
    assert second.stage == "retired"
    assert (journal_root / "journals" / "rst_first" / "journal.json").exists()
    assert (journal_root / "journals" / "rst_second" / "journal.json").exists()


def test_production_restore_rejects_sqlalchemy_manifest(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)
    manifest_path = backup_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["backend"] = "sqlalchemy"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data_root = tmp_path / "restore-data"
    data_root.mkdir()

    with pytest.raises(D6SqliteError) as raised:
        restore_sqlite_backup(
            data_root=data_root,
            db_name="aemeath.db",
            backup_dir=backup_dir,
            journal_root=data_root / ".restore-journal",
            expected_backend="sqlite_raw",
            confirm_writers_stopped=True,
        )

    assert raised.value.reason == "production_sqlalchemy_manifest_rejected"


def test_cli_verify_reports_safe_json(tmp_path: Path):
    _db_path, backup_dir = _backup(tmp_path)

    completed = __import__("subprocess").run(
        [
            __import__("sys").executable,
            "scripts/deployment/verify_sqlite_backup.py",
            "--backup-dir",
            str(backup_dir),
            "--backend",
            "sqlite_raw",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert "hello" not in completed.stdout


def test_cli_backup_rejects_unknown_identity(tmp_path: Path):
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/deployment/backup_sqlite.py",
            "--db-path",
            str(db_path),
            "--backup-root",
            str(backup_root),
            "--backend",
            "sqlite_raw",
            "--label",
            "cli-unknown",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "status": "failed",
        "reason": "deployment_identity_invalid",
    }
    assert not list((backup_root / "backups").rglob(MANIFEST_NAME))


@pytest.mark.skipif(__import__("os").name == "nt", reason="requires Linux read-only permissions")
def test_linux_read_only_wal_backup(tmp_path: Path):
    db_path = tmp_path / "data" / "aemeath.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    _seed_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "INSERT INTO messages (id, session_id, user_id, role, content, created_at) "
        "VALUES ("
        "'linux_wal_msg', 'session_a', 'user_a', 'user', 'wal', "
        "'2026-08-07T00:00:00Z'"
        ")"
    )
    connection.commit()
    connection.close()
    db_path.chmod(0o444)
    Path(f"{db_path}-wal").chmod(0o444)
    Path(f"{db_path}-shm").chmod(0o444)

    result = create_sqlite_backup(
        db_path=db_path,
        backup_root=backup_root,
        backend="sqlite_raw",
        label="wal",
        max_duration_seconds=30,
        app_image_digest=VALID_APP_DIGEST,
        ops_image_digest=VALID_OPS_DIGEST,
        deploy_lock_sha256=VALID_DEPLOY_LOCK_SHA256,
        compose_config_sha256=VALID_COMPOSE_CONFIG_SHA256,
        git_commit=VALID_GIT_COMMIT,
    )

    assert verify_sqlite_backup(backup_dir=result.backup_dir, backend="sqlite_raw")
