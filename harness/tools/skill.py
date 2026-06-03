from __future__ import annotations

import asyncio
from typing import Any, Callable

from harness.models.tools import SideEffect, ToolDefinition


class Skill:
    """A multi-step skill package.

    Externally appears as a single ToolDefinition with a single implementation function.
    Internally orchestrates multiple steps, potentially using other registered tools.

    Usage:
        skill = Skill(
            name="research_topic",
            description="Research a topic by searching the web and summarizing findings",
            input_schema={...},
            steps=[step1, step2],
        )
        registry.register(skill.definition, skill.build_fn(registry))
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
    ) -> Callable[[dict[str, Any]], Any]:
        async def skill_fn(input: dict[str, Any]) -> dict[str, Any]:
            context: dict[str, Any] = {"input": input, "intermediate": {}}
            for step_fn in self.steps:
                result = step_fn(context, tool_fns_provider())
                if asyncio.iscoroutine(result):
                    result = await result
                context["intermediate"].update(result if isinstance(result, dict) else {"result": result})
            return {"result": context["intermediate"]}

        return skill_fn
