from __future__ import annotations

import json
from pathlib import Path

from app.agent.context_builder import ContextBuilder
from app.character.schemas import CharacterRuntimeContext
from app.retrieval.provider import WorldInfoEvidenceProvider
from app.retrieval.world_info import WorldInfoKeywordRetriever, WorldInfoLoader
from app.schemas import RelationshipState


def _write_world_info(path: Path) -> None:
    data = {
        "entries": {
            "1": {
                "uid": 1,
                "comment": "Trigger Entry",
                "content": "Trigger lore evidence.",
                "key": ["TriggerTopic"],
                "keysecondary": [],
                "constant": False,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 100,
                "depth": 4,
                "probability": 100,
            },
            "86": {
                "uid": 86,
                "comment": "Base Worldview",
                "content": "Base worldview evidence.",
                "key": [],
                "keysecondary": [],
                "constant": True,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 100,
                "depth": 4,
                "probability": 100,
            },
            "92": {
                "uid": 92,
                "comment": "Marker Constant",
                "content": "<marker>",
                "key": [],
                "keysecondary": [],
                "constant": True,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 100,
                "depth": 4,
                "probability": 100,
            },
            "160": {
                "uid": 160,
                "comment": "Home",
                "content": "Home evidence.",
                "key": ["Home", "House"],
                "keysecondary": [],
                "constant": False,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 100,
                "depth": 4,
                "probability": 100,
            },
            "2": {
                "uid": 2,
                "comment": "Shorekeeper",
                "content": "Shorekeeper lore evidence.",
                "key": ["Shorekeeper"],
                "keysecondary": [],
                "constant": False,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 100,
                "depth": 4,
                "probability": 100,
            },
            "3": {
                "uid": 3,
                "comment": "Aemeath View",
                "content": "Aemeath remembers Shorekeeper as someone who calms Rover.",
                "key": ["Shorekeeper"],
                "keysecondary": [],
                "constant": False,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 101,
                "depth": 4,
                "probability": 100,
            },
            "200": {
                "uid": 200,
                "comment": "Non Trigger",
                "content": "Non trigger lore evidence.",
                "key": ["MinorTopic"],
                "keysecondary": [],
                "constant": False,
                "disable": False,
                "selective": True,
                "selectiveLogic": 0,
                "position": 0,
                "order": 102,
                "depth": 4,
                "probability": 100,
            },
        },
        "originalData": {"entries": []},
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _provider(path: Path, **kwargs) -> WorldInfoEvidenceProvider:
    retriever = WorldInfoKeywordRetriever(loader=WorldInfoLoader(path))
    return WorldInfoEvidenceProvider(retriever=retriever, **kwargs)


def test_world_info_evidence_provider_returns_keyword_lore_results(tmp_path: Path):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Tell me about Shorekeeper.",
    )

    assert [item.metadata["comment"] for item in pack.lore_results] == [
        "Base Worldview",
        "Shorekeeper",
        "Aemeath View",
    ]
    assert pack.voice_results == []
    assert [item.namespace for item in pack.lore_results] == [
        "world:constant",
        "world:keyword",
        "world:keyword",
    ]


def test_world_info_evidence_provider_returns_empty_pack_without_key_match(tmp_path: Path):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Good morning.",
    )

    assert pack.lore_results == []
    assert pack.voice_results == []


def test_world_info_evidence_provider_supports_constant_metadata_override(tmp_path: Path):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path, keyword_limit=1)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Tell me about Shorekeeper.",
        metadata={
            "include_world_info_constants": True,
            "constant_limit": 1,
        },
    )

    constant_results = [item for item in pack.lore_results if item.namespace == "world:constant"]
    keyword_results = [item for item in pack.lore_results if item.namespace == "world:keyword"]
    assert [item.metadata["comment"] for item in constant_results] == ["Base Worldview"]
    assert [item.metadata["comment"] for item in keyword_results] == ["Shorekeeper"]


def test_world_info_evidence_provider_only_adds_base_worldview_for_trigger_uids(
    tmp_path: Path,
):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Tell me about MinorTopic.",
    )

    comments = [item.metadata["comment"] for item in pack.lore_results]
    assert comments == ["Non Trigger"]


def test_world_info_evidence_provider_adds_base_worldview_for_configured_trigger_uids(
    tmp_path: Path,
):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Tell me about TriggerTopic.",
    )

    comments = [item.metadata["comment"] for item in pack.lore_results]
    assert comments == ["Base Worldview", "Trigger Entry"]


def test_world_info_evidence_provider_returns_home_only_by_keyword_match(
    tmp_path: Path,
):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path)

    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Tell me about Home.",
    )

    assert [item.metadata["comment"] for item in pack.lore_results] == ["Home"]
    assert pack.lore_results[0].namespace == "world:keyword"


def test_world_info_evidence_provider_results_enter_canon_evidence_prompt(tmp_path: Path):
    path = tmp_path / "world_info.json"
    _write_world_info(path)
    provider = _provider(path)
    pack = provider.build_evidence_pack(
        user_id="user_default",
        character_id="aemeath",
        query="Tell me about Shorekeeper.",
    )
    builder = ContextBuilder(Path("app/prompts/dialogue_prompt.v0.1.md"))
    context = builder.build_context(
        character_context=CharacterRuntimeContext(
            character_id="aemeath",
            display_name="Aemeath",
            english_name="Aemeath",
            version="test",
            canon_point="test",
            identity="test identity",
            relation_to_user="test relation",
        ),
        user_input="Tell me about Shorekeeper.",
        recent_messages=[],
        long_term_memories=[],
        relationship_state=RelationshipState(),
        evidence_pack=pack,
    )

    canon_section = next(section for section in context.sections if section.id == "canon_evidence")
    assert any("Shorekeeper lore evidence." in line for line in canon_section.content_lines)
    assert context.metadata["evidence_ids"]["lore"] == [
        "world_info:86",
        "world_info:2",
        "world_info:3",
    ]
