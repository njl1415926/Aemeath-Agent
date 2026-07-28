from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from app.semantic.errors import SemanticQueryConfigurationError

SEMANTIC_QUERY_BUDGET_V1 = "semantic-query-budget/v1"

_CANONICAL_SEMANTIC_QUERY_BUDGETS: Final = MappingProxyType(
    {
        SEMANTIC_QUERY_BUDGET_V1: (1024, 3072, 4096, 8000),
    }
)


@dataclass(frozen=True)
class SemanticQueryBudget:
    version: Literal["semantic-query-budget/v1"] = SEMANTIC_QUERY_BUDGET_V1
    max_dependency_chars_per_message: int = 1024
    max_dependency_utf8_bytes_per_message: int = 3072
    max_query_chars: int = 4096
    max_query_utf8_bytes: int = 8000

    def __post_init__(self) -> None:
        canonical = _CANONICAL_SEMANTIC_QUERY_BUDGETS.get(self.version)
        actual = (
            self.max_dependency_chars_per_message,
            self.max_dependency_utf8_bytes_per_message,
            self.max_query_chars,
            self.max_query_utf8_bytes,
        )
        if canonical is None or actual != canonical:
            raise SemanticQueryConfigurationError(
                reason="semantic_query_budget_definition_mismatch"
            )


def load_semantic_query_budget(
    version: str = SEMANTIC_QUERY_BUDGET_V1,
) -> SemanticQueryBudget:
    canonical = _CANONICAL_SEMANTIC_QUERY_BUDGETS.get(version)
    if canonical is None:
        raise SemanticQueryConfigurationError(
            reason="semantic_query_budget_definition_mismatch"
        )
    return SemanticQueryBudget(
        version=SEMANTIC_QUERY_BUDGET_V1,
        max_dependency_chars_per_message=canonical[0],
        max_dependency_utf8_bytes_per_message=canonical[1],
        max_query_chars=canonical[2],
        max_query_utf8_bytes=canonical[3],
    )
