from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalResult:
    id: str
    namespace: str
    content: str
    source_type: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidencePack:
    memory_results: list[RetrievalResult] = field(default_factory=list)
    lore_results: list[RetrievalResult] = field(default_factory=list)
    voice_results: list[RetrievalResult] = field(default_factory=list)

