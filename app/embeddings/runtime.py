from __future__ import annotations

from dataclasses import dataclass, field
from threading import Condition, Lock, get_ident
from typing import Callable

from app.embeddings.ports import EmbeddingProviderPort


@dataclass
class EmbeddingProviderRuntime:
    provider: EmbeddingProviderPort
    _close: Callable[[], None] = field(repr=False)
    _condition: Condition = field(
        default_factory=lambda: Condition(Lock()), init=False, repr=False
    )
    _state: str = field(default="open", init=False, repr=False)
    _closing_thread_id: int | None = field(default=None, init=False, repr=False)

    def close(self) -> None:
        current_thread_id = get_ident()
        with self._condition:
            if self._state == "closed":
                return
            if self._state == "closing":
                if self._closing_thread_id == current_thread_id:
                    return
                while self._state != "closed":
                    self._condition.wait()
                return
            self._state = "closing"
            self._closing_thread_id = current_thread_id
        try:
            self._close()
        finally:
            with self._condition:
                self._state = "closed"
                self._closing_thread_id = None
                self._condition.notify_all()
