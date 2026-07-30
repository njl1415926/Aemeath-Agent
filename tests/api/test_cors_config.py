from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app


def test_explicit_dev_cors_origin_gets_narrow_headers(settings):
    app = create_app(
        replace(settings, api_cors_allowed_origins=("https://client.example.test",))
    )
    with TestClient(app) as client:
        response = client.options(
            "/v1/chat",
            headers={
                "Origin": "https://client.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type,X-Request-ID",
            },
        )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://client.example.test"
    assert response.headers.get("Access-Control-Allow-Credentials") != "true"
    assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"


def test_empty_cors_allowlist_emits_no_cross_origin_permission(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "https://untrusted.test"})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    ["*", "https://example.test/path", "https://user@example.test", "https://example.test/"],
)
def test_cors_rejects_wildcard_path_userinfo_and_trailing_slash(settings, origin: str):
    with pytest.raises(ValueError):
        replace(settings, api_cors_allowed_origins=(origin,)).validate()
