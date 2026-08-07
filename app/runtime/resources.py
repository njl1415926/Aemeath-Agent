from __future__ import annotations

from dataclasses import dataclass
from threading import Condition, Lock, get_ident
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from app.config import Settings
    from app.db.store_factory import StoreFacade
    from app.memory.admin import MemoryAdminService
    from app.observability.ports import AppLoggerPort
    from app.runtime.streaming import ActiveStreamRegistry
    from app.services.agent_service import AgentService
    from app.services.agent_stream_service import AgentStreamService


@dataclass(frozen=True)
class ResourceCloseFailure:
    resource_name: str
    error_type: str


class RuntimeResources:
    """Own application resources and close each one at most once, in reverse order."""

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._state: Literal["open", "closing", "closed"] = "open"
        self._resources: list[tuple[str, Callable[[], None]]] = []
        self._failures: tuple[ResourceCloseFailure, ...] = ()
        self._closing_thread_id: int | None = None

    def register(self, *, name: str, close: Callable[[], None]) -> None:
        if not name or not callable(close):
            raise ValueError("runtime resource name and close callback are required")
        with self._condition:
            if self._state != "open":
                raise RuntimeError("runtime_resources_closed")
            self._resources.append((name, close))

    def close(self) -> tuple[ResourceCloseFailure, ...]:
        current_thread_id = get_ident()
        with self._condition:
            if self._state == "closed":
                return self._failures
            if self._state == "closing":
                if self._closing_thread_id == current_thread_id:
                    return self._failures
                while self._state != "closed":
                    self._condition.wait()
                return self._failures
            self._state = "closing"
            self._closing_thread_id = current_thread_id
            resources = tuple(reversed(self._resources))
            self._resources.clear()

        failures: list[ResourceCloseFailure] = []
        for name, close in resources:
            try:
                close()
            except BaseException as exc:  # shutdown must continue through all resources
                failures.append(
                    ResourceCloseFailure(
                        resource_name=name,
                        error_type=type(exc).__name__,
                    )
                )

        with self._condition:
            self._failures = tuple(failures)
            self._state = "closed"
            self._closing_thread_id = None
            self._condition.notify_all()
            return self._failures


@dataclass
class ApplicationRuntime:
    surface: Literal["cli", "api"]
    agent_service: AgentService
    memory_admin_service: MemoryAdminService
    store: StoreFacade
    settings: Settings
    resources: RuntimeResources
    app_logger: AppLoggerPort
    agent_stream_service: AgentStreamService | None = None
    active_stream_registry: ActiveStreamRegistry | None = None

    def close(self) -> tuple[ResourceCloseFailure, ...]:
        failures = self.resources.close()
        if failures:
            from app.errors import redact_sensitive
            from app.observability.schemas import AppLogRecord

            for failure in failures:
                self.app_logger.record(
                    AppLogRecord(
                        event_name="runtime.resource_close_failed",
                        level="error",
                        payload=redact_sensitive(
                            {
                                "resource_name": failure.resource_name,
                                "error_type": failure.error_type,
                            }
                        ),
                    )
                )
        return failures
