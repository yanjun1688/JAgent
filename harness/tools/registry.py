from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from harness.core.system_prompt import build_tool_schemas
from harness.models.tools import ToolDefinition

if TYPE_CHECKING:
    from harness.tools.base import BaseTool


def unknown_tool_message(name: str) -> str:
    """Single source of the canonical "tool does not exist" fragment (R7).

    Every trusted seam that rejects an unregistered tool must derive its core
    message from this function (call sites may add their own prefix for
    triage clustering); none may hand-write ``unknown tool '{name}'`` again.
    """
    return f"unknown tool '{name}'"


class UnknownToolError(LookupError):
    """Raised by fail-fast trusted paths when a named tool is not registered.

    Collect-all paths (e.g. PlanGuardrail, which accumulates every bad step
    into one error list for a single LLM retry) must use
    ``ToolRegistry.tool_def_or_error`` instead, so validation does not
    degenerate into stopping at the first unknown tool.
    """

    def __init__(self, name: str) -> None:
        super().__init__(unknown_tool_message(name))
        self.name = name


class ToolRegistry:
    """Central registry for tool definitions and implementations.

    Supports dynamic registration — tools can be added at runtime.
    Provides unified access for Scheduler, AgentKernel, and LLM schema generation.

    Usage:
        registry = ToolRegistry()
        registry.register_tool(FileOpTool())          # canonical entry (D-07)

        tool_defs = registry.list_tool_defs()
        tool_fns = registry.list_tool_fns()
        schemas = registry.build_llm_schemas()
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._fns: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register_tool(self, tool: "BaseTool") -> str:
        """Register a ``BaseTool`` (ADR-010 D-03/D-07) — unique public entry.

        Fail-closed: the tool definition is validated against the trusted
        safety rules (e.g. a DELETE side effect must carry the destructive
        guardrail, scope targets must carry the scope guardrail) before it is
        stored. A violation raises ValueError so a tool missing its guardrail
        is a startup error rather than a silent runtime allow.
        """
        td = tool.to_definition()
        from harness.tools.base import make_invoker
        from harness.tools.guardrails import validate_registration_safety

        violations = validate_registration_safety(td)
        if violations:
            raise ValueError(
                f"Refusing to register tool '{td.name}' due to safety contract violations:\n  - "
                + "\n  - ".join(violations)
            )

        self._register(td, make_invoker(tool))
        return td.name

    def _register(
        self,
        tool_def: ToolDefinition,
        fn: Callable[[dict[str, Any]], Any],
    ) -> str:
        """Private storage primitive — not a public registration API (ADR-010)."""
        if tool_def.name in self._tools:
            raise ValueError(f"Tool '{tool_def.name}' is already registered")
        self._tools[tool_def.name] = tool_def
        self._fns[tool_def.name] = fn
        return tool_def.name

    def get_tool_def(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def get_tool_fn(self, name: str) -> Callable[[dict[str, Any]], Any] | None:
        return self._fns.get(name)

    def tool_def_or_error(self, name: str) -> tuple[ToolDefinition | None, str | None]:
        """Non-raising lookup primitive (R7).

        Returns ``(tool_def, None)`` when registered, otherwise
        ``(None, message)`` with the canonical fragment from
        ``unknown_tool_message``. This is the shared bottom layer for both
        control-flow styles: collect-all validators append ``message`` to an
        error list, while fail-fast paths wrap it via ``require_tool_def``.
        """
        tool_def = self.get_tool_def(name)
        if tool_def is None:
            return None, unknown_tool_message(name)
        return tool_def, None

    def require_tool_def(self, name: str) -> ToolDefinition:
        """Fail-fast wrapper over :meth:`tool_def_or_error` (R7).

        Raises :class:`UnknownToolError` when the tool is not registered.
        """
        tool_def, _ = self.tool_def_or_error(name)
        if tool_def is None:
            raise UnknownToolError(name)
        return tool_def

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
