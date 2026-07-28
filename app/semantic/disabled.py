from __future__ import annotations

from app.semantic.budget import SEMANTIC_QUERY_BUDGET_V1
from app.semantic.query_builder import SEMANTIC_QUERY_CONSTRUCTION_VERSION
from app.semantic.schemas import (
    SEMANTIC_QUERY_ROLES,
    SemanticQueryResolution,
    SemanticTurnInput,
    TurnSemanticContext,
)


class DisabledSemanticQueryCoordinator:
    def prepare_turn_context(
        self,
        *,
        turn_input: SemanticTurnInput,
    ) -> TurnSemanticContext:
        return TurnSemanticContext(
            turn_id=turn_input.turn_id,
            status="not_requested",
            construction_version=SEMANTIC_QUERY_CONSTRUCTION_VERSION,
            budget_version=SEMANTIC_QUERY_BUDGET_V1,
            query_strategy="none",
            budget_reason_codes=(),
            provider=None,
            profile_id=None,
            signature_fingerprint=None,
            requested_role_count=0,
            unique_query_count=0,
            dependency_message_count=0,
            query_char_count=0,
            query_utf8_bytes=0,
            provider_called=False,
            duration_ms=0.0,
            resolutions=tuple(
                SemanticQueryResolution(
                    role=role,
                    status="not_requested",
                    reason_code="semantic_role_not_requested",
                    request_key=None,
                    resolved_embedding=None,
                )
                for role in SEMANTIC_QUERY_ROLES
            ),
        )
