from __future__ import annotations

from typing import Any, Callable

from harness.core.system_prompt import build_tool_schemas
from harness.models.tools import ToolDefinition


class ToolRegistry:
    """Central registry for tool definitions and implementations.

    Supports dynamic registration — tools can be added at runtime.
    Provides unified access for Scheduler, AgentKernel, and LLM schema generation.

    Usage:
        registry = ToolRegistry()
        registry.register(tool_def, tool_fn)

        tool_defs = registry.list_tool_defs()
        tool_fns = registry.list_tool_fns()
        schemas = registry.build_llm_schemas()
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._fns: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(
        self,
        tool_def: ToolDefinition,
        fn: Callable[[dict[str, Any]], Any],
    ) -> str:
        if tool_def.name in self._tools:
            raise ValueError(f"Tool '{tool_def.name}' is already registered")
        self._tools[tool_def.name] = tool_def
        self._fns[tool_def.name] = fn
        return tool_def.name

    def get_tool_def(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def get_tool_fn(self, name: str) -> Callable[[dict[str, Any]], Any] | None:
        return self._fns.get(name)

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)
        self._fns.pop(name, None)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_tool_defs(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def list_tool_fns(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return dict(self._fns)

    def build_llm_schemas(self) -> list[dict[str, Any]]:
        return build_tool_schemas(self.list_tool_defs())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
