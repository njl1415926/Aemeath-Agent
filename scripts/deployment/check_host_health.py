from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.deployment.host_health import (  # noqa: E402
    HostHealthThresholds,
    inspect_host_health,
    parse_compose_service_states,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Aemeath host deployment health.")
    parser.add_argument("--project-directory", type=Path, default=Path("/opt/aemeath"))
    parser.add_argument("--compose-file", type=Path, default=Path("/opt/aemeath/compose.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path("/etc/aemeath/prod.env"))
    parser.add_argument("--data-root", type=Path, default=Path("/var/lib/aemeath"))
    parser.add_argument("--log-root", type=Path, default=Path("/var/log/aemeath"))
    parser.add_argument("--backup-root", type=Path, default=Path("/var/backups/aemeath"))
    parser.add_argument("--min-free-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--min-free-ratio", type=float, default=0.10)
    parser.add_argument("--max-backup-age-seconds", type=int, default=36 * 3600)
    parser.add_argument("--log-lookback-seconds", type=int, default=15 * 60)
    parser.add_argument("--max-quota-rejections", type=int, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project_directory = args.project_directory.resolve(strict=True)
        compose_file = args.compose_file.resolve(strict=True)
        env_file = args.env_file.resolve(strict=True)
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(project_directory),
                "--env-file",
                str(env_file),
                "-f",
                str(compose_file),
                "ps",
                "--format",
                "json",
                "api",
                "proxy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("compose_ps_failed")
        compose_states = parse_compose_service_states(completed.stdout)
        thresholds = HostHealthThresholds(
            min_free_bytes=args.min_free_bytes,
            min_free_ratio=args.min_free_ratio,
            max_backup_age_seconds=args.max_backup_age_seconds,
            log_lookback_seconds=args.log_lookback_seconds,
            max_quota_rejections=args.max_quota_rejections,
        )
        report = inspect_host_health(
            compose_states=compose_states,
            disk_paths={
                "data": args.data_root,
                "logs": args.log_root,
                "backups": args.backup_root,
            },
            backup_root=args.backup_root,
            app_log_root=args.log_root / "app",
            thresholds=thresholds,
            now=datetime.now(timezone.utc),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        print(json.dumps({"status": "error", "reason": "host_health_check_failed"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
