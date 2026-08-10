from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.deployment.sqlite_backup import D6SqliteError, verify_sqlite_backup  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an Aemeath SQLite backup.")
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--expected-source-db")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_sqlite_backup(
            backup_dir=args.backup_dir,
            backend=args.backend,
            expected_source_db=args.expected_source_db,
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
                "backup_id": result.backup_id,
                "backup_dir": str(result.backup_dir),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
