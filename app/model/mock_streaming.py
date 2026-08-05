from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from app.model.streaming import (
    ModelStreamEvent,
    ModelStreamFinished,
    ModelStreamRequest,
    ModelTextDelta,
)


class MockModelStreamSession:
    def __init__(
        self,
        *,
        events: tuple[ModelStreamEvent, ...],
        failure: Exception | None,
        fail_after_events: int | None,
    ) -> None:
        self._events = events
        self._failure = failure
        self._fail_after_events = fail_after_events
        self._lock = Lock()
        self._closed = False
        self._cancelled = False
        self._iterated = False
        self.close_count = 0

    def __iter__(self) -> Iterator[ModelStreamEvent]:
        with self._lock:
            if self._iterated:
                raise RuntimeError("model_stream_session_already_iterated")
            self._iterated = True
        emitted = 0
        for event in self._events:
            with self._lock:
                if self._closed or self._cancelled:
                    return
            if self._failure is not None and self._fail_after_events == emitted:
                raise self._failure
            yield event
            emitted += 1
        if self._failure is not None and (
            self._fail_after_events is None or self._fail_after_events == emitted
        ):
            raise self._failure

    def cancel(self, *, reason: str) -> None:
        del reason
        with self._lock:
            self._cancelled = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.close_count += 1


class MockStreamingChatModelClient:
    def __init__(
        self,
        *,
        events: tuple[ModelStreamEvent, ...] | None = None,
        failure: Exception | None = None,
        fail_after_events: int | None = None,
    ) -> None:
        self._events = (
            events
            if events is not None
            else (
                ModelTextDelta("mock response"),
                ModelStreamFinished("complete"),
            )
        )
        self._failure = failure
        self._fail_after_events = fail_after_events
        self.open_count = 0
        self.last_request: ModelStreamRequest | None = None
        self.last_session: MockModelStreamSession | None = None

    @contextmanager
    def open_stream(
        self,
        *,
        request: ModelStreamRequest,
    ) -> Iterator[MockModelStreamSession]:
        self.open_count += 1
        self.last_request = request
        session = MockModelStreamSession(
            events=self._events,
            failure=self._failure,
            fail_after_events=self._fail_after_events,
        )
        self.last_session = session
        try:
            yield session
        finally:
            session.close()
