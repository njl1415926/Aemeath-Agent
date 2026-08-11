from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class HostHealthThresholds:
    min_free_bytes: int = 2 * 1024 * 1024 * 1024
    min_free_ratio: float = 0.10
    max_backup_age_seconds: int = 36 * 3600
    log_lookback_seconds: int = 15 * 60
    max_api_5xx: int = 0
    max_provider_failures: int = 0
    max_quota_rejections: int = 5
    max_log_scan_bytes: int = 32 * 1024 * 1024
    max_log_files: int = 128

    def validate(self) -> None:
        if self.min_free_bytes < 1:
            raise ValueError("min_free_bytes_invalid")
        if not 0 < self.min_free_ratio < 1:
            raise ValueError("min_free_ratio_invalid")
        for value in (
            self.max_backup_age_seconds,
            self.log_lookback_seconds,
            self.max_log_scan_bytes,
            self.max_log_files,
        ):
            if value < 1:
                raise ValueError("host_health_threshold_invalid")
        for value in (
            self.max_api_5xx,
            self.max_provider_failures,
            self.max_quota_rejections,
        ):
            if value < 0:
                raise ValueError("host_health_count_threshold_invalid")


@dataclass(frozen=True)
class AppLogSignals:
    api_5xx: int
    provider_failures: int
    quota_rejections: int
    malformed_lines: int
    scanned_file_count: int
    scanned_bytes: int
    scan_truncated: bool


def parse_compose_service_states(raw: str) -> dict[str, tuple[str, str]]:
    stripped = raw.strip()
    if not stripped:
        return {}
    try:
        decoded = json.loads(stripped)
        records = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        records = [json.loads(line) for line in stripped.splitlines() if line.strip()]

    states: dict[str, tuple[str, str]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("compose_ps_record_invalid")
        service = record.get("Service")
        state = record.get("State")
        health = record.get("Health") or ""
        if not all(isinstance(value, str) for value in (service, state, health)):
            raise ValueError("compose_ps_field_invalid")
        if service:
            states[service] = (state.lower(), health.lower())
    return states


def inspect_host_health(
    *,
    compose_states: dict[str, tuple[str, str]],
    disk_paths: dict[str, Path],
    backup_root: Path,
    app_log_root: Path,
    thresholds: HostHealthThresholds,
    now: datetime | None = None,
) -> dict[str, Any]:
    thresholds.validate()
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        raise ValueError("host_health_now_must_be_timezone_aware")

    critical_reasons: list[str] = []
    warning_reasons: list[str] = []
    unhealthy_services = []
    for service in ("api", "proxy"):
        state, health = compose_states.get(service, ("missing", ""))
        if state != "running" or health != "healthy":
            unhealthy_services.append(service)
    if unhealthy_services:
        critical_reasons.append("resident_service_unhealthy")

    disk_summary: dict[str, dict[str, int | float | bool]] = {}
    for label, path in disk_paths.items():
        if not path.is_dir():
            disk_summary[label] = {"available": False}
            critical_reasons.append("disk_path_unavailable")
            continue
        usage = shutil.disk_usage(path)
        free_ratio = usage.free / usage.total if usage.total else 0.0
        healthy = (
            usage.free >= thresholds.min_free_bytes
            and free_ratio >= thresholds.min_free_ratio
        )
        disk_summary[label] = {
            "available": True,
            "healthy": healthy,
            "free_bytes": usage.free,
            "free_ratio": round(free_ratio, 6),
        }
        if not healthy:
            critical_reasons.append("disk_capacity_low")

    backup_age = _latest_backup_age_seconds(backup_root=backup_root, now=observed_now)
    backup_healthy = (
        backup_age is not None and backup_age <= thresholds.max_backup_age_seconds
    )
    if not backup_healthy:
        critical_reasons.append("backup_missing_or_stale")

    signals = inspect_app_log_signals(
        root=app_log_root,
        now=observed_now,
        lookback_seconds=thresholds.log_lookback_seconds,
        max_scan_bytes=thresholds.max_log_scan_bytes,
        max_files=thresholds.max_log_files,
    )
    if signals.api_5xx > thresholds.max_api_5xx:
        critical_reasons.append("api_5xx_threshold_exceeded")
    if signals.provider_failures > thresholds.max_provider_failures:
        critical_reasons.append("provider_failure_threshold_exceeded")
    if signals.quota_rejections > thresholds.max_quota_rejections:
        warning_reasons.append("quota_rejection_threshold_exceeded")
    if signals.malformed_lines:
        warning_reasons.append("app_log_malformed_lines")
    if signals.scan_truncated:
        critical_reasons.append("app_log_scan_incomplete")

    critical_reasons = sorted(set(critical_reasons))
    warning_reasons = sorted(set(warning_reasons))
    status = "critical" if critical_reasons else "warning" if warning_reasons else "ok"
    return {
        "status": status,
        "critical_reasons": critical_reasons,
        "warning_reasons": warning_reasons,
        "checks": {
            "services": {
                "healthy": not unhealthy_services,
                "unhealthy_count": len(unhealthy_services),
            },
            "disks": disk_summary,
            "backup": {
                "healthy": backup_healthy,
                "age_seconds": backup_age,
                "max_age_seconds": thresholds.max_backup_age_seconds,
            },
            "app_log_signals": {
                "lookback_seconds": thresholds.log_lookback_seconds,
                "api_5xx": signals.api_5xx,
                "provider_failures": signals.provider_failures,
                "quota_rejections": signals.quota_rejections,
                "malformed_lines": signals.malformed_lines,
                "scanned_file_count": signals.scanned_file_count,
                "scanned_bytes": signals.scanned_bytes,
                "scan_truncated": signals.scan_truncated,
            },
        },
    }


def inspect_app_log_signals(
    *,
    root: Path,
    now: datetime,
    lookback_seconds: int,
    max_scan_bytes: int,
    max_files: int,
) -> AppLogSignals:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("app_log_root_invalid")
    resolved_root = root.resolve(strict=True)
    cutoff = now - timedelta(seconds=lookback_seconds)
    candidates = []
    for candidate in resolved_root.rglob("*.jsonl"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise ValueError("app_log_path_escape")
        details = resolved.stat()
        if datetime.fromtimestamp(details.st_mtime, timezone.utc) >= cutoff:
            candidates.append((details.st_mtime_ns, resolved, details.st_size))
    candidates.sort(reverse=True)

    scan_truncated = len(candidates) > max_files
    selected = candidates[:max_files]
    scanned_bytes = 0
    scanned_files = 0
    api_5xx = 0
    provider_failures = 0
    quota_rejections = 0
    malformed_lines = 0
    for _mtime_ns, path, size_bytes in selected:
        if scanned_bytes + size_bytes > max_scan_bytes:
            scan_truncated = True
            break
        scanned_bytes += size_bytes
        scanned_files += 1
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    created_at = _parse_datetime(record.get("created_at"))
                    if created_at < cutoff:
                        continue
                    event_name = record.get("event_name")
                    payload = record.get("payload")
                    if not isinstance(event_name, str) or not isinstance(payload, dict):
                        raise ValueError("app_log_record_invalid")
                    if event_name == "api.request_failed":
                        status_code = payload.get("status_code")
                        if isinstance(status_code, int) and status_code >= 500:
                            api_5xx += 1
                    elif event_name.startswith("model.") and event_name.endswith("_failed"):
                        provider_failures += 1
                    elif event_name == "usage_quota.rejected":
                        quota_rejections += 1
                except (json.JSONDecodeError, TypeError, ValueError):
                    malformed_lines += 1
    return AppLogSignals(
        api_5xx=api_5xx,
        provider_failures=provider_failures,
        quota_rejections=quota_rejections,
        malformed_lines=malformed_lines,
        scanned_file_count=scanned_files,
        scanned_bytes=scanned_bytes,
        scan_truncated=scan_truncated,
    )


def _latest_backup_age_seconds(*, backup_root: Path, now: datetime) -> int | None:
    if backup_root.is_symlink() or not backup_root.is_dir():
        return None
    resolved_root = backup_root.resolve(strict=True)
    latest: datetime | None = None
    for manifest in _safe_manifests(resolved_root):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("format_version") != "aemeath.sqlite_backup.manifest.v1":
                continue
            created_at = _parse_datetime(payload.get("created_at_utc"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if latest is None or created_at > latest:
            latest = created_at
    if latest is None:
        return None
    return max(0, int((now - latest).total_seconds()))


def _safe_manifests(root: Path) -> Iterable[Path]:
    backup_generations = root / "backups"
    if not backup_generations.is_dir() or backup_generations.is_symlink():
        return
    for candidate in backup_generations.rglob("manifest.json"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        if resolved.is_relative_to(root):
            yield resolved


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)
