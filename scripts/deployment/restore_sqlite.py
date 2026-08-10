from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.deployment.sqlite_backup import D6SqliteError, restore_sqlite_backup  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore an Aemeath SQLite backup.")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--db-name", default="aemeath.db")
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--expected-backend", required=True)
    parser.add_argument("--confirm-writers-stopped", action="store_true")
    parser.add_argument("--restore-id")
    parser.add_argument("--resume-restore-id")
    parser.add_argument("--rollback-restore-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = restore_sqlite_backup(
            data_root=args.data_root,
            db_name=args.db_name,
            backup_dir=args.backup_dir,
            journal_root=args.journal_root,
            expected_backend=args.expected_backend,
            confirm_writers_stopped=args.confirm_writers_stopped,
            restore_id=args.restore_id,
            resume_restore_id=args.resume_restore_id,
            rollback_restore_id=args.rollback_restore_id,
        )
    except D6SqliteError as exc:
        print(json.dumps({"status": "failed", "reason": exc.reason}, sort_keys=True))
        return exc.exit_code
    except Exception:
        print(json.dumps({"status": "failed", "reason": "internal_error"}, sort_keys=True))
        return 70
    print(
        json.dumps(
            {
                "status": "ok",
                "restore_id": result.restore_id,
                "stage": result.stage,
                "journal_dir": str(result.journal_dir),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
