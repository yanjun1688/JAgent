"""Guardrail framework — pre-execution safety checks for tool calls."""

from dataclasses import dataclass

import jsonschema

from harness.models.tools import ToolDefinition


@dataclass
class GuardrailResult:
    passed: bool
    guardrail_id: str
    reason: str


class SchemaGuardrail:
    GUARDRAIL_ID = "schema"

    @staticmethod
    def check(tool_def: ToolDefinition, input: dict) -> GuardrailResult:
        schema = tool_def.input_schema or {}
        try:
            jsonschema.validate(instance=input, schema=schema)
            return GuardrailResult(passed=True, guardrail_id=SchemaGuardrail.GUARDRAIL_ID, reason="")
        except jsonschema.ValidationError as exc:
            return GuardrailResult(
                passed=False,
                guardrail_id=SchemaGuardrail.GUARDRAIL_ID,
                reason=exc.message,
            )


class GuardrailRunner:
    def __init__(self, custom_guardrails: dict[str, type] | None = None):
        self._registry: dict[str, type] = custom_guardrails.copy() if custom_guardrails else {}

    def register(self, guardrail_type: str, guardrail_cls: type):
        self._registry[guardrail_type] = guardrail_cls

    def run(self, tool_def: ToolDefinition, input: dict) -> list[GuardrailResult]:
        results: list[GuardrailResult] = []

        schema_result = SchemaGuardrail.check(tool_def, input)
        results.append(schema_result)
        if not schema_result.passed:
            return results

        if tool_def.guardrails:
            for gr in tool_def.guardrails:
                guardrail_cls = self._registry.get(gr.guardrail_type)
                if guardrail_cls is None:
                    results.append(
                        GuardrailResult(
                            passed=False,
                            guardrail_id=gr.guardrail_type,
                            reason=f"Unknown guardrail type: {gr.guardrail_type}",
                        )
                    )
                    return results

                instance = guardrail_cls()
                result = instance.check(tool_def, input, gr.config)
                results.append(result)
                if not result.passed:
                    return results

        return results
