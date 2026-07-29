from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.agent.context_budget import ContextBudgetPolicy, DeterministicContextCompressor
from app.agent.prompt_context import (
    PromptCompiler,
    PromptContext,
    PromptSection,
    estimate_prompt_chars,
)
from app.config import Settings
from app.embeddings.providers.hash import DeterministicHashEmbeddingProvider
from app.embeddings.runtime import EmbeddingProviderRuntime
from app.embeddings.schemas import EmbeddingRequest
from app.schemas import AgentTurnInput
from app.utils.ids import new_id


def _section(
    section_id: str,
    lines: list[str],
    source_ids: list[str] | None = None,
) -> PromptSection:
    return PromptSection(
        id=section_id,
        title=f"{section_id}:",
        content_lines=lines,
        source_ids=list(source_ids or []),
    )


def _compress(
    context: PromptContext,
    *,
    max_chars: int = 24_000,
    recent_limit: int = 8,
    memory_limit: int = 4,
    evidence_limit: int = 4,
    enabled: bool = True,
) -> PromptContext:
    return DeterministicContextCompressor(
        policy=ContextBudgetPolicy(
            enabled=enabled,
            max_chars=max_chars,
            recent_message_limit=recent_limit,
            memory_limit=memory_limit,
            evidence_limit=evidence_limit,
        )
    ).compress(context=context)


def _by_id(context: PromptContext, section_id: str) -> PromptSection:
    return next(section for section in context.sections if section.id == section_id)


def test_prompt_char_estimator_matches_compiled_message_content():
    context = PromptContext(
        user_input="现在呢？",
        sections=[
            _section("base_instructions", ["第一行", "second line"]),
            _section("style_examples", ["example head\nexample tail"], ["style:1"]),
        ],
    )

    messages = PromptCompiler().compile_messages(context=context)

    assert estimate_prompt_chars(context=context) == sum(
        len(message["content"]) for message in messages
    )


def test_disabled_budget_preserves_compiled_prompt_and_sections():
    context = PromptContext(
        user_input="hello",
        sections=[
            _section("base_instructions", ["base"]),
            _section("recent_conversation", ["old", "new"], ["msg:1", "msg:2"]),
        ],
        metadata={"memory_ids": ["memory:1"]},
    )

    compressed = _compress(context, max_chars=1, recent_limit=0, enabled=False)

    assert compressed.sections == context.sections
    assert PromptCompiler().compile_messages(
        context=compressed
    ) == PromptCompiler().compile_messages(context=context)
    assert compressed.metadata["memory_ids"] == ["memory:1"]
    assert compressed.metadata["context_budget"]["status"] == "disabled"
    assert compressed.metadata["context_budget"]["applied"] is False


def test_section_limits_keep_recent_tail_and_upstream_ranked_prefixes():
    recent_ids = [f"msg:{index}" for index in range(10)]
    memory_ids = [f"memory:{index}" for index in range(6)]
    canon_ids = [f"canon:{index}" for index in range(6)]
    voice_ids = [f"voice:{index}" for index in range(6)]
    context = PromptContext(
        user_input="current",
        sections=[
            _section("base_instructions", ["base"]),
            _section("style_examples", ["style 1", "style 2"], ["style:1", "style:2"]),
            _section("canon_evidence", canon_ids, canon_ids),
            _section("voice_evidence", voice_ids, voice_ids),
            _section("interaction_memories", memory_ids, memory_ids),
            _section("relationship_state", ["relationship"]),
            _section("recent_conversation", recent_ids, recent_ids),
        ],
        metadata={
            "memory_ids": memory_ids,
            "evidence_ids": {"lore": canon_ids, "voice": voice_ids},
        },
    )

    compressed = _compress(context, max_chars=100_000)
    report = compressed.metadata["context_budget"]

    assert _by_id(compressed, "recent_conversation").source_ids == recent_ids[-8:]
    assert _by_id(compressed, "interaction_memories").source_ids == memory_ids[:4]
    assert _by_id(compressed, "canon_evidence").source_ids == canon_ids[:4]
    assert _by_id(compressed, "voice_evidence").source_ids == voice_ids[:4]
    assert _by_id(compressed, "style_examples").source_ids == ["style:1", "style:2"]
    assert report["dropped_source_ids"] == {
        "canon_evidence": canon_ids[4:],
        "voice_evidence": voice_ids[4:],
        "interaction_memories": memory_ids[4:],
        "recent_conversation": recent_ids[:2],
    }
    assert compressed.metadata["memory_ids"] == memory_ids
    assert compressed.metadata["evidence_ids"] == {
        "lore": canon_ids,
        "voice": voice_ids,
    }


def test_global_budget_drops_style_examples_first_and_removes_empty_section():
    final_shape = PromptContext(
        user_input="current",
        sections=[
            _section("base_instructions", ["base"]),
            _section("voice_evidence", ["voice kept"], ["voice:1"]),
            _section("relationship_state", ["relationship"]),
        ],
    )
    context = PromptContext(
        user_input=final_shape.user_input,
        sections=[
            final_shape.sections[0],
            _section(
                "style_examples",
                ["drop-secret-a" * 10, "drop-secret-b" * 10],
                ["style:1", "style:2"],
            ),
            final_shape.sections[1],
            final_shape.sections[2],
        ],
    )

    compressed = _compress(context, max_chars=estimate_prompt_chars(context=final_shape))
    report = compressed.metadata["context_budget"]

    assert [section.id for section in compressed.sections] == [
        "base_instructions",
        "voice_evidence",
        "relationship_state",
    ]
    assert report["dropped_section_ids"] == ["style_examples"]
    assert report["dropped_source_ids"]["style_examples"] == ["style:1", "style:2"]
    assert report["estimated_after"] <= report["budget_limit"]
    assert "drop-secret" not in json.dumps(report)


def test_global_budget_drops_oldest_recent_messages_first():
    final_shape = PromptContext(
        user_input="current",
        sections=[
            _section("base_instructions", ["base"]),
            _section("recent_conversation", ["latest"], ["msg:3"]),
        ],
    )
    context = PromptContext(
        user_input=final_shape.user_input,
        sections=[
            final_shape.sections[0],
            _section(
                "recent_conversation",
                ["oldest" * 10, "middle" * 10, "latest"],
                ["msg:1", "msg:2", "msg:3"],
            ),
        ],
    )

    compressed = _compress(context, max_chars=estimate_prompt_chars(context=final_shape))

    assert _by_id(compressed, "recent_conversation").source_ids == ["msg:3"]
    assert compressed.metadata["context_budget"]["dropped_source_ids"] == {
        "recent_conversation": ["msg:1", "msg:2"]
    }


def test_global_budget_preserves_complete_cross_section_trim_priority():
    trim_order = [
        "style_examples",
        "voice_evidence",
        "canon_evidence",
        "interaction_memories",
        "recent_conversation",
    ]
    context = PromptContext(
        user_input="current",
        sections=[
            _section("base_instructions", ["base"]),
            _section("style_examples", ["style" * 20], ["style:1"]),
            _section("canon_evidence", ["canon" * 20], ["canon:1"]),
            _section("voice_evidence", ["voice" * 20], ["voice:1"]),
            _section("interaction_memories", ["memory" * 20], ["memory:1"]),
            _section("relationship_state", ["relationship"]),
            _section("recent_conversation", ["recent" * 20], ["message:1"]),
        ],
    )

    for drop_count in range(1, len(trim_order) + 1):
        expected_dropped = set(trim_order[:drop_count])
        target = PromptContext(
            user_input=context.user_input,
            sections=[
                section for section in context.sections if section.id not in expected_dropped
            ],
        )

        compressed = _compress(
            context,
            max_chars=estimate_prompt_chars(context=target),
            recent_limit=10,
            memory_limit=10,
            evidence_limit=10,
        )
        report = compressed.metadata["context_budget"]

        assert set(report["dropped_section_ids"]) == expected_dropped
        assert [
            section_id
            for section_id in trim_order
            if any(section.id == section_id for section in compressed.sections)
        ] == trim_order[drop_count:]


def test_protected_and_unclassified_sections_survive_impossible_budget():
    context = PromptContext(
        user_input="current input must stay",
        sections=[
            _section("base_instructions", ["protected base" * 20]),
            _section("relationship_state", ["protected relationship" * 20]),
            _section("future_safety_rules", ["future protected" * 20]),
            _section("style_examples", ["optional" * 20], ["style:1"]),
        ],
    )

    compressed = _compress(context, max_chars=1)
    report = compressed.metadata["context_budget"]

    assert compressed.user_input == context.user_input
    assert [section.id for section in compressed.sections] == [
        "base_instructions",
        "relationship_state",
        "future_safety_rules",
    ]
    assert report["status"] == "protected_floor_exceeded"
    assert report["over_budget_chars"] > 0
    assert report["unclassified_section_ids"] == ["future_safety_rules"]


def test_invalid_trimmable_item_mapping_is_fail_safe_protected():
    context = PromptContext(
        user_input="current",
        sections=[
            _section("base_instructions", ["base"]),
            _section("canon_evidence", ["one", "two"], ["canon:1"]),
        ],
    )

    compressed = _compress(context, max_chars=1, evidence_limit=0)
    report = compressed.metadata["context_budget"]

    assert _by_id(compressed, "canon_evidence").content_lines == ["one", "two"]
    assert report["invalid_item_mapping_section_ids"] == ["canon_evidence"]
    assert report["status"] == "protected_floor_exceeded"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_chars", 0),
        ("recent_message_limit", -1),
        ("memory_limit", -1),
        ("evidence_limit", -1),
    ],
)
def test_context_budget_policy_rejects_invalid_limits(field: str, value: int):
    kwargs = {field: value}

    with pytest.raises(ValueError):
        ContextBudgetPolicy(**kwargs)


def test_settings_reads_context_budget_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AEMEATH_ENABLE_CONTEXT_BUDGET", "true")
    monkeypatch.setenv("AEMEATH_CONTEXT_BUDGET_MAX_CHARS", "12345")
    monkeypatch.setenv("AEMEATH_CONTEXT_BUDGET_RECENT_MESSAGE_LIMIT", "7")
    monkeypatch.setenv("AEMEATH_CONTEXT_BUDGET_MEMORY_LIMIT", "3")
    monkeypatch.setenv("AEMEATH_CONTEXT_BUDGET_EVIDENCE_LIMIT", "2")

    settings = Settings.from_env()

    assert settings.enable_context_budget is True
    assert settings.context_budget_max_chars == 12345
    assert settings.context_budget_recent_message_limit == 7
    assert settings.context_budget_memory_limit == 3
    assert settings.context_budget_evidence_limit == 2


def test_cli_and_api_runtime_use_the_same_context_budget_policy(settings, tmp_path: Path):
    from app.api.server import create_app
    from app.main import build_controller

    configured = replace(
        settings,
        db_path=tmp_path / "cli.db",
        enable_context_budget=True,
        context_budget_max_chars=1234,
        context_budget_recent_message_limit=7,
        context_budget_memory_limit=3,
        context_budget_evidence_limit=2,
    )
    cli_runtime = build_controller(settings=configured)
    controller = cli_runtime.agent_service.controller
    api_app = create_app(replace(configured, db_path=tmp_path / "api.db"))

    cli_policy = controller.context_builder.context_compressor.policy
    api_policy = (
        api_app.state.runtime.agent_service.controller.context_builder.context_compressor.policy
    )

    assert cli_policy == api_policy


class _RecordingHashProvider:
    def __init__(self):
        self.delegate = DeterministicHashEmbeddingProvider()
        self.requests: list[EmbeddingRequest] = []

    @property
    def signature(self):
        return self.delegate.signature

    def embed(self, request: EmbeddingRequest):
        self.requests.append(request)
        return self.delegate.embed(request)


def test_context_budget_does_not_change_t0135_semantic_query(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.main import build_controller

    providers: list[_RecordingHashProvider] = []

    def build_provider(*_args, **_kwargs):
        provider = _RecordingHashProvider()
        providers.append(provider)
        return EmbeddingProviderRuntime(provider=provider, _close=lambda: None)

    monkeypatch.setattr("app.main.build_memory_embedding_runtime", build_provider)
    base = replace(
        settings,
        enable_long_term_memory=True,
        memory_embedding_provider="hash",
    )
    disabled_runtime = build_controller(
        settings=replace(
            base,
            db_path=tmp_path / "disabled.db",
            enable_context_budget=False,
        )
    )
    enabled_runtime = build_controller(
        settings=replace(
            base,
            db_path=tmp_path / "enabled.db",
            enable_context_budget=True,
            context_budget_max_chars=1,
        )
    )

    for controller in (
        disabled_runtime.agent_service.controller,
        enabled_runtime.agent_service.controller,
    ):
        controller.handle_user_message(
            AgentTurnInput(
                user_id=settings.default_user_id,
                session_id=new_id("session"),
                character_id=settings.default_character_id,
                message="semantic query must stay identical",
                metadata={},
            )
        )

    assert len(providers) == 2
    assert len(providers[0].requests) == len(providers[1].requests) == 1
    assert providers[0].requests[0] == providers[1].requests[0]


def test_budget_trace_and_prompt_log_exclude_dropped_body(settings, tmp_path: Path):
    from app.main import build_controller

    configured = replace(
        settings,
        db_path=tmp_path / "logged.db",
        enable_context_budget=True,
        context_budget_max_chars=1,
        enable_trace_log=True,
        trace_log_root=tmp_path / "traces",
        enable_prompt_run_log=True,
        prompt_run_log_root=tmp_path / "prompts",
    )
    runtime = build_controller(settings=configured)
    controller, resolved = runtime.agent_service.controller, runtime.settings
    character = controller.character_provider.build_runtime_context(
        character_id=resolved.default_character_id,
        user_input="log budget test",
        metadata={},
    )
    dropped_marker = character.style_examples[0].splitlines()[0]

    controller.handle_user_message(
        AgentTurnInput(
            user_id=resolved.default_user_id,
            session_id=new_id("session"),
            character_id=resolved.default_character_id,
            message="log budget test",
            metadata={},
        )
    )

    [trace_path] = list(resolved.trace_log_root.glob("*/*/*/*.jsonl"))
    trace_records = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    [prompt_path] = list(resolved.prompt_run_log_root.glob("*/*/*/*.jsonl"))
    prompt_raw = prompt_path.read_text(encoding="utf-8")
    prompt_record = json.loads(prompt_raw)
    budget_events = [
        record for record in trace_records if record["event_type"] == "context_budget_applied"
    ]

    assert len(budget_events) == 1
    assert budget_events[0]["payload"]["status"] == "protected_floor_exceeded"
    assert prompt_record["prompt_context_metadata"]["context_budget"]["budget_enabled"] is True
    assert not any(section["id"] == "style_examples" for section in prompt_record["sections"])
    assert dropped_marker not in trace_path.read_text(encoding="utf-8")
    assert dropped_marker not in prompt_raw
