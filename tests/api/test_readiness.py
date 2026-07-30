from __future__ import annotations

from dataclasses import replace
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.model.mock import MockChatModelClient
from app.runtime.readiness import ReadinessReason
from app.runtime.resources import ApplicationRuntime


def test_health_and_ready_are_public_bounded_probes(settings, monkeypatch):
    def _provider_must_not_run(*_args, **_kwargs):
        raise AssertionError("readiness must not call a Provider")

    monkeypatch.setattr(MockChatModelClient, "chat", _provider_must_not_run)
    app = create_app(settings)

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.headers["Cache-Control"] == "no-store"
    assert ready.status_code == 200
    assert ready.headers["Cache-Control"] == "no-store"
    assert ready.json() == {
        "status": "ready",
        "service": "aemeath-agent",
        "reason": "ready",
    }


def test_lifespan_marks_closing_before_synchronous_close(settings):
    app = create_app(settings)
    original_close = app.state.application_runtime.close
    observed: list[ReadinessReason] = []

    def _record_close():
        observed.append(app.state.readiness.check(monotonic() + 1).reason)
        return original_close()

    app.state.application_runtime.close = _record_close

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200

    assert observed == [ReadinessReason.CLOSING]


def test_ready_returns_fixed_503_without_internal_details(settings):
    app = create_app(settings)

    class _Unavailable:
        def check(self, deadline: float):
            del deadline
            from app.runtime.readiness import ReadinessResult

            return ReadinessResult(False, ReadinessReason.STORAGE_UNAVAILABLE)

    app.state.readiness = _Unavailable()
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "service": "aemeath-agent",
        "reason": "storage_unavailable",
    }
    assert "sqlite" not in response.text.lower()
    assert "path" not in response.text.lower()


def test_sqlalchemy_dev_api_uses_shared_sqlite_readiness_adapter(settings):
    app = create_app(replace(settings, db_backend="sqlalchemy"))

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert app.state.application_runtime.settings.db_backend == "sqlalchemy"


def test_create_app_closes_runtime_when_post_build_wiring_fails(settings, monkeypatch):
    closed: list[ApplicationRuntime] = []
    original_close = ApplicationRuntime.close

    def _record_close(runtime: ApplicationRuntime):
        closed.append(runtime)
        return original_close(runtime)

    def _fail_readiness(**_kwargs):
        raise RuntimeError("readiness-wiring-failed")

    monkeypatch.setattr(ApplicationRuntime, "close", _record_close)
    monkeypatch.setattr("app.api.server.build_database_readiness", _fail_readiness)

    with pytest.raises(RuntimeError, match="readiness-wiring-failed"):
        create_app(settings)

    assert len(closed) == 1
