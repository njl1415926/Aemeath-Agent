from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.cli import _handle_session_command
from app.main import build_controller
from app.schemas import AgentTurnInput
from app.session.errors import SessionArchivedError, SessionEndedError, SessionOwnershipError
from app.utils.ids import new_id


def test_direct_turn_ensures_session(controller_and_store):
    controller, store, settings = controller_and_store
    session_id = new_id("session")

    controller.handle_user_message(
        AgentTurnInput(
            user_id=settings.default_user_id,
            session_id=session_id,
            character_id=settings.default_character_id,
            message="hello",
        )
    )

    session = store.get_session(session_id=session_id)
    assert session is not None
    assert session.user_id == settings.default_user_id
    assert session.character_id == settings.default_character_id
    assert session.message_count == 2


@pytest.mark.parametrize("lifecycle", ["ended", "archived"])
def test_inactive_session_rejects_turn_without_side_effects(settings, lifecycle: str):
    settings = replace(
        settings,
        enable_long_term_memory=True,
        enable_prompt_run_log=True,
        prompt_run_log_root=settings.db_path.parent / ".aemeath" / "sessions",
    )
    runtime = build_controller(settings=settings)
    controller, resolved_settings, store = (
        runtime.agent_service.controller,
        runtime.settings,
        runtime.store,
    )
    session = store.create_session(
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    if lifecycle == "ended":
        store.end_session(session_id=session.id, user_id=resolved_settings.default_user_id)
        expected_error = SessionEndedError
    else:
        store.archive_session(session_id=session.id, user_id=resolved_settings.default_user_id)
        expected_error = SessionArchivedError

    with pytest.raises(expected_error):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=resolved_settings.default_user_id,
                session_id=session.id,
                character_id=resolved_settings.default_character_id,
                message="must not persist",
            )
        )

    assert store.list_messages_by_session(session_id=session.id) == []
    assert store.list_memory_extraction_jobs(session_id=session.id) == []
    assert not resolved_settings.prompt_run_log_root.exists()


def test_cross_user_turn_is_rejected_before_message_write(controller_and_store):
    controller, store, settings = controller_and_store
    session = store.create_session(
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
    )

    with pytest.raises(SessionOwnershipError):
        controller.handle_user_message(
            AgentTurnInput(
                user_id="other_user",
                session_id=session.id,
                character_id=settings.default_character_id,
                message="must not persist",
            )
        )

    assert store.list_messages_by_session(session_id=session.id) == []


def test_memory_job_provenance_matches_session(long_term_memory_controller_and_store):
    controller, store, settings = long_term_memory_controller_and_store
    session_id = new_id("session")

    for index in range(6):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=settings.default_user_id,
                session_id=session_id,
                character_id=settings.default_character_id,
                message=f"turn {index}",
            )
        )

    session = store.get_session(session_id=session_id)
    [job] = [
        job
        for job in store.list_memory_extraction_jobs(session_id=session_id)
        if job.trigger_type == "window"
    ]
    assert session is not None
    assert (job.user_id, job.session_id, job.character_id) == (
        session.user_id,
        session.id,
        session.character_id,
    )


def test_resumed_session_history_reaches_prompt_log(settings):
    settings = replace(
        settings,
        enable_prompt_run_log=True,
        prompt_run_log_root=settings.db_path.parent / ".aemeath" / "sessions",
    )
    runtime = build_controller(settings=settings)
    controller, resolved_settings, store = (
        runtime.agent_service.controller,
        runtime.settings,
        runtime.store,
    )
    session = store.create_session(
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    controller.handle_user_message(
        AgentTurnInput(
            user_id=resolved_settings.default_user_id,
            session_id=session.id,
            character_id=resolved_settings.default_character_id,
            message="first message",
        )
    )
    store.end_session(session_id=session.id, user_id=resolved_settings.default_user_id)
    store.resume_session(
        session_id=session.id,
        user_id=resolved_settings.default_user_id,
        character_id=resolved_settings.default_character_id,
    )
    controller.handle_user_message(
        AgentTurnInput(
            user_id=resolved_settings.default_user_id,
            session_id=session.id,
            character_id=resolved_settings.default_character_id,
            message="second message",
        )
    )

    records = []
    for log_file in resolved_settings.prompt_run_log_root.glob("*/*/*/*.jsonl"):
        records.extend(
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line
        )
    second = records[-1]
    recent = next(
        section for section in second["sections"] if section["id"] == "recent_conversation"
    )
    assert any("first message" in line for line in recent["content_lines"])


def test_cli_session_new_end_resume_archive_workflow(controller_and_store, capsys):
    _controller, store, settings = controller_and_store
    current = store.create_session(
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
    )
    store.save_message(
        session_id=current.id,
        user_id=settings.default_user_id,
        role="user",
        content="old history",
    )

    new_id = _handle_session_command(
        command="/session new",
        session_store=store,
        message_store=store,
        current_session_id=current.id,
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
        history_limit=12,
    )
    assert new_id != current.id
    assert store.get_session(session_id=current.id).status == "active"  # type: ignore[union-attr]

    unchanged = _handle_session_command(
        command=f"/session end {current.id}",
        session_store=store,
        message_store=store,
        current_session_id=new_id,
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
        history_limit=12,
    )
    assert unchanged == new_id

    resumed_id = _handle_session_command(
        command=f"/session resume {current.id}",
        session_store=store,
        message_store=store,
        current_session_id=new_id,
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
        history_limit=12,
    )
    assert resumed_id == current.id
    assert "user> old history" in capsys.readouterr().out

    replacement_id = _handle_session_command(
        command="/session archive",
        session_store=store,
        message_store=store,
        current_session_id=resumed_id,
        user_id=settings.default_user_id,
        character_id=settings.default_character_id,
        history_limit=12,
    )
    assert replacement_id != resumed_id
    assert store.get_session(session_id=resumed_id).status == "archived"  # type: ignore[union-attr]
