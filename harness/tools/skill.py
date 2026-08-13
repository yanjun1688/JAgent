from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from harness.models.tools import SideEffect, ToolDefinition
from harness.tools.base import BaseTool

_log = logging.getLogger("harness.agent.skill")


def _make_executor_wrapper(
    executor: Any,
    tool_name: str,
    tool_def: ToolDefinition,
    tool_fn: Callable,
) -> Callable:
    """Wrap a raw tool_fn so it routes through ToolExecutor (guardrails, idempotency, events)."""
    from harness.tools.executor import (
        ConfirmationNeededError,
        ExecutionStatus,
        current_run_id,
    )

    async def wrapper(input: dict[str, Any]) -> Any:
        run_id = current_run_id.get()
        if not run_id:
            _log.warning(
                "[bypass] tool=%s no run_id context — calling tool_fn directly (bypasses Tool Layer)", tool_name
            )
            return await tool_fn(input)
        result = await executor.execute(run_id, tool_name, input, tool_def, tool_fn)
        if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
            return result.output
        if result.status == ExecutionStatus.CONFIRMATION_NEEDED and result.confirmation_id:
            raise ConfirmationNeededError(tool_name, result.confirmation_id)
        raise RuntimeError(f"Tool '{tool_name}' failed: {result.error or result.status.value}")

    return wrapper


class Skill(BaseTool):
    """A multi-step skill package — ADR-010 D-06: declared as a single BaseTool.

    Externally appears as a single ToolDefinition with a single invoker.
    Internally ``run()`` orchestrates multiple steps, potentially using other
    registered tools (routed through the trusted ToolExecutor when a tool layer
    is wired via ``with_tool_layer``).

    Usage:
        skill = Skill(
            name="research_topic",
            description="Research a topic by searching the web and summarizing findings",
            input_schema={...},
            steps=[step1, step2],
        )
        registry.register_tool(skill)
    """

    side_effects = [SideEffect.EXTERNAL]

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        steps: list[Callable[[dict[str, Any], dict[str, Callable]], Any]],
        output_schema: dict[str, Any] | None = None,
        timeout_ms: int = 120000,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema or {"type": "object", "properties": {"result": {"type": "string"}}}
        self.steps = steps
        self.timeout_ms = timeout_ms
        self.idempotency_key_fields = list(self.input_schema.get("properties", {}).keys())
        self._executor: Any = None
        self._tool_fns_provider: Callable[[], dict[str, Callable]] | None = None
        self._tool_defs_provider: Callable[[], list[ToolDefinition]] | None = None

    @property
    def definition(self) -> ToolDefinition:
        """Backward-compatible accessor — same contract as ``to_definition()``."""
        return self.to_definition()

    def with_tool_layer(
        self,
        executor: Any,
        tool_fns_provider: Callable[[], dict[str, Callable]],
        tool_defs_provider: Callable[[], list[ToolDefinition]],
    ) -> "Skill":
        """Wire internal sub-tool calls through the trusted ToolExecutor."""
        self._executor = executor
        self._tool_fns_provider = tool_fns_provider
        self._tool_defs_provider = tool_defs_provider
        return self

    async def run(self, input: dict) -> dict[str, Any]:
        context: dict[str, Any] = {"input": input, "intermediate": {}}
        raw_fns = self._tool_fns_provider() if self._tool_fns_provider else {}

        if self._executor is not None and self._tool_defs_provider is not None:
            tool_defs = {td.name: td for td in self._tool_defs_provider()}
            wrapped_fns: dict[str, Callable] = {}
            for name, fn in raw_fns.items():
                td = tool_defs.get(name)
                wrapped_fns[name] = _make_executor_wrapper(self._executor, name, td, fn) if td else fn
        else:
            wrapped_fns = raw_fns

        for step_fn in self.steps:
            result = step_fn(context, wrapped_fns)
            if asyncio.iscoroutine(result):
                result = await result
            context["intermediate"].update(result if isinstance(result, dict) else {"result": result})
        return {"result": context["intermediate"]}

    def build_fn(
        self,
        tool_fns_provider: Callable[[], dict[str, Callable]],
        executor: Any | None = None,
        tool_defs_provider: Callable[[], list[ToolDefinition]] | None = None,
    ) -> Callable[[dict[str, Any]], Any]:
        """Backward-compatible accessor — returns a callable skill_fn(input)."""
        self._tool_fns_provider = tool_fns_provider
        if executor is not None and tool_defs_provider is not None:
            self.with_tool_layer(executor, tool_fns_provider, tool_defs_provider)

        async def skill_fn(input: dict[str, Any]) -> dict[str, Any]:
            return await self.run(input)

        return skill_fn
