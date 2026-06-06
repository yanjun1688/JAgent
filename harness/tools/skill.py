from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from harness.models.tools import SideEffect, ToolDefinition

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
            _log.warning("[bypass] tool=%s no run_id context — calling tool_fn directly (bypasses Tool Layer)", tool_name)
            return await tool_fn(input)
        result = await executor.execute(run_id, tool_name, input, tool_def, tool_fn)
        if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
            return result.output
        if result.status == ExecutionStatus.CONFIRMATION_NEEDED and result.confirmation_id:
            raise ConfirmationNeededError(tool_name, result.confirmation_id)
        raise RuntimeError(f"Tool '{tool_name}' failed: {result.error or result.status.value}")

    return wrapper


class Skill:
    """A multi-step skill package.

    Externally appears as a single ToolDefinition with a single implementation function.
    Internally orchestrates multiple steps, potentially using other registered tools.

    When executor and tool_defs_provider are provided, internal tool calls within steps
    are routed through the ToolExecutor (guardrails, idempotency, events, confirmation).
    Otherwise, tool_fns are called directly (backward compatible).

    Usage:
        skill = Skill(
            name="research_topic",
            description="Research a topic by searching the web and summarizing findings",
            input_schema={...},
            steps=[step1, step2],
        )
        registry.register(skill.definition, skill.build_fn(registry.list_tool_fns))
    """

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

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            idempotency_key_fields=list(self.input_schema.get("properties", {}).keys()),
            side_effects=[SideEffect.EXTERNAL],
            timeout_ms=self.timeout_ms,
        )

    def build_fn(
        self,
        tool_fns_provider: Callable[[], dict[str, Callable]],
        executor: Any | None = None,
        tool_defs_provider: Callable[[], list[ToolDefinition]] | None = None,
    ) -> Callable[[dict[str, Any]], Any]:
        async def skill_fn(input: dict[str, Any]) -> dict[str, Any]:
            context: dict[str, Any] = {"input": input, "intermediate": {}}
            raw_fns = tool_fns_provider()

            if executor is not None and tool_defs_provider is not None:
                tool_defs = {td.name: td for td in tool_defs_provider()}
                wrapped_fns: dict[str, Callable] = {}
                for name, fn in raw_fns.items():
                    td = tool_defs.get(name)
                    wrapped_fns[name] = _make_executor_wrapper(executor, name, td, fn) if td else fn
            else:
                wrapped_fns = raw_fns

            for step_fn in self.steps:
                result = step_fn(context, wrapped_fns)
                if asyncio.iscoroutine(result):
                    result = await result
                context["intermediate"].update(result if isinstance(result, dict) else {"result": result})
            return {"result": context["intermediate"]}

        return skill_fn
