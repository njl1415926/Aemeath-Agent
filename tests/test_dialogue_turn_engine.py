from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.schemas import AgentTurnInput
from app.session.errors import SessionEndedError
from app.utils.ids import new_id


def _record_call(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    target: object,
    method_name: str,
    event: str | Callable[[tuple[Any, ...], dict[str, Any]], str],
) -> None:
    original = getattr(target, method_name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        event_name = event(args, kwargs) if callable(event) else event
        events.append(event_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method_name, wrapped)


def _turn_input(*, settings: Any, session_id: str) -> AgentTurnInput:
    return AgentTurnInput(
        user_id=settings.default_user_id,
        session_id=session_id,
        character_id=settings.default_character_id,
        message="请记录这次同步编排顺序。",
        metadata={"source": "characterization"},
    )


def test_sync_dialogue_pipeline_preserves_stage_and_terminal_order(
    controller_and_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _store, settings = controller_and_store
    events: list[str] = []

    _record_call(
        monkeypatch,
        events,
        controller.character_provider,
        "load_profile",
        "character.load_profile",
    )
    _record_call(
        monkeypatch,
        events,
        controller.session_store,
        "ensure_session",
        "session.ensure",
    )
    _record_call(
        monkeypatch,
        events,
        controller.trace_logger,
        "record",
        lambda args, _kwargs: f"trace.{args[0].event_type}",
    )
    _record_call(
        monkeypatch,
        events,
        controller.character_provider,
        "build_runtime_context",
        "character.build_context",
    )
    _record_call(
        monkeypatch,
        events,
        controller.message_store,
        "save_message",
        lambda _args, kwargs: f"message.save.{kwargs['role']}",
    )
    _record_call(
        monkeypatch,
        events,
        controller.message_store,
        "get_recent_messages",
        "message.get_recent",
    )
    _record_call(
        monkeypatch,
        events,
        controller.relationship_store,
        "get_or_create_relationship_state",
        "relationship.get",
    )
    _record_call(
        monkeypatch,
        events,
        controller.semantic_query_coordinator,
        "prepare_turn_context",
        "semantic.prepare",
    )
    _record_call(
        monkeypatch,
        events,
        controller.memory_reader,
        "retrieve_query",
        "memory.retrieve",
    )
    _record_call(
        monkeypatch,
        events,
        controller.evidence_provider,
        "build_evidence_pack",
        "evidence.build",
    )
    _record_call(
        monkeypatch,
        events,
        controller.context_builder,
        "build_context",
        "prompt.build_context",
    )
    _record_call(
        monkeypatch,
        events,
        controller.prompt_compiler,
        "compile_messages",
        "prompt.compile",
    )
    _record_call(
        monkeypatch,
        events,
        controller.response_generator,
        "generate",
        "model.generate",
    )
    _record_call(
        monkeypatch,
        events,
        controller.state_updater,
        "decide",
        "state.decide",
    )
    _record_call(
        monkeypatch,
        events,
        controller.relationship_store,
        "update_relationship_state",
        "relationship.update",
    )
    _record_call(
        monkeypatch,
        events,
        controller.memory_write_classifier,
        "decide",
        "memory.classify",
    )
    _record_call(
        monkeypatch,
        events,
        controller.memory_writer,
        "persist_decision",
        "memory.persist",
    )
    _record_call(
        monkeypatch,
        events,
        controller.memory_writer,
        "enqueue_window_if_due",
        "memory.enqueue_window",
    )
    _record_call(
        monkeypatch,
        events,
        controller.prompt_run_logger,
        "record",
        lambda args, _kwargs: f"prompt_log.{args[0].status}",
    )

    output = controller.handle_user_message(
        _turn_input(settings=settings, session_id=new_id("session"))
    )

    assert output.response
    assert events == [
        "character.load_profile",
        "session.ensure",
        "trace.turn_started",
        "character.build_context",
        "message.save.user",
        "message.get_recent",
        "relationship.get",
        "semantic.prepare",
        "trace.semantic_context_prepared",
        "memory.retrieve",
        "evidence.build",
        "trace.context_sources_selected",
        "prompt.build_context",
        "trace.prompt_context_built",
        "prompt.compile",
        "model.generate",
        "trace.response_generated",
        "message.save.assistant",
        "state.decide",
        "trace.state_update_decided",
        "relationship.update",
        "relationship.get",
        "memory.classify",
        "memory.persist",
        "memory.enqueue_window",
        "trace.memory_write_decided",
        "trace.turn_completed",
        "prompt_log.completed",
    ]


def test_session_validation_failure_precedes_user_persistence_and_turn_terminal(
    controller_and_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _store, settings = controller_and_store
    events: list[str] = []
    original_load = controller.character_provider.load_profile

    def load_profile(*args: Any, **kwargs: Any) -> Any:
        events.append("character.load_profile")
        return original_load(*args, **kwargs)

    def fail_session(**_kwargs: Any) -> None:
        events.append("session.ensure")
        raise SessionEndedError("ended marker=private-session-content")

    def fail_if_saved(**_kwargs: Any) -> str:
        raise AssertionError("user message must not be saved after Session validation failure")

    def record_trace(event: Any) -> None:
        events.append(f"trace.{event.event_type}")

    monkeypatch.setattr(controller.character_provider, "load_profile", load_profile)
    monkeypatch.setattr(controller.session_store, "ensure_session", fail_session)
    monkeypatch.setattr(controller.message_store, "save_message", fail_if_saved)
    monkeypatch.setattr(controller.trace_logger, "record", record_trace)

    with pytest.raises(SessionEndedError):
        controller.handle_user_message(
            _turn_input(settings=settings, session_id=new_id("session"))
        )

    assert events == [
        "character.load_profile",
        "session.ensure",
        "trace.session_validation_failed",
    ]


def test_prepared_turn_repr_does_not_expose_prompt_or_user_content(
    controller_and_store: tuple[Any, Any, Any],
) -> None:
    controller, _store, settings = controller_and_store
    marker = "SENSITIVE_PREPARED_TURN_MARKER_2d19"
    turn_input = AgentTurnInput(
        user_id=settings.default_user_id,
        session_id=new_id("session"),
        character_id=settings.default_character_id,
        message=marker,
        metadata={"private": marker},
    )
    preflight = controller.turn_engine.preflight(
        turn_input=turn_input,
        turn_id=new_id("turn"),
    )
    character_context = controller.turn_engine.prepare_character_context(
        preflight=preflight
    )
    user_message_id = controller.message_store.save_message(
        session_id=turn_input.session_id,
        user_id=turn_input.user_id,
        role="user",
        content=turn_input.message,
    )

    prepared = controller.turn_engine.prepare(
        preflight=preflight,
        user_message_id=user_message_id,
        character_context=character_context,
    )

    assert marker not in repr(preflight)
    assert marker not in repr(prepared)
    assert "prompt_messages=" not in repr(prepared)
    assert "memory_results=" not in repr(prepared)


def test_model_failure_records_failed_trace_before_failed_prompt_log(
    controller_and_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _store, settings = controller_and_store
    events: list[str] = []

    def record_trace(event: Any) -> None:
        events.append(f"trace.{event.event_type}")

    def record_prompt(run: Any) -> None:
        events.append(f"prompt_log.{run.status}")

    class FailingResponseGenerator:
        def generate(self, **_kwargs: Any) -> str:
            events.append("model.generate")
            raise ValueError("safe_characterization_failure")

    monkeypatch.setattr(controller.trace_logger, "record", record_trace)
    monkeypatch.setattr(controller.prompt_run_logger, "record", record_prompt)
    controller.response_generator = FailingResponseGenerator()

    with pytest.raises(ValueError, match="safe_characterization_failure"):
        controller.handle_user_message(
            _turn_input(settings=settings, session_id=new_id("session"))
        )

    assert events[-4:] == [
        "trace.prompt_context_built",
        "model.generate",
        "trace.turn_failed",
        "prompt_log.failed",
    ]


def test_agent_package_contains_no_copied_streaming_controller() -> None:
    agent_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("app/agent").glob("*.py")
    )

    assert "class StreamingDialogueController" not in agent_source
