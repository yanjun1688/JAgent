"""Semantic evaluator — determines whether a tool's output indicates success or unsuccessful.

Decoupled from the executor: executor merely calls ``evaluate()`` and acts on the
result.  All evaluation logic lives here, making it independently testable and
replaceable without touching executor internals.
"""

from typing import Any

from harness.models.events import ToolResultType
from harness.models.tools import SuccessIndicator, ToolDefinition


class SemanticEvaluator:
    @staticmethod
    def evaluate(output: Any, tool_def: ToolDefinition) -> tuple[ToolResultType, str | None]:
        """Return (result_type, error_msg) for the given output.

        Parameters:
            output: Raw tool output (as returned by the sandbox).
            tool_def: Tool definition whose ``success_indicator`` drives evaluation.

        Returns:
            ``(ToolResultType.SUCCESS, None)`` when the output is semantically
            correct.  ``(ToolResultType.UNSUCCESSFUL, error_msg)`` when the
            indicator detects a semantic failure.
        """
        indicator: SuccessIndicator | None = tool_def.success_indicator
        if indicator is None or not isinstance(output, dict):
            return ToolResultType.SUCCESS, None

        actual = output.get(indicator.field)
        if actual is None:
            return ToolResultType.SUCCESS, None

        ok: bool
        match indicator.op:
            case "eq":
                ok = actual == indicator.value
            case "ne":
                ok = actual != indicator.value
            case "lt":
                ok = actual < indicator.value
            case "lte":
                ok = actual <= indicator.value
            case "gt":
                ok = actual > indicator.value
            case "gte":
                ok = actual >= indicator.value
            case "in":
                ok = actual in indicator.value
            case _:
                ok = True

        if ok:
            return ToolResultType.SUCCESS, None

        error = (
            output.get("error")
            or output.get("message")
            or f"{indicator.field}={actual} (op={indicator.op}, value={indicator.value})"
        )
        return ToolResultType.UNSUCCESSFUL, str(error)
