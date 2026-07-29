from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.errors import ErrorCode
from app.runtime.guardrails.rate_limit import InProcessApiRateLimiter
from app.runtime.guardrails.schemas import ApiRateLimitRequest
from app.utils.ids import new_id


def test_token_bucket_returns_stable_reason_and_retry_after():
    now = [100.0]
    limiter = InProcessApiRateLimiter(
        user_rate_per_minute=60,
        user_burst=1,
        ip_rate_per_minute=120,
        ip_burst=2,
        idle_ttl_seconds=10,
        clock=lambda: now[0],
    )
    request = ApiRateLimitRequest(user_id="u1", ip_key="127.0.0.1")
    assert limiter.check(request=request).allowed
    rejected = limiter.check(request=request)
    assert not rejected.allowed
    assert rejected.reason_code == "user_rate_limit"
    assert rejected.retry_after_seconds == 1
    now[0] += 1
    assert limiter.check(request=request).allowed


def test_retry_after_uses_longest_wait_when_user_and_ip_are_both_limited():
    now = [100.0]
    limiter = InProcessApiRateLimiter(
        user_rate_per_minute=60,
        user_burst=1,
        ip_rate_per_minute=6,
        ip_burst=1,
        idle_ttl_seconds=30,
        clock=lambda: now[0],
    )
    request = ApiRateLimitRequest(user_id="u1", ip_key="127.0.0.1")

    assert limiter.check(request=request).allowed
    rejected = limiter.check(request=request)

    assert rejected.reason_code == "user_rate_limit"
    assert rejected.retry_after_seconds == 10


def test_bucket_sweep_uses_fixed_interval_and_bounded_batches():
    now = [0.0]
    limiter = InProcessApiRateLimiter(
        user_rate_per_minute=60,
        user_burst=1,
        ip_rate_per_minute=60,
        ip_burst=1,
        idle_ttl_seconds=5,
        sweep_interval_seconds=10,
        sweep_batch_size=3,
        clock=lambda: now[0],
    )
    for index in range(5):
        assert limiter.check(
            request=ApiRateLimitRequest(user_id=f"stale-{index}", ip_key=None)
        ).allowed

    now[0] = 6.0
    assert limiter.check(
        request=ApiRateLimitRequest(user_id="before-interval", ip_key=None)
    ).allowed
    assert len(limiter._buckets) == 6

    now[0] = 10.0
    assert limiter.check(
        request=ApiRateLimitRequest(user_id="first-sweep", ip_key=None)
    ).allowed
    assert len(limiter._buckets) == 4

    assert limiter.check(
        request=ApiRateLimitRequest(user_id="second-sweep", ip_key=None)
    ).allowed
    assert len(limiter._buckets) == 3


def test_api_chat_rate_limit_is_after_auth_and_sets_retry_after(settings):
    configured = replace(
        settings,
        api_allow_local_default_user=True,
        api_chat_user_rate_per_minute=60,
        api_chat_user_burst=1,
        api_chat_ip_rate_per_minute=60,
        api_chat_ip_burst=5,
    )
    client = TestClient(create_app(configured))
    body = {
        "session_id": new_id("session"),
        "character_id": "aemeath",
        "message": "hello",
    }
    assert client.post("/v1/chat", json=body).status_code == 200
    response = client.post("/v1/chat", json={**body, "session_id": new_id("session")})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == ErrorCode.API_RATE_LIMITED.value
    assert int(response.headers["Retry-After"]) >= 1


def test_invalid_external_request_id_is_never_echoed(settings):
    app = create_app(settings)
    response = TestClient(app).get(
        "/health",
        headers={"X-Request-ID": " invalid-secret-request-id "},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    assert "invalid-secret" not in response.headers["X-Request-ID"]


def test_non_chat_endpoint_is_not_ip_rate_limited(settings):
    client = TestClient(create_app(settings))
    for _ in range(30):
        assert client.get("/health").status_code == 200
