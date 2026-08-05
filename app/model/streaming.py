from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol, TypeAlias


@dataclass(frozen=True)
class ModelStreamRequest:
    messages: list[dict[str, str]] = field(repr=False)


@dataclass(frozen=True)
class ModelTextDelta:
    text: str = field(repr=False)


@dataclass(frozen=True)
class ModelStreamFinished:
    finish_kind: Literal["complete", "output_limit"]


ModelStreamEvent: TypeAlias = ModelTextDelta | ModelStreamFinished


class ModelStreamSession(Protocol):
    def __iter__(self) -> Iterator[ModelStreamEvent]: ...

    def cancel(self, *, reason: str) -> None: ...

    def close(self) -> None: ...


class StreamingChatModelClient(Protocol):
    def open_stream(
        self,
        *,
        request: ModelStreamRequest,
    ) -> AbstractContextManager[ModelStreamSession]: ...
