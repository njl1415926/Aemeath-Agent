from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.deployment.host_health import (
    HostHealthThresholds,
    inspect_host_health,
    parse_compose_service_states,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _layout(tmp_path: Path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    app_logs = logs / "app"
    backups = tmp_path / "backups"
    for path in (data, app_logs, backups):
        path.mkdir(parents=True)
    manifest = backups / "backups" / "2026" / "08" / "11" / "generation" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "format_version": "aemeath.sqlite_backup.manifest.v1",
                "created_at_utc": "2026-08-11T11:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return data, logs, app_logs, backups


def _record(event_name: str, *, status_code: int | None = None) -> str:
    payload = {} if status_code is None else {"status_code": status_code}
    return json.dumps(
        {
            "created_at": "2026-08-11T11:55:00+00:00",
            "event_name": event_name,
            "payload": payload,
        }
    )


def _thresholds(**overrides) -> HostHealthThresholds:
    values = {
        "min_free_bytes": 1,
        "min_free_ratio": 0.000001,
        "max_backup_age_seconds": 36 * 3600,
        "log_lookback_seconds": 900,
    }
    values.update(overrides)
    return HostHealthThresholds(**values)


def test_parse_compose_service_states_supports_array_and_json_lines():
    records = [
        {"Service": "api", "State": "running", "Health": "healthy"},
        {"Service": "proxy", "State": "running", "Health": "healthy"},
    ]
    expected = {"api": ("running", "healthy"), "proxy": ("running", "healthy")}

    assert parse_compose_service_states(json.dumps(records)) == expected
    assert parse_compose_service_states("\n".join(json.dumps(item) for item in records)) == expected


def test_healthy_host_report_contains_only_safe_aggregates(tmp_path: Path):
    data, logs, app_logs, backups = _layout(tmp_path)
    report = inspect_host_health(
        compose_states={"api": ("running", "healthy"), "proxy": ("running", "healthy")},
        disk_paths={"data": data, "logs": logs, "backups": backups},
        backup_root=backups,
        app_log_root=app_logs,
        thresholds=_thresholds(),
        now=NOW,
    )

    assert report["status"] == "ok"
    assert report["critical_reasons"] == []
    rendered = json.dumps(report)
    assert str(tmp_path) not in rendered
    assert "request_id" not in rendered


def test_health_detects_services_stale_backup_and_structured_log_signals(tmp_path: Path):
    data, logs, app_logs, backups = _layout(tmp_path)
    manifest = next(backups.rglob("manifest.json"))
    manifest.write_text(
        json.dumps(
            {
                "format_version": "aemeath.sqlite_backup.manifest.v1",
                "created_at_utc": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    log = app_logs / "2026" / "08" / "11" / "app.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                _record("api.request_failed", status_code=503),
                _record("model.deepseek_failed"),
                *[_record("usage_quota.rejected") for _ in range(6)],
                "not-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    timestamp = (NOW - timedelta(minutes=1)).timestamp()
    os.utime(log, (timestamp, timestamp))

    report = inspect_host_health(
        compose_states={"api": ("running", "unhealthy")},
        disk_paths={"data": data, "logs": logs, "backups": backups},
        backup_root=backups,
        app_log_root=app_logs,
        thresholds=_thresholds(max_quota_rejections=5),
        now=NOW,
    )

    assert report["status"] == "critical"
    assert "resident_service_unhealthy" in report["critical_reasons"]
    assert "backup_missing_or_stale" in report["critical_reasons"]
    assert "api_5xx_threshold_exceeded" in report["critical_reasons"]
    assert "provider_failure_threshold_exceeded" in report["critical_reasons"]
    assert "quota_rejection_threshold_exceeded" in report["warning_reasons"]
    signals = report["checks"]["app_log_signals"]
    assert signals["api_5xx"] == 1
    assert signals["provider_failures"] == 1
    assert signals["quota_rejections"] == 6
    assert signals["malformed_lines"] == 1


def test_log_scan_budget_fails_closed(tmp_path: Path):
    data, logs, app_logs, backups = _layout(tmp_path)
    log = app_logs / "app.jsonl"
    log.write_text(_record("api.request_completed", status_code=200) + "\n", encoding="utf-8")
    timestamp = (NOW - timedelta(minutes=1)).timestamp()
    os.utime(log, (timestamp, timestamp))

    report = inspect_host_health(
        compose_states={"api": ("running", "healthy"), "proxy": ("running", "healthy")},
        disk_paths={"data": data, "logs": logs, "backups": backups},
        backup_root=backups,
        app_log_root=app_logs,
        thresholds=_thresholds(max_log_scan_bytes=1),
        now=NOW,
    )

    assert report["status"] == "critical"
    assert "app_log_scan_incomplete" in report["critical_reasons"]
