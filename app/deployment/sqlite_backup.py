from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from app.db.browser_auth_migrations import (
    BROWSER_AUTH_MIGRATION_ID,
    BROWSER_AUTH_SCHEMA_REVISION,
    validate_browser_auth_schema_readonly,
)
from app.db.sqlite_index_contract import (
    ACTIVE_MEMORY_IDENTITY_INDEX,
    has_valid_active_memory_identity_index,
)
from app.db.sqlite_store import (
    MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION,
    MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION,
    MEMORY_SCOPE_MIGRATION_VERSION,
)
from app.db.streaming_chat_migrations import (
    STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,
    validate_streaming_chat_schema_readonly,
)
from app.utils.ids import new_id

BACKUP_MANIFEST_VERSION = "aemeath.sqlite_backup.manifest.v1"
SCHEMA_CONTRACT_VERSION = "aemeath.sqlite.schema_contract.v1"
RESTORE_JOURNAL_VERSION = "aemeath.sqlite_restore.journal.v1"
RESTORE_POINTER_VERSION = "aemeath.sqlite_restore.active_pointer.v1"
BACKUP_DB_NAME = "aemeath.db"
MANIFEST_NAME = "manifest.json"
RESTORE_SIDECAR_SUFFIXES = ("", "-wal", "-shm")
PRODUCTION_BACKEND = "sqlite_raw"
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
RESTORE_ID_PATTERN = re.compile(r"^rst_[A-Za-z0-9_-]{1,80}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


class D6SqliteError(Exception):
    def __init__(self, reason: str, exit_code: int):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


class BackupDeadlineExceeded(D6SqliteError):
    def __init__(self) -> None:
        super().__init__("backup_deadline_exceeded", 23)


class BackupCommittedDurabilityUncertain(D6SqliteError):
    def __init__(self, backup_id: str, backup_dir: Path) -> None:
        super().__init__("backup_committed_durability_uncertain", 24)
        self.backup_id = backup_id
        self.backup_dir = backup_dir


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    backup_dir: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class VerifyResult:
    backup_id: str
    backup_dir: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class RestoreResult:
    restore_id: str
    stage: str
    journal_dir: Path


FaultHook = Callable[[str], None]


def create_sqlite_backup(
    *,
    db_path: Path,
    backup_root: Path,
    backend: str,
    label: str = "manual",
    busy_timeout_ms: int = 5000,
    pages_per_step: int = 256,
    sleep_ms: int = 50,
    max_duration_seconds: float = 300.0,
    app_image_digest: str = "unknown",
    ops_image_digest: str = "unknown",
    deploy_lock_sha256: str = "unknown",
    compose_config_sha256: str = "unknown",
    git_commit: str = "unknown",
    compose_project: str = "aemeath",
    now: datetime | None = None,
) -> BackupResult:
    if backend not in {"sqlite_raw", "sqlalchemy"}:
        raise D6SqliteError("backend_invalid", 2)
    if not LABEL_PATTERN.fullmatch(label):
        raise D6SqliteError("label_invalid", 2)
    if busy_timeout_ms <= 0 or pages_per_step <= 0 or sleep_ms < 0:
        raise D6SqliteError("backup_tuning_invalid", 2)
    if max_duration_seconds <= 0:
        raise D6SqliteError("backup_deadline_invalid", 2)
    _validate_backup_identity(
        backend=backend,
        app_image_digest=app_image_digest,
        ops_image_digest=ops_image_digest,
        deploy_lock_sha256=deploy_lock_sha256,
        compose_config_sha256=compose_config_sha256,
        git_commit=git_commit,
        reason="deployment_identity_invalid",
        exit_code=2,
    )

    source = _require_regular_file(db_path, "db_path")
    root = _require_directory(backup_root, "backup_root", writable=True)
    data_root = source.parent.resolve()
    if _is_relative_to(root, data_root):
        raise D6SqliteError("backup_root_inside_data_root", 10)

    created_at = now or datetime.now(timezone.utc)
    backup_id = new_id("bkp")
    label_part = label
    timestamp = _timestamp_for_name(created_at)
    date_dir = (
        root
        / "backups"
        / created_at.strftime("%Y")
        / created_at.strftime("%m")
        / created_at.strftime("%d")
    )
    generation_name = f"aemeath-{timestamp}-{label_part}-{backup_id}"
    final_dir = date_dir / generation_name
    tmp_root = root / "tmp"
    tmp_dir = tmp_root / f"generation-{backup_id}"
    if final_dir.exists() or tmp_dir.exists():
        raise D6SqliteError("destination_exists", 11)
    tmp_dir.mkdir(parents=True, mode=0o750, exist_ok=False)
    _fsync_directory(tmp_dir.parent)
    destination_tmp = tmp_dir / f"{BACKUP_DB_NAME}.tmp"
    manifest_tmp = tmp_dir / f"{MANIFEST_NAME}.tmp"

    deadline = time.monotonic() + max_duration_seconds
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = _connect_readonly(source, timeout_seconds=busy_timeout_ms / 1000)
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.execute("PRAGMA foreign_keys=ON")
        destination_conn = sqlite3.connect(destination_tmp)

        def _progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() > deadline:
                raise BackupDeadlineExceeded()

        try:
            source_conn.backup(
                destination_conn,
                pages=pages_per_step,
                progress=_progress,
                sleep=sleep_ms / 1000,
            )
        except BackupDeadlineExceeded:
            raise
        except sqlite3.OperationalError as exc:
            raise D6SqliteError("sqlite_backup_failed", 20) from exc
        _check_backup_deadline(deadline)
        destination_conn.execute("PRAGMA foreign_keys=ON")
        _require_integrity_clean(destination_conn)
        _check_backup_deadline(deadline)
        _require_foreign_keys_clean(destination_conn)
        _check_backup_deadline(deadline)
        destination_conn.commit()
        destination_conn.close()
        destination_conn = None
        _fsync_file(destination_tmp)
        _check_backup_deadline(deadline)
        _fsync_directory(tmp_dir)
        _check_backup_deadline(deadline)

        manifest = _build_manifest(
            db_path=source,
            backup_db=destination_tmp,
            generation_name=generation_name,
            backup_id=backup_id,
            label=label,
            backend=backend,
            created_at=created_at,
            source_journal_mode=_source_journal_mode(source_conn),
            app_image_digest=app_image_digest,
            ops_image_digest=ops_image_digest,
            deploy_lock_sha256=deploy_lock_sha256,
            compose_config_sha256=compose_config_sha256,
            git_commit=git_commit,
            compose_project=compose_project,
        )
        _check_backup_deadline(deadline)
        _write_json_atomic(manifest_tmp, manifest)
        _check_backup_deadline(deadline)
        (tmp_dir / f"{BACKUP_DB_NAME}.tmp").replace(tmp_dir / BACKUP_DB_NAME)
        _fsync_directory(tmp_dir)
        _check_backup_deadline(deadline)
        manifest_tmp.replace(tmp_dir / MANIFEST_NAME)
        _fsync_directory(tmp_dir)
        _check_backup_deadline(deadline)
        date_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
        _fsync_directory(date_dir)
        _check_backup_deadline(deadline)
        tmp_dir.replace(final_dir)
        try:
            _fsync_directory(date_dir)
            _fsync_directory(root)
        except OSError as exc:
            raise BackupCommittedDurabilityUncertain(backup_id, final_dir) from exc
    except D6SqliteError:
        raise
    except OSError as exc:
        raise D6SqliteError("backup_publish_failed", 22) from exc
    finally:
        if source_conn is not None:
            source_conn.close()
        if destination_conn is not None:
            destination_conn.close()

    return BackupResult(backup_id=backup_id, backup_dir=final_dir, manifest=manifest)


def verify_sqlite_backup(
    *,
    backup_dir: Path,
    backend: str,
    production_restore: bool = False,
    expected_source_db: str | None = None,
) -> VerifyResult:
    directory = _require_directory(backup_dir, "backup_dir", writable=False)
    if not (directory / BACKUP_DB_NAME).exists() or not (directory / MANIFEST_NAME).exists():
        raise D6SqliteError("backup_generation_incomplete", 21)
    db_path = _require_regular_file(directory / BACKUP_DB_NAME, "backup_db")
    manifest_path = _require_regular_file(directory / MANIFEST_NAME, "manifest")
    if expected_source_db is not None and Path(expected_source_db).name != expected_source_db:
        raise D6SqliteError("expected_source_db_invalid", 2)
    manifest = _read_manifest(manifest_path)
    _validate_manifest_shape(manifest)
    if production_restore and backend != PRODUCTION_BACKEND:
        raise D6SqliteError("production_backend_invalid", 21)
    if production_restore and manifest["source"]["backend"] != PRODUCTION_BACKEND:
        raise D6SqliteError("production_sqlalchemy_manifest_rejected", 21)
    if manifest["source"]["backend"] != backend:
        raise D6SqliteError("backup_backend_mismatch", 21)
    if expected_source_db and manifest["source"]["db_basename"] != expected_source_db:
        raise D6SqliteError("backup_source_db_mismatch", 21)
    if manifest["artifact"]["generation_dir"] != directory.name:
        raise D6SqliteError("backup_generation_name_mismatch", 21)
    size = db_path.stat().st_size
    digest = sha256_file(db_path)
    if size != manifest["artifact"]["size_bytes"] or digest != manifest["artifact"]["sha256"]:
        raise D6SqliteError("backup_hash_mismatch", 21)

    conn = _connect_readonly(db_path)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        _require_integrity_clean(conn)
        _require_foreign_keys_clean(conn)
        schema = _schema_summary(conn, backend=backend)
    finally:
        conn.close()
    if schema["schema_contract_version"] != manifest["schema"]["schema_contract_version"]:
        raise D6SqliteError("schema_contract_invalid", 21)
    if schema["sqlite_schema_sha256"] != manifest["schema"]["sqlite_schema_sha256"]:
        raise D6SqliteError("schema_fingerprint_mismatch", 21)
    if schema["migration_ids"] != manifest["schema"]["migration_ids"]:
        raise D6SqliteError("schema_migration_mismatch", 21)
    counts = _safe_counts_from_db(db_path)
    if counts != manifest["safe_counts"]:
        raise D6SqliteError("backup_safe_counts_mismatch", 21)
    return VerifyResult(
        backup_id=manifest["backup_id"],
        backup_dir=directory,
        manifest=manifest,
    )


def restore_sqlite_backup(
    *,
    data_root: Path,
    db_name: str,
    backup_dir: Path,
    journal_root: Path,
    expected_backend: str,
    confirm_writers_stopped: bool,
    restore_id: str | None = None,
    resume_restore_id: str | None = None,
    rollback_restore_id: str | None = None,
    fault_hook: FaultHook | None = None,
) -> RestoreResult:
    if not confirm_writers_stopped:
        raise D6SqliteError("writers_stopped_confirmation_missing", 12)
    if expected_backend != PRODUCTION_BACKEND:
        raise D6SqliteError("production_backend_invalid", 21)
    if Path(db_name).name != db_name or not db_name:
        raise D6SqliteError("db_name_invalid", 2)
    if sum(x is not None for x in (restore_id, resume_restore_id, rollback_restore_id)) > 1:
        raise D6SqliteError("restore_mode_conflict", 2)
    for value in (restore_id, resume_restore_id, rollback_restore_id):
        if value is not None and not RESTORE_ID_PATTERN.fullmatch(value):
            raise D6SqliteError("restore_id_invalid", 2)

    root = _require_directory(data_root, "data_root", writable=True)
    backup = verify_sqlite_backup(
        backup_dir=backup_dir,
        backend=expected_backend,
        production_restore=True,
        expected_source_db=db_name,
    )
    journal = _require_or_create_directory(journal_root, "journal_root")
    if not _is_relative_to(journal, root):
        raise D6SqliteError("journal_root_outside_data_root", 10)
    if _is_relative_to(backup.backup_dir, root):
        raise D6SqliteError("backup_inside_data_root", 10)
    live_db = root / db_name
    try:
        with _restore_operation_lock(journal):
            _reject_unexpected_sidecars(root, db_name)

            requested_resume = resume_restore_id or rollback_restore_id
            pointer = _load_active_pointer(journal)
            if requested_resume is None and pointer is not None:
                pointer_journal = _load_journal_by_id(journal, str(pointer["restore_id"]))
                if pointer_journal.get("stage") in {"verified", "rolled_back", "retired"}:
                    _retire_pointer(journal, pointer_journal, fault_hook=fault_hook)
                    pointer = None
                else:
                    raise D6SqliteError("active_restore_exists", 31)
            if requested_resume is not None:
                if pointer is None or pointer["restore_id"] != requested_resume:
                    raise D6SqliteError("active_restore_mismatch", 31)
                current = _load_journal_by_id(journal, requested_resume)
            else:
                rid = restore_id or new_id("rst")
                current = _new_restore_journal(
                    journal_root=journal,
                    restore_id=rid,
                    data_root=root,
                    db_name=db_name,
                    backup=backup,
                )
                _save_journal(journal, current, fault_hook=fault_hook, event="journal_started")
                _save_pointer(journal, current, fault_hook=fault_hook, event="pointer_started")
            if rollback_restore_id is not None:
                return _rollback_restore(
                    journal_root=journal,
                    journal=current,
                    fault_hook=fault_hook,
                )
            if current.get("stage") not in {
                "old_quarantined",
                "candidate_installed",
                "rollback_in_progress",
            }:
                _begin_immediate_preflight(live_db)

            return _resume_restore(
                journal_root=journal,
                journal=current,
                backup=backup,
                fault_hook=fault_hook,
            )
    except D6SqliteError:
        raise
    except FileNotFoundError as exc:
        raise D6SqliteError("restore_file_missing", 31) from exc
    except OSError as exc:
        raise D6SqliteError("restore_filesystem_failed", 31) from exc


def _resume_restore(
    *,
    journal_root: Path,
    journal: dict[str, Any],
    backup: VerifyResult,
    fault_hook: FaultHook | None,
) -> RestoreResult:
    stage = str(journal["stage"])
    if stage == "started":
        _prepare_candidate(journal, backup, fault_hook=fault_hook)
        _save_journal(journal_root, journal, fault_hook=fault_hook, event="journal_prepared")
        _save_pointer(journal_root, journal, fault_hook=fault_hook, event="pointer_prepared")
        stage = "prepared"
    if stage == "prepared":
        _freeze_old_set(journal, fault_hook=fault_hook)
        _save_journal(journal_root, journal, fault_hook=fault_hook, event="journal_inventory")
        _save_pointer(journal_root, journal, fault_hook=fault_hook, event="pointer_inventory")
        stage = "old_quarantine_in_progress"
    if stage == "old_quarantine_in_progress":
        _move_old_set_to_quarantine(journal, fault_hook=fault_hook)
        _save_pointer(journal_root, journal, fault_hook=fault_hook, event="pointer_old_quarantined")
        stage = str(journal["stage"])
    if stage == "old_quarantined":
        _install_candidate(
            journal,
            manifest=backup.manifest,
            journal_root=journal_root,
            fault_hook=fault_hook,
        )
        _save_journal(
            journal_root,
            journal,
            fault_hook=fault_hook,
            event="journal_candidate_installed",
        )
        _save_pointer(
            journal_root,
            journal,
            fault_hook=fault_hook,
            event="pointer_candidate_installed",
        )
        stage = "candidate_installed"
    if stage == "candidate_installed":
        live_db = Path(journal["paths"]["data_root"]) / journal["paths"]["db_name"]
        try:
            _verify_installed_candidate(live_db, backup.manifest)
        except D6SqliteError:
            _rollback_restore(journal_root=journal_root, journal=journal, fault_hook=fault_hook)
            raise D6SqliteError("candidate_verify_failed_rolled_back", 32)
        journal["candidate"]["installed_sha256"] = sha256_file(live_db)
        _set_stage(journal, "verified")
        _save_journal(journal_root, journal, fault_hook=fault_hook, event="journal_verified")
        _save_pointer(journal_root, journal, fault_hook=fault_hook, event="pointer_verified")
    if journal["stage"] == "rollback_in_progress":
        return _rollback_restore(
            journal_root=journal_root,
            journal=journal,
            fault_hook=fault_hook,
        )
    if journal["stage"] == "verified":
        _retire_pointer(journal_root, journal, fault_hook=fault_hook)
    return RestoreResult(
        restore_id=journal["restore_id"],
        stage=journal["stage"],
        journal_dir=_journal_dir(journal_root, journal["restore_id"]),
    )


def _rollback_restore(
    *,
    journal_root: Path,
    journal: dict[str, Any],
    fault_hook: FaultHook | None,
) -> RestoreResult:
    live_db = Path(journal["paths"]["data_root"]) / journal["paths"]["db_name"]
    stage = str(journal["stage"])
    if stage in {"started", "prepared", "old_quarantine_in_progress"}:
        raise D6SqliteError("rollback_stage_not_safe", 31)
    candidate_publication_possible = stage in {
        "candidate_installed",
        "rollback_in_progress",
        "verified",
    }
    if journal["stage"] != "rollback_in_progress":
        _set_stage(journal, "rollback_in_progress")
        _save_journal(
            journal_root,
            journal,
            fault_hook=fault_hook,
            event="journal_rollback_started",
        )
        _save_pointer(
            journal_root,
            journal,
            fault_hook=fault_hook,
            event="pointer_rollback_started",
        )
    failed_dir = (
        _journal_dir(journal_root, journal["restore_id"])
        / "quarantine"
        / "failed_candidate"
    )
    failed_dir.mkdir(parents=True, exist_ok=True)
    install_tmp = _candidate_install_tmp_path(journal)
    if install_tmp.exists() and candidate_publication_possible:
        target = failed_dir / install_tmp.name
        if target.exists() and sha256_file(target) != sha256_file(install_tmp):
            raise D6SqliteError("failed_candidate_duplicate", 33)
        if not target.exists():
            install_tmp.replace(target)
            _fsync_directory(install_tmp.parent)
            _fsync_directory(target.parent)
    for path in _db_set_paths(live_db):
        if path.exists():
            if _path_is_restored_old_copy(journal, path):
                continue
            if not candidate_publication_possible:
                continue
            target = failed_dir / path.name
            if target.exists() and sha256_file(target) != sha256_file(path):
                raise D6SqliteError("failed_candidate_duplicate", 33)
            if not target.exists():
                path.replace(target)
                _fsync_directory(path.parent)
                _fsync_directory(target.parent)
    for entry in journal.get("old_set", []):
        if not entry["expected_present"]:
            continue
        source = Path(entry["quarantine_path"])
        target = Path(entry["live_path"])
        if (
            entry.get("state") == "rollback_completed"
            and target.exists()
            and sha256_file(target) == entry["sha256"]
        ):
            continue
        if not source.exists() or sha256_file(source) != entry["sha256"]:
            raise D6SqliteError("rollback_old_set_invalid", 33)
        _move_with_journal(
            journal_root=journal_root,
            journal=journal,
            entry=entry,
            source=source,
            target=target,
            intent="rollback_quarantine_to_live",
            completed_state="rollback_completed",
            fault_hook=fault_hook,
        )
    _fsync_directory(live_db.parent)
    _verify_current_db_readonly(live_db)
    _set_stage(journal, "rolled_back")
    _save_journal(journal_root, journal, fault_hook=fault_hook, event="journal_rolled_back")
    _save_pointer(journal_root, journal, fault_hook=fault_hook, event="pointer_rolled_back")
    _retire_pointer(journal_root, journal, fault_hook=fault_hook)
    return RestoreResult(
        restore_id=journal["restore_id"],
        stage=journal["stage"],
        journal_dir=_journal_dir(journal_root, journal["restore_id"]),
    )


def _path_is_restored_old_copy(journal: dict[str, Any], path: Path) -> bool:
    for entry in journal.get("old_set", []):
        if (
            entry.get("state") == "rollback_completed"
            and Path(entry["live_path"]) == path
            and path.exists()
            and sha256_file(path) == entry["sha256"]
        ):
            return True
    return False


def _prepare_candidate(
    journal: dict[str, Any],
    backup: VerifyResult,
    *,
    fault_hook: FaultHook | None,
) -> None:
    prepared_dir = Path(journal["paths"]["prepared_dir"])
    prepared_dir.mkdir(parents=True, exist_ok=True)
    candidate = prepared_dir / "candidate.db"
    if not candidate.exists():
        shutil.copyfile(backup.backup_dir / BACKUP_DB_NAME, candidate)
        _fsync_file(candidate)
        _fsync_directory(prepared_dir)
        _call_fault(fault_hook, "candidate_copied")
    if sha256_file(candidate) != backup.manifest["artifact"]["sha256"]:
        raise D6SqliteError("candidate_digest_mismatch", 21)
    _verify_installed_candidate(candidate, backup.manifest)
    journal["candidate"]["prepared_sha256"] = sha256_file(candidate)
    _set_stage(journal, "prepared")


def _freeze_old_set(journal: dict[str, Any], *, fault_hook: FaultHook | None) -> None:
    data_root = Path(journal["paths"]["data_root"])
    db_name = journal["paths"]["db_name"]
    quarantine = Path(journal["paths"]["quarantine_dir"])
    quarantine.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for suffix in RESTORE_SIDECAR_SUFFIXES:
        live = data_root / f"{db_name}{suffix}"
        quarantine_path = quarantine / live.name
        present = live.exists()
        if present:
            if live.is_symlink() or not live.is_file():
                raise D6SqliteError("old_set_file_invalid", 10)
            digest = sha256_file(live)
            size = live.stat().st_size
            state = "live"
        else:
            digest = None
            size = None
            state = "absent"
        entries.append(
            {
                "name": live.name,
                "expected_present": present,
                "size_bytes": size,
                "sha256": digest,
                "live_path": str(live),
                "quarantine_path": str(quarantine_path),
                "state": state,
                "last_intent": None,
                "completed_at_utc": None,
            }
        )
    journal["old_set"] = entries
    _set_stage(journal, "old_quarantine_in_progress")
    _call_fault(fault_hook, "old_set_inventory_frozen")


def _move_old_set_to_quarantine(
    journal: dict[str, Any],
    *,
    fault_hook: FaultHook | None,
) -> None:
    _reconcile_old_set(journal, allow_live=True)
    for entry in journal["old_set"]:
        if not entry["expected_present"]:
            continue
        live = Path(entry["live_path"])
        quarantine = Path(entry["quarantine_path"])
        if quarantine.exists() and sha256_file(quarantine) == entry["sha256"]:
            entry["state"] = "move_completed"
            continue
        if not live.exists():
            raise D6SqliteError("restore_file_missing", 31)
        _move_with_journal(
            journal_root=_journal_root_from_journal(journal),
            journal=journal,
            entry=entry,
            source=live,
            target=quarantine,
            intent="move_live_to_quarantine",
            completed_state="move_completed",
            fault_hook=fault_hook,
        )
    _reconcile_old_set(journal, allow_live=False)
    _set_stage(journal, "old_quarantined")
    _save_journal(
        _journal_root_from_journal(journal),
        journal,
        fault_hook=fault_hook,
        event="journal_old_quarantined",
    )


def _move_with_journal(
    *,
    journal_root: Path,
    journal: dict[str, Any],
    entry: dict[str, Any],
    source: Path,
    target: Path,
    intent: str,
    completed_state: str,
    fault_hook: FaultHook | None,
) -> None:
    entry["last_intent"] = intent
    _save_journal(journal_root, journal, fault_hook=fault_hook, event=f"intent_{entry['name']}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise D6SqliteError("restore_file_duplicate", 31)
    source.replace(target)
    _fsync_directory(source.parent)
    _call_fault(fault_hook, f"renamed_{entry['name']}")
    _fsync_directory(target.parent)
    _call_fault(fault_hook, f"fsynced_rename_{entry['name']}")
    entry["state"] = completed_state
    entry["completed_at_utc"] = _utc_now_z()
    _save_journal(journal_root, journal, fault_hook=fault_hook, event=f"completed_{entry['name']}")


def _install_candidate(
    journal: dict[str, Any],
    *,
    manifest: dict[str, Any],
    journal_root: Path,
    fault_hook: FaultHook | None,
) -> None:
    live_db = Path(journal["paths"]["data_root"]) / journal["paths"]["db_name"]
    candidate = Path(journal["paths"]["prepared_dir"]) / "candidate.db"
    if not candidate.exists():
        raise D6SqliteError("candidate_missing", 31)
    expected_sha256 = str(manifest["artifact"]["sha256"])
    if sha256_file(candidate) != expected_sha256:
        raise D6SqliteError("candidate_digest_mismatch", 21)
    live_sidecars = [Path(str(live_db) + "-wal"), Path(str(live_db) + "-shm")]
    for sidecar in live_sidecars:
        if sidecar.exists():
            raise D6SqliteError("old_sidecar_still_live", 31)
    install_tmp = _candidate_install_tmp_path(journal)
    journal["candidate"]["install_intent"] = {
        "source_path": str(candidate),
        "tmp_path": str(install_tmp),
        "target_path": str(live_db),
        "sha256": expected_sha256,
    }
    _save_journal(
        journal_root,
        journal,
        fault_hook=fault_hook,
        event="candidate_install_intent",
    )
    if live_db.exists():
        if sha256_file(live_db) != expected_sha256:
            raise D6SqliteError("live_candidate_mismatch", 31)
        if install_tmp.exists():
            install_tmp.unlink()
            _fsync_directory(install_tmp.parent)
        journal["candidate"]["installed_sha256"] = expected_sha256
        _set_stage(journal, "candidate_installed")
        return
    if install_tmp.exists() and sha256_file(install_tmp) != expected_sha256:
        install_tmp.unlink()
        _fsync_directory(install_tmp.parent)
    if not install_tmp.exists():
        shutil.copyfile(candidate, install_tmp)
        _fsync_file(install_tmp)
        _fsync_directory(install_tmp.parent)
    _call_fault(fault_hook, "candidate_temp_prepared")
    install_tmp.replace(live_db)
    _call_fault(fault_hook, "candidate_published")
    _fsync_file(live_db)
    _fsync_directory(live_db.parent)
    _call_fault(fault_hook, "candidate_publish_fsynced")
    journal["candidate"]["installed_sha256"] = expected_sha256
    _set_stage(journal, "candidate_installed")


def _verify_installed_candidate(path: Path, manifest: dict[str, Any]) -> None:
    if sha256_file(path) != manifest["artifact"]["sha256"]:
        raise D6SqliteError("candidate_digest_mismatch", 21)
    conn = _connect_readonly(path)
    try:
        _require_integrity_clean(conn)
        _require_foreign_keys_clean(conn)
        schema = _schema_summary(conn, backend=manifest["source"]["backend"])
    finally:
        conn.close()
    if schema["schema_contract_version"] != manifest["schema"]["schema_contract_version"]:
        raise D6SqliteError("schema_contract_invalid", 21)


def _verify_current_db_readonly(path: Path) -> None:
    conn = _connect_readonly(path)
    try:
        _require_integrity_clean(conn)
        _require_foreign_keys_clean(conn)
    finally:
        conn.close()


def _reconcile_old_set(journal: dict[str, Any], *, allow_live: bool) -> None:
    for entry in journal.get("old_set", []):
        live = Path(entry["live_path"])
        quarantine = Path(entry["quarantine_path"])
        live_ok = (
            live.exists()
            and entry["expected_present"]
            and sha256_file(live) == entry["sha256"]
        )
        quarantine_ok = (
            quarantine.exists()
            and entry["expected_present"]
            and sha256_file(quarantine) == entry["sha256"]
        )
        if not entry["expected_present"]:
            if live.exists() or quarantine.exists():
                raise D6SqliteError("restore_unexpected_sidecar_copy", 31)
            continue
        if live_ok and quarantine_ok:
            raise D6SqliteError("restore_file_duplicate", 31)
        if not live_ok and not quarantine_ok:
            raise D6SqliteError("restore_file_missing", 31)
        if live_ok and not allow_live:
            raise D6SqliteError("restore_file_still_live", 31)
        if quarantine_ok:
            entry["state"] = "move_completed"


def _new_restore_journal(
    *,
    journal_root: Path,
    restore_id: str,
    data_root: Path,
    db_name: str,
    backup: VerifyResult,
) -> dict[str, Any]:
    journal_dir = _journal_dir(journal_root, restore_id)
    if journal_dir.exists():
        raise D6SqliteError("restore_id_exists", 31)
    prepared = journal_dir / "prepared"
    quarantine = journal_dir / "quarantine" / "old"
    prepared.mkdir(parents=True, exist_ok=False)
    quarantine.mkdir(parents=True, exist_ok=True)
    return {
        "format_version": RESTORE_JOURNAL_VERSION,
        "restore_id": restore_id,
        "started_at_utc": _utc_now_z(),
        "updated_at_utc": _utc_now_z(),
        "stage": "started",
        "backup": {
            "backup_id": backup.backup_id,
            "generation_dir": backup.backup_dir.name,
            "manifest_sha256": sha256_file(backup.backup_dir / MANIFEST_NAME),
            "artifact_sha256": backup.manifest["artifact"]["sha256"],
            "backend": backup.manifest["source"]["backend"],
            "ops_image_digest": backup.manifest["deployment"]["ops_image_digest"],
            "schema_contract_version": backup.manifest["schema"]["schema_contract_version"],
        },
        "paths": {
            "data_root": str(data_root),
            "db_name": db_name,
            "journal_root": str(journal_root),
            "journal_dir": str(journal_dir),
            "prepared_dir": str(prepared),
            "quarantine_dir": str(quarantine),
        },
        "old_set": [],
        "candidate": {"prepared_sha256": None, "installed_sha256": None},
        "history": [{"stage": "started", "at_utc": _utc_now_z()}],
        "failure": None,
    }


def _set_stage(journal: dict[str, Any], stage: str) -> None:
    journal["stage"] = stage
    journal["updated_at_utc"] = _utc_now_z()
    journal.setdefault("history", []).append({"stage": stage, "at_utc": journal["updated_at_utc"]})


def _save_journal(
    journal_root: Path,
    journal: dict[str, Any],
    *,
    fault_hook: FaultHook | None,
    event: str,
) -> None:
    path = _journal_dir(journal_root, journal["restore_id"]) / "journal.json"
    _write_json_atomic(path, journal)
    _fsync_directory(path.parent)
    _call_fault(fault_hook, event)


def _save_pointer(
    journal_root: Path,
    journal: dict[str, Any],
    *,
    fault_hook: FaultHook | None,
    event: str,
) -> None:
    pointer = {
        "format_version": RESTORE_POINTER_VERSION,
        "restore_id": journal["restore_id"],
        "journal_path": f"journals/{journal['restore_id']}/journal.json",
        "stage": journal["stage"],
        "updated_at_utc": _utc_now_z(),
    }
    _write_json_atomic(_pointer_path(journal_root), pointer)
    _fsync_directory(journal_root)
    _call_fault(fault_hook, event)


def _retire_pointer(
    journal_root: Path,
    journal: dict[str, Any],
    *,
    fault_hook: FaultHook | None,
) -> None:
    history_dir = journal_root / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    terminal = history_dir / f"{journal['restore_id']}.terminal.json"
    _write_json_atomic(
        terminal,
        {
            "format_version": "aemeath.sqlite_restore.terminal.v1",
            "restore_id": journal["restore_id"],
            "stage": journal["stage"],
            "backup": journal["backup"],
            "completed_at_utc": _utc_now_z(),
        },
    )
    _fsync_directory(history_dir)
    _call_fault(fault_hook, "terminal_history_written")
    _set_stage(journal, "retired")
    _save_journal(journal_root, journal, fault_hook=fault_hook, event="journal_retired")
    pointer = _pointer_path(journal_root)
    if pointer.exists():
        pointer.unlink()
        _fsync_directory(journal_root)
    _call_fault(fault_hook, "pointer_retired")


def _load_active_pointer(journal_root: Path) -> dict[str, Any] | None:
    path = _pointer_path(journal_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D6SqliteError("active_pointer_invalid", 31) from exc
    if payload.get("format_version") != RESTORE_POINTER_VERSION or "restore_id" not in payload:
        raise D6SqliteError("active_pointer_invalid", 31)
    return payload


def _load_journal_by_id(journal_root: Path, restore_id: str) -> dict[str, Any]:
    path = _journal_dir(journal_root, restore_id) / "journal.json"
    if not path.exists():
        raise D6SqliteError("restore_journal_missing", 31)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D6SqliteError("restore_journal_invalid", 31) from exc
    if (
        payload.get("format_version") != RESTORE_JOURNAL_VERSION
        or payload.get("restore_id") != restore_id
    ):
        raise D6SqliteError("restore_journal_invalid", 31)
    if "journal_root" not in payload["paths"]:
        payload["paths"]["journal_root"] = str(journal_root)
    return payload


def _build_manifest(
    *,
    db_path: Path,
    backup_db: Path,
    generation_name: str,
    backup_id: str,
    label: str,
    backend: str,
    created_at: datetime,
    source_journal_mode: str,
    app_image_digest: str,
    ops_image_digest: str,
    deploy_lock_sha256: str,
    compose_config_sha256: str,
    git_commit: str,
    compose_project: str,
) -> dict[str, Any]:
    conn = _connect_readonly(backup_db)
    try:
        sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        schema = _schema_summary(conn, backend=backend)
    finally:
        conn.close()
    return {
        "format_version": BACKUP_MANIFEST_VERSION,
        "backup_id": backup_id,
        "created_at_utc": _utc_z(created_at),
        "label": label,
        "source": {
            "db_basename": db_path.name,
            "db_path_sha256": hashlib.sha256(str(db_path).encode("utf-8")).hexdigest(),
            "backend": backend,
            "compose_project": compose_project,
        },
        "deployment": {
            "git_commit": git_commit,
            "app_image_digest": app_image_digest,
            "ops_image_digest": ops_image_digest,
            "deploy_lock_sha256": deploy_lock_sha256,
            "compose_config_sha256": compose_config_sha256,
        },
        "artifact": {
            "generation_dir": generation_name,
            "db_filename": BACKUP_DB_NAME,
            "manifest_filename": MANIFEST_NAME,
            "size_bytes": backup_db.stat().st_size,
            "sha256": sha256_file(backup_db),
            "page_size": page_size,
            "page_count": page_count,
        },
        "sqlite": {
            "sqlite_version": sqlite_version,
            "application_id": application_id,
            "user_version": user_version,
            "journal_mode_seen_on_source": source_journal_mode,
            "checkpoint": {"mode": "none", "result": "not_attempted"},
            "integrity_check": "ok",
            "foreign_key_check_rows": 0,
        },
        "schema": schema,
        "safe_counts": _safe_counts_from_db(backup_db),
        "offsite_interface": "documented_only",
    }


def _schema_summary(connection: sqlite3.Connection, *, backend: str) -> dict[str, Any]:
    if backend not in {"sqlite_raw", "sqlalchemy"}:
        raise D6SqliteError("backend_invalid", 2)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    required = {"accounts", "schema_migrations", "sessions", "messages"}
    if not required <= tables:
        raise D6SqliteError("schema_required_table_missing", 21)
    migration_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    required_markers = {
        BROWSER_AUTH_MIGRATION_ID,
        MEMORY_SCOPE_MIGRATION_VERSION,
        MEMORY_EMBEDDING_ARTIFACT_MIGRATION_VERSION,
        MEMORY_EXACT_ACTIVE_IDENTITY_MIGRATION_VERSION,
        STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,
    }
    if not required_markers <= set(migration_ids):
        raise D6SqliteError("schema_migration_missing", 21)
    browser_fingerprint = validate_browser_auth_schema_readonly(connection)
    validate_streaming_chat_schema_readonly(connection)
    index_list_rows = connection.execute("PRAGMA index_list('long_term_memories')").fetchall()
    index_xinfo_rows = connection.execute(
        f"PRAGMA index_xinfo('{ACTIVE_MEMORY_IDENTITY_INDEX}')"
    ).fetchall()
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?",
        (ACTIVE_MEMORY_IDENTITY_INDEX,),
    ).fetchone()
    if not has_valid_active_memory_identity_index(
        index_list_rows=index_list_rows,
        index_xinfo_rows=index_xinfo_rows,
        index_sql=index_sql[0] if index_sql else None,
    ):
        raise D6SqliteError("active_memory_identity_index_invalid", 21)
    return {
        "schema_contract_version": SCHEMA_CONTRACT_VERSION,
        "sqlite_schema_sha256": _sqlite_schema_fingerprint(connection),
        "migration_ids": migration_ids,
        "browser_auth_schema_revision": BROWSER_AUTH_SCHEMA_REVISION,
        "browser_auth_contract_fingerprint": browser_fingerprint,
        "streaming_chat_contract": STREAMING_CHAT_IDEMPOTENCY_MIGRATION_ID,
        "active_memory_identity_index": "valid",
    }


def _safe_counts_from_db(db_path: Path) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return {
            "accounts_total": _count(conn, "accounts"),
            "sessions_by_status": _status_counts(
                conn,
                "sessions",
                "status",
                ("active", "ended", "archived"),
            ),
            "messages_total": _count(conn, "messages"),
            "chat_requests_by_status": _status_counts(
                conn,
                "chat_request_idempotency",
                "status",
                ("in_progress", "completed", "failed"),
            ),
            "memories_by_status": _status_counts(
                conn,
                "long_term_memories",
                "status",
                ("active", "archived", "disabled"),
            ),
            "memory_jobs_by_status": _status_counts(
                conn,
                "memory_extraction_jobs",
                "status",
                ("pending", "running", "completed", "failed"),
            ),
            "browser_auth_sessions_total": _count(conn, "web_auth_sessions"),
            "relationship_states_total": _count(conn, "relationship_states"),
        }
    finally:
        conn.close()


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _status_counts(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    statuses: tuple[str, ...],
) -> dict[str, int]:
    counts = {status: 0 for status in statuses}
    for status, count in connection.execute(
        f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"
    ).fetchall():
        key = str(status)
        if key in counts:
            counts[key] = int(count)
    return counts


def _sqlite_schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%'
        ORDER BY type, name
        """
    ).fetchall()
    payload = "\n".join(
        "|".join(
            (
                str(row[0]).lower(),
                str(row[1]).lower(),
                str(row[2]).lower(),
                _normalize_sql(str(row[3])),
            )
        )
        for row in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split()).lower()


def _validate_manifest_shape(manifest: dict[str, Any]) -> None:
    if manifest.get("format_version") != BACKUP_MANIFEST_VERSION:
        raise D6SqliteError("manifest_format_invalid", 21)
    required = {"backup_id", "source", "deployment", "artifact", "sqlite", "schema", "safe_counts"}
    if not required <= set(manifest):
        raise D6SqliteError("manifest_missing_fields", 21)
    nested_required = {
        "source": {"db_basename", "db_path_sha256", "backend", "compose_project"},
        "deployment": {
            "git_commit",
            "app_image_digest",
            "ops_image_digest",
            "deploy_lock_sha256",
            "compose_config_sha256",
        },
        "artifact": {
            "generation_dir",
            "db_filename",
            "manifest_filename",
            "size_bytes",
            "sha256",
            "page_size",
            "page_count",
        },
        "sqlite": {
            "sqlite_version",
            "application_id",
            "user_version",
            "journal_mode_seen_on_source",
            "checkpoint",
            "integrity_check",
            "foreign_key_check_rows",
        },
        "schema": {
            "schema_contract_version",
            "sqlite_schema_sha256",
            "migration_ids",
            "browser_auth_schema_revision",
            "browser_auth_contract_fingerprint",
            "streaming_chat_contract",
            "active_memory_identity_index",
        },
    }
    for section, keys in nested_required.items():
        payload = manifest.get(section)
        if not isinstance(payload, dict) or not keys <= set(payload):
            raise D6SqliteError("manifest_missing_fields", 21)
    if "offsite" in manifest:
        raise D6SqliteError("manifest_mutable_offsite_invalid", 21)
    if manifest.get("offsite_interface") != "documented_only":
        raise D6SqliteError("manifest_offsite_interface_invalid", 21)
    if manifest["schema"].get("schema_contract_version") != SCHEMA_CONTRACT_VERSION:
        raise D6SqliteError("schema_contract_invalid", 21)
    _validate_backup_identity(
        backend=manifest["source"]["backend"],
        app_image_digest=manifest["deployment"]["app_image_digest"],
        ops_image_digest=manifest["deployment"]["ops_image_digest"],
        deploy_lock_sha256=manifest["deployment"]["deploy_lock_sha256"],
        compose_config_sha256=manifest["deployment"]["compose_config_sha256"],
        git_commit=manifest["deployment"]["git_commit"],
        reason="manifest_deployment_identity_invalid",
        exit_code=21,
    )
    if manifest["artifact"].get("db_filename") != BACKUP_DB_NAME:
        raise D6SqliteError("manifest_artifact_invalid", 21)
    if manifest["artifact"].get("manifest_filename") != MANIFEST_NAME:
        raise D6SqliteError("manifest_artifact_invalid", 21)
    if manifest["sqlite"].get("integrity_check") != "ok":
        raise D6SqliteError("manifest_sqlite_invalid", 21)
    if manifest["sqlite"].get("foreign_key_check_rows") != 0:
        raise D6SqliteError("manifest_sqlite_invalid", 21)
    if not isinstance(manifest.get("safe_counts"), dict):
        raise D6SqliteError("manifest_missing_fields", 21)


def _validate_backup_identity(
    *,
    backend: str,
    app_image_digest: str,
    ops_image_digest: str,
    deploy_lock_sha256: str,
    compose_config_sha256: str,
    git_commit: str,
    reason: str,
    exit_code: int,
) -> None:
    if backend != PRODUCTION_BACKEND:
        return
    values = (
        app_image_digest,
        ops_image_digest,
        deploy_lock_sha256,
        compose_config_sha256,
        git_commit,
    )
    if any(not isinstance(value, str) or value.strip() in {"", "unknown"} for value in values):
        raise D6SqliteError(reason, exit_code)
    if not IMAGE_DIGEST_PATTERN.fullmatch(app_image_digest):
        raise D6SqliteError(reason, exit_code)
    if not IMAGE_DIGEST_PATTERN.fullmatch(ops_image_digest):
        raise D6SqliteError(reason, exit_code)
    if not SHA256_HEX_PATTERN.fullmatch(deploy_lock_sha256):
        raise D6SqliteError(reason, exit_code)
    if not SHA256_HEX_PATTERN.fullmatch(compose_config_sha256):
        raise D6SqliteError(reason, exit_code)
    if not GIT_COMMIT_PATTERN.fullmatch(git_commit):
        raise D6SqliteError(reason, exit_code)


def _check_backup_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise BackupDeadlineExceeded()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D6SqliteError("manifest_invalid", 21) from exc
    if not isinstance(payload, dict):
        raise D6SqliteError("manifest_invalid", 21)
    return payload


def _source_journal_mode(connection: sqlite3.Connection) -> str:
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    except sqlite3.Error:
        return "unknown"


def _require_integrity_clean(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        raise D6SqliteError("sqlite_integrity_failed", 21)


def _require_foreign_keys_clean(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise D6SqliteError("foreign_key_violation", 21)


def _connect_readonly(path: Path, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    try:
        uri = f"{path.resolve(strict=True).as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise D6SqliteError("foreign_keys_not_enabled", 20)
        return connection
    except D6SqliteError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise D6SqliteError("sqlite_open_failed", 20) from exc


def _begin_immediate_preflight(path: Path) -> None:
    if not path.exists():
        return
    connection = sqlite3.connect(path, timeout=1.0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    except sqlite3.Error as exc:
        raise D6SqliteError("current_db_writer_lock_failed", 20) from exc
    finally:
        connection.close()


def _reject_unexpected_sidecars(root: Path, db_name: str) -> None:
    allowed = {f"{db_name}{suffix}" for suffix in RESTORE_SIDECAR_SUFFIXES}
    allowed.add(".restore-journal")
    unexpected = [
        path.name
        for path in root.iterdir()
        if path.name.startswith(db_name) and path.name not in allowed
    ]
    if unexpected:
        raise D6SqliteError("unexpected_sidecar", 10)


def _db_set_paths(live_db: Path) -> list[Path]:
    return [Path(f"{live_db}{suffix}") for suffix in RESTORE_SIDECAR_SUFFIXES]


def _candidate_install_tmp_path(journal: dict[str, Any]) -> Path:
    live_db = Path(journal["paths"]["data_root"]) / journal["paths"]["db_name"]
    return live_db.with_name(f".{live_db.name}.{journal['restore_id']}.candidate.tmp")


@contextmanager
def _restore_operation_lock(journal_root: Path) -> Iterator[None]:
    lock_path = journal_root / "active.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            _acquire_restore_lock(handle)
            try:
                yield
            finally:
                _release_restore_lock(handle)
    except D6SqliteError:
        raise
    except OSError as exc:
        raise D6SqliteError("restore_lock_unavailable", 31) from exc


def _acquire_restore_lock(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise D6SqliteError("restore_in_progress", 31) from exc


def _release_restore_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _require_regular_file(path: Path, name: str) -> Path:
    _reject_symlink_components(path, name)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise D6SqliteError(f"{name}_unavailable", 10) from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise D6SqliteError(f"{name}_not_regular", 10)
    return resolved


def _require_directory(path: Path, name: str, *, writable: bool) -> Path:
    _reject_symlink_components(path, name)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise D6SqliteError(f"{name}_unavailable", 10) from exc
    if not resolved.is_dir():
        raise D6SqliteError(f"{name}_not_directory", 10)
    if writable and not os.access(resolved, os.W_OK | os.R_OK | os.X_OK):
        raise D6SqliteError(f"{name}_permission_denied", 12)
    if not writable and not os.access(resolved, os.R_OK | os.X_OK):
        raise D6SqliteError(f"{name}_permission_denied", 12)
    return resolved


def _require_or_create_directory(path: Path, name: str) -> Path:
    _reject_symlink_components(path, name)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise D6SqliteError(f"{name}_unavailable", 10) from exc
    return _require_directory(path, name, writable=True)


def _reject_symlink_components(path: Path, name: str) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise D6SqliteError(f"{name}_symlink_rejected", 10)
        except OSError as exc:
            raise D6SqliteError(f"{name}_unavailable", 10) from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{new_id('tmp')}")
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    _fsync_directory(path.parent)


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp_for_name(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now_z() -> str:
    return _utc_z(datetime.now(timezone.utc))


def _pointer_path(journal_root: Path) -> Path:
    return journal_root / "active.pointer.json"


def _journal_dir(journal_root: Path, restore_id: str) -> Path:
    return journal_root / "journals" / restore_id


def _journal_root_from_journal(journal: dict[str, Any]) -> Path:
    return Path(journal["paths"]["journal_root"])


def _call_fault(fault_hook: FaultHook | None, event: str) -> None:
    if fault_hook is not None:
        fault_hook(event)
