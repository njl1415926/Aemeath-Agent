from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.deployment.uvicorn_config import validated_trusted_proxy_ips

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value", ["*", "0.0.0.0", "10.0.0.0/24", "10.0.0.2,10.0.0.3"])
def test_prod_trusted_proxy_rejects_wildcard_network_or_multiple_ips(value: str):
    with pytest.raises(ValueError):
        validated_trusted_proxy_ips(value, environment="prod")


def test_prod_trusted_proxy_accepts_one_fixed_caddy_ip():
    assert validated_trusted_proxy_ips("172.30.0.2", environment="prod") == "172.30.0.2"


def test_untrusted_client_cannot_spoof_forwarded_ip():
    observed: list[tuple[str, int]] = []

    async def downstream(scope, receive, send):
        del receive
        observed.append(scope["client"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ProxyHeadersMiddleware(downstream, trusted_hosts="172.30.0.2")
    scope = {
        "type": "http",
        "scheme": "http",
        "client": ("203.0.113.20", 1234),
        "headers": [(b"x-forwarded-for", b"198.51.100.5")],
    }

    async def _run():
        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(_message):
            return None

        await middleware(scope, _receive, _send)

    asyncio.run(_run())

    assert observed == [("203.0.113.20", 1234)]


def test_caddy_contract_has_exact_raw_limit_and_uses_first_hop_forwarding_defaults():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert re.search(r"http_port 8080\s+https_port 8443", caddyfile)
    assert re.search(r"servers\s*\{\s*protocols h1 h2\s*\}", caddyfile)
    assert "h3" not in caddyfile
    assert re.search(r"request_body\s*\{\s*max_size 128KiB\s*\}", caddyfile)
    assert "untrusted inbound X-Forwarded-*" in caddyfile
    assert "header_up" not in caddyfile
    assert "flush_interval" not in caddyfile
    assert not re.search(r"(?m)^\s*log\s", caddyfile)


def test_deployment_images_are_linux_amd64_digest_pinned_and_non_root():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    caddy_lock = (ROOT / "deploy" / "caddy-image.lock").read_text(encoding="utf-8")

    base = (
        "python:3.10.20-slim-bookworm@"
        "sha256:5cc3381b18ec240e4462510648ed7970917ac8eff4c3463320429fec285491be"
    )
    assert dockerfile.count(f"FROM {base}") == 2
    assert dockerfile.count("python -m pip uninstall --yes pip setuptools") == 2
    assert "USER 10001:10001" in dockerfile
    assert 'CMD ["python", "/opt/aemeath/scripts/deployment/start_api.py"]' in dockerfile
    assert re.search(r"caddy:2\.10\.2-alpine@sha256:[0-9a-f]{64}", caddy_lock)
