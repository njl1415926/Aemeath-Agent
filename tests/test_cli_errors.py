from __future__ import annotations

from types import SimpleNamespace

from app import cli
from app.session.errors import SessionEndedError


class _FailingAgentService:
    def handle_chat(self, *, user_context, request):
        raise SessionEndedError("session ended token=secret123")


class _FakeStore:
    def create_session(self, *, user_id, character_id, metadata):
        return SimpleNamespace(
            id="session_test",
            status="active",
            character_id=character_id,
            message_count=0,
            last_message_at=None,
        )


def test_cli_turn_failure_prints_stable_error_code(monkeypatch, capsys):
    settings = SimpleNamespace(
        default_user_id="user_default",
        default_character_id="aemeath",
        recent_message_limit=12,
    )
    inputs = iter(["hello", "/exit"])

    monkeypatch.setattr(
        cli,
        "build_agent_service",
        lambda: (_FailingAgentService(), None, settings, _FakeStore()),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    assert cli.run_cli() == 0

    output = capsys.readouterr().out
    assert "[SESSION_ENDED]" in output
    assert "Session has ended. Resume it before sending messages." in output
    assert "secret123" not in output
