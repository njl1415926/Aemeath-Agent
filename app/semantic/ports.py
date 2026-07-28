from __future__ import annotations

from typing import Protocol

from app.semantic.schemas import SemanticTurnInput, TurnSemanticContext


class SemanticQueryCoordinatorPort(Protocol):
    def prepare_turn_context(
        self,
        *,
        turn_input: SemanticTurnInput,
    ) -> TurnSemanticContext:
        ...
