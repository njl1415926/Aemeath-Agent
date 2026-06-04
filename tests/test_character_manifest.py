from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from app.character.provider import CharacterProvider, CharacterRegistry, _format_style_example


def test_v02_directory_loads_through_character_registry():
    registry = CharacterRegistry.from_single_card(
        character_card_path=Path("app/character/Aemeath.v0.2"),
        default_character_id="aemeath",
    )

    profile = registry.load_profile(character_id="aemeath")
    context = registry.build_runtime_context(character_id="aemeath")

    assert profile.character_id == "aemeath"
    assert profile.version == "aemeath.v0.2"
    assert profile.english_name == "Aemeath"
    assert context.character_id == "aemeath"
    assert context.version == "aemeath.v0.2"


def test_v02_maps_runtime_context_key_sections():
    provider = CharacterProvider(Path("app/character/Aemeath.v0.2"), character_id="aemeath")

    context = provider.build_runtime_context(character_id="aemeath")

    assert context.identity
    assert context.relation_to_user
    assert context.persona_rules
    assert context.speech_rules
    assert context.boundaries
    assert context.forbidden_claims
    assert context.avoid
    assert context.style_examples
    assert any(rule.startswith("stable_lore_summary:") for rule in context.persona_rules)


def test_v02_full_style_examples_are_injected_without_mode():
    provider = CharacterProvider(Path("app/character/Aemeath.v0.2"), character_id="aemeath")

    context = provider.build_runtime_context(character_id="aemeath")

    assert len(context.style_examples) == 25
    assert all("User context:" in example for example in context.style_examples)
    assert any("Sample user input:" in example for example in context.style_examples)


def test_v02_context_tags_select_non_example_rules():
    provider = CharacterProvider(Path("app/character/Aemeath.v0.2"), character_id="aemeath")

    default = provider.build_runtime_context(character_id="aemeath")
    lore = provider.build_runtime_context(
        character_id="aemeath",
        metadata={"character_context_tags": ["lore_answer"]},
    )

    assert len(lore.persona_rules) > len(default.persona_rules)
    assert any(rule.startswith("stable_lore_summary:") for rule in lore.persona_rules)


def test_style_examples_support_response_and_responses_forms():
    single = {
        "id": "single",
        "user_context": "user greets Aemeath.",
        "response": "Hi.",
    }
    multiple = {
        "id": "multiple",
        "user_context": "user cares about Aemeath.",
        "responses": ["I am resting.", "You should rest too."],
    }

    assert "Aemeath response: Hi." in _format_style_example(single)
    formatted_multiple = _format_style_example(multiple)
    assert "Aemeath response variants:" in formatted_multiple
    assert "- I am resting." in formatted_multiple


def test_v02_load_failure_falls_back_to_v01_folder(tmp_path: Path):
    broken_dir = tmp_path / "broken_character"
    broken_dir.mkdir()
    (broken_dir / "manifest.yaml").write_text(
        "schema_version: character_manifest.v0.2\ncomponents:\n  profile: missing.yaml\n",
        encoding="utf-8",
    )

    provider = CharacterProvider(broken_dir, character_id="aemeath")
    profile = provider.load_profile(character_id="aemeath")
    context = provider.build_runtime_context(character_id="aemeath")

    assert profile.version == "aemeath.v0.1"
    assert context.version == "aemeath.v0.1"
    assert "character_source_fallback" in context.source_doc_ids
    assert any(broken_dir.as_posix() in source_id for source_id in context.source_doc_ids)
    assert any("Aemeath.v0.1/character.yaml" in source_id for source_id in context.source_doc_ids)


def test_legacy_root_yaml_explicit_path_still_loads():
    provider = CharacterProvider(
        Path("app/character/aemeath.v0.1.yaml"),
        character_id="aemeath",
    )

    profile = provider.load_profile(character_id="aemeath")
    context = provider.build_runtime_context(character_id="aemeath")

    assert profile.version == "aemeath.v0.1"
    assert context.version == "aemeath.v0.1"
    assert context.source_doc_ids == ["app/character/aemeath.v0.1.yaml"]


def test_v02_manifest_can_select_short_example_bank_with_responses(tmp_path: Path):
    source_dir = Path("app/character/Aemeath.v0.2")
    character_dir = tmp_path / "Aemeath.v0.2"
    shutil.copytree(source_dir, character_dir)
    manifest_path = character_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
    manifest["components"]["style_examples"] = "style_examples_short.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    provider = CharacterProvider(character_dir, character_id="aemeath")
    context = provider.build_runtime_context(character_id="aemeath")

    assert any("Aemeath response variants:" in example for example in context.style_examples)


def test_dialogue_controller_does_not_read_character_files_directly():
    controller_source = Path("app/agent/dialogue_controller.py").read_text(encoding="utf-8")

    assert "Aemeath.v0.2" not in controller_source
    assert "manifest.yaml" not in controller_source
    assert "load_character" not in controller_source
