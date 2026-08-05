from __future__ import annotations

import json
from pathlib import Path

from app.auth.admin import main
from app.db.sqlite_store import SQLiteStore


def _configure(monkeypatch, db_path: Path) -> None:
    monkeypatch.setenv("AEMEATH_DB_PATH", str(db_path))
    monkeypatch.setenv("AEMEATH_DB_BACKEND", "sqlite_raw")
    monkeypatch.setenv("AEMEATH_USE_MOCK_LLM", "true")
    monkeypatch.delenv("AEMEATH_ENV", raising=False)


def test_operator_cli_token_is_one_time_and_lists_only_safe_records(
    tmp_path: Path, monkeypatch, capsys
):
    _configure(monkeypatch, tmp_path / "admin.db")

    assert (
        main(
            [
                "--origin",
                "https://example.test",
                "create-invite",
                "--username",
                "alice",
                "--display-name",
                "Alice",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "This value will not be shown again:"
    assert output[1].startswith("https://example.test/invite#token=inv_")
    raw_token = output[1].split("token=", 1)[1]
    assert output[1].count(raw_token) == 1

    assert main(["--origin", "https://example.test", "list-invites"]) == 0
    listed = capsys.readouterr().out
    records = json.loads(listed)
    assert records[0]["username_normalized"] == "alice"
    assert records[0]["derived_status"] == "pending"
    assert raw_token not in listed
    assert "token_digest" not in listed


def test_operator_cli_reset_disable_enable_and_idempotent_session_revoke(
    tmp_path: Path, monkeypatch, capsys
):
    db_path = tmp_path / "admin-controls.db"
    _configure(monkeypatch, db_path)
    store = SQLiteStore(db_path)
    store.upsert_account(
        account_id="operator_account",
        username="ops_user",
        display_name="Ops User",
        status="active",
        roles=["user"],
    )

    assert (
        main(
            [
                "--origin",
                "https://example.test",
                "issue-password-reset",
                "--account",
                "user_default",
            ]
        )
        == 0
    )
    reset_output = capsys.readouterr().out
    assert "reset-password#token=rst_" in reset_output

    assert main(["--origin", "https://example.test", "disable-account", "ops_user"]) == 0
    assert json.loads(capsys.readouterr().out) == {"changed": True}
    assert (
        main(
            [
                "--origin",
                "https://example.test",
                "enable-account",
                "operator_account",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"changed": True}
    assert (
        main(
            [
                "--origin",
                "https://example.test",
                "revoke-account-sessions",
                "user_default",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"revoked_count": 0}


def test_operator_cli_masks_exception_details(tmp_path: Path, monkeypatch, capsys):
    _configure(monkeypatch, tmp_path / "admin-error.db")

    assert (
        main(
            [
                "--origin",
                "https://example.test",
                "create-invite",
                "--username",
                "Invalid User",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "auth admin command failed (ValueError)"
    assert "Invalid User" not in captured.err
