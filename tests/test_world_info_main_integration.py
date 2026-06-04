from __future__ import annotations

from app.retrieval.provider import WorldInfoEvidenceProvider


def test_main_build_controller_uses_world_info_evidence_provider(controller_and_store):
    controller, _store, _settings = controller_and_store

    assert isinstance(controller.evidence_provider, WorldInfoEvidenceProvider)
