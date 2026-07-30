from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from app.deployment.uvicorn_config import validated_trusted_proxy_ips  # noqa: E402


def main() -> int:
    environment = os.environ.get("AEMEATH_ENVIRONMENT", "dev").strip().lower()
    try:
        trusted_proxy_ips = validated_trusted_proxy_ips(
            os.environ.get("AEMEATH_TRUSTED_PROXY_IPS"),
            environment=environment,
        )
    except ValueError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True))
        return 1
    uvicorn.run(
        "app.api.server:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        workers=1,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxy_ips,
        access_log=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=60,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
