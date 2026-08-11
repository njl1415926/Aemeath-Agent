from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class LogRetentionPolicy:
    retention_days: int
    max_total_bytes: int
    active_grace_seconds: int = 3600

    def validate(self) -> None:
        if self.retention_days < 1:
            raise ValueError("retention_days_invalid")
        if self.max_total_bytes < 1:
            raise ValueError("max_total_bytes_invalid")
        if self.active_grace_seconds < 60:
            raise ValueError("active_grace_seconds_invalid")


@dataclass(frozen=True)
class LogRetentionEntry:
    path: Path
    size_bytes: int
    mtime_ns: int
    reason: str


@dataclass(frozen=True)
class LogRetentionPlan:
    root: Path
    entries: tuple[LogRetentionEntry, ...]
    scanned_file_count: int
    skipped_symlink_count: int
    total_bytes_before: int
    projected_bytes_after: int
    target_satisfied: bool


@dataclass(frozen=True)
class LogRetentionResult:
    planned_file_count: int
    deleted_file_count: int
    skipped_changed_file_count: int
    deleted_bytes: int
    remaining_candidate_count: int
    total_bytes_after: int
    target_satisfied: bool


def plan_jsonl_retention(
    *,
    root: Path,
    policy: LogRetentionPolicy,
    now: datetime | None = None,
) -> LogRetentionPlan:
    policy.validate()
    resolved_root = _validate_root(root)
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        raise ValueError("retention_now_must_be_timezone_aware")
    now_timestamp = observed_now.timestamp()
    age_cutoff = now_timestamp - policy.retention_days * 86400
    active_cutoff = now_timestamp - policy.active_grace_seconds

    files, skipped_symlinks = _scan_jsonl_files(resolved_root)
    total_bytes = sum(item.size_bytes for item in files)
    selected: dict[Path, LogRetentionEntry] = {}

    for item in files:
        mtime = item.mtime_ns / 1_000_000_000
        if mtime < age_cutoff and mtime < active_cutoff:
            selected[item.path] = LogRetentionEntry(
                path=item.path,
                size_bytes=item.size_bytes,
                mtime_ns=item.mtime_ns,
                reason="age",
            )

    projected_bytes = total_bytes - sum(item.size_bytes for item in selected.values())
    if projected_bytes > policy.max_total_bytes:
        oldest_first = sorted(files, key=lambda item: (item.mtime_ns, item.path.as_posix()))
        for item in oldest_first:
            if projected_bytes <= policy.max_total_bytes:
                break
            if item.path in selected:
                continue
            if item.mtime_ns / 1_000_000_000 >= active_cutoff:
                continue
            selected[item.path] = LogRetentionEntry(
                path=item.path,
                size_bytes=item.size_bytes,
                mtime_ns=item.mtime_ns,
                reason="size",
            )
            projected_bytes -= item.size_bytes

    entries = tuple(
        sorted(selected.values(), key=lambda item: (item.mtime_ns, item.path.as_posix()))
    )
    return LogRetentionPlan(
        root=resolved_root,
        entries=entries,
        scanned_file_count=len(files),
        skipped_symlink_count=skipped_symlinks,
        total_bytes_before=total_bytes,
        projected_bytes_after=projected_bytes,
        target_satisfied=projected_bytes <= policy.max_total_bytes,
    )


def apply_jsonl_retention(
    *,
    plan: LogRetentionPlan,
    policy: LogRetentionPolicy,
    now: datetime | None = None,
) -> LogRetentionResult:
    deleted_count = 0
    skipped_changed = 0
    deleted_bytes = 0
    for entry in plan.entries:
        if not _entry_is_unchanged(plan.root, entry):
            skipped_changed += 1
            continue
        entry.path.unlink()
        deleted_count += 1
        deleted_bytes += entry.size_bytes

    remaining = plan_jsonl_retention(root=plan.root, policy=policy, now=now)
    return LogRetentionResult(
        planned_file_count=len(plan.entries),
        deleted_file_count=deleted_count,
        skipped_changed_file_count=skipped_changed,
        deleted_bytes=deleted_bytes,
        remaining_candidate_count=len(remaining.entries),
        total_bytes_after=remaining.total_bytes_before,
        target_satisfied=remaining.target_satisfied and not remaining.entries,
    )


def _validate_root(root: Path) -> Path:
    if not root.is_absolute():
        raise ValueError("log_root_must_be_absolute")
    if root.is_symlink():
        raise ValueError("log_root_symlink_rejected")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("log_root_not_directory")
    return resolved


@dataclass(frozen=True)
class _ObservedFile:
    path: Path
    size_bytes: int
    mtime_ns: int


def _scan_jsonl_files(root: Path) -> tuple[list[_ObservedFile], int]:
    files: list[_ObservedFile] = []
    skipped_symlinks = 0
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        safe_directories = []
        for name in directory_names:
            candidate = current / name
            if candidate.is_symlink():
                skipped_symlinks += 1
            else:
                safe_directories.append(name)
        directory_names[:] = safe_directories

        for name in file_names:
            candidate = current / name
            if candidate.suffix != ".jsonl":
                continue
            details = candidate.lstat()
            if stat.S_ISLNK(details.st_mode):
                skipped_symlinks += 1
                continue
            if not stat.S_ISREG(details.st_mode):
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("log_candidate_path_escape")
            files.append(
                _ObservedFile(
                    path=resolved,
                    size_bytes=details.st_size,
                    mtime_ns=details.st_mtime_ns,
                )
            )
    return files, skipped_symlinks


def _entry_is_unchanged(root: Path, entry: LogRetentionEntry) -> bool:
    try:
        details = entry.path.lstat()
        resolved = entry.path.resolve(strict=True)
    except FileNotFoundError:
        return False
    if not resolved.is_relative_to(root):
        return False
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        return False
    return details.st_size == entry.size_bytes and details.st_mtime_ns == entry.mtime_ns
