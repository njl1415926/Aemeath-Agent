from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.deployment.marker import marker_is_absent  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check controlled artifacts for a fake secret marker."
    )
    parser.add_argument("--marker-file", required=True, type=Path)
    parser.add_argument("--category", required=True)
    parser.add_argument("targets", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        passed = marker_is_absent(marker_file=args.marker_file, targets=args.targets)
    except (OSError, ValueError):
        print(json.dumps({"category": args.category, "status": "error"}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {"category": args.category, "status": "pass" if passed else "fail"},
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
