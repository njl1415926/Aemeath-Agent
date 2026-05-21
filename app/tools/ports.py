from __future__ import annotations

from typing import Protocol

from app.schemas import AgentTurnInput
from app.tools.schemas import (
    ToolCall,
    ToolContext,
    ToolNeedDecision,
    ToolPolicyDecision,
    ToolResult,
    ToolSpec,
)


class ToolRegistryPort(Protocol):
    def list_tools(self, *, context: ToolContext) -> list[ToolSpec]:
        ...

    def get_tool(self, *, name: str, context: ToolContext) -> ToolSpec | None:
        ...


class ToolNeedDetectorPort(Protocol):
    def decide(
        self,
        *,
        turn_input: AgentTurnInput,
        response_mode: str,
        available_tools: list[ToolSpec],
    ) -> ToolNeedDecision:
        ...


class ToolPolicyPort(Protocol):
    def authorize(self, *, tool_call: ToolCall, context: ToolContext) -> ToolPolicyDecision:
        ...


class ToolExecutorPort(Protocol):
    def execute(self, *, tool_call: ToolCall, context: ToolContext) -> ToolResult:
        ...
