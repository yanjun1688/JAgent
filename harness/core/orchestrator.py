"""Orchestrator — dynamic multi-step tool orchestration (V0.4+).

Agent submits a sequence of tool calls in a single orchestrate() invocation;
Harness executes each step through the full Tool Layer pipeline
(Guardrails, idempotency, confirmation, sandbox) and aggregates results.
"""

import asyncio
import uuid
from typing import Any

from harness.models.events import (
    Event,
    EventType,
    GuardrailTriggeredPayload,
    OrchestrationCompletedPayload,
    OrchestrationFailedPayload,
    OrchestrationStartedPayload,
    RunPausedPayload,
    StepCompletedPayload,
    StepFailedPayload,
)
from harness.models.tools import DependencyConstraint, RetryPolicy, SideEffect, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ExecutionStatus, ToolExecutor, current_run_id
from harness.tools.guardrails import SchemaGuardrail
from harness.tools.registry import ToolRegistry

# ── orchestrate tool definition ──────────────────────────────────

ORCHESTRATE_DEF = ToolDefinition(
    name="orchestrate",
    description="Execute multiple tool calls in sequence under harness control. "
    "Provide an intent and a list of steps. Each step specifies a tool and its input. "
    "All steps execute atomically — you only see the final aggregated result.",
    input_schema={
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "The goal of this orchestration"},
            "steps": {
                "type": "array",
                "description": "Ordered list of tool steps to execute",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Name of the tool to call"},
                        "input": {"type": "object", "description": "Input arguments for the tool"},
                        "description": {"type": "string", "description": "Human-readable purpose of this step (optional)"},
                    },
                    "required": ["tool", "input"],
                },
            },
        },
        "required": ["intent", "steps"],
    },
    idempotency_key_fields=["intent", "steps"],
    side_effects=[SideEffect.EXTERNAL],
    timeout_ms=600_000,
    retry_policy=RetryPolicy(max_retries=0),
    requires_confirmation=False,
    depends_on=[
        DependencyConstraint(event_type="RunStarted", message="Run must be started before orchestration"),
    ],
)


def make_orchestrate_fn(orchestrator: "Orchestrator"):
    """Create a tool_fn for the `orchestrate` tool bound to an Orchestrator instance.

    Usage:
        orch = Orchestrator(store, executor, registry)
        registry.register(ORCHESTRATE_DEF, make_orchestrate_fn(orch))
    """
    async def orchestrate_fn(input: dict[str, Any]) -> dict[str, Any]:
        run_id = current_run_id.get()
        return await orchestrator.execute(run_id, input)
    return orchestrate_fn


class PlanGuardrail:
    """Built-in guardrail for orchestration plans — validates step count,
    tool availability, and input schemas before execution begins."""

    def __init__(self, registry: ToolRegistry, max_steps: int = 10):
        self.registry = registry
        self.max_steps = max_steps

    def validate(self, steps: list[dict[str, Any]]) -> None:
        if not steps:
            raise ValueError("Plan must contain at least one step")

        if len(steps) > self.max_steps:
            raise ValueError(f"Plan exceeds maximum steps ({len(steps)} > {self.max_steps})")

        for i, step in enumerate(steps):
            tool_name = step.get("tool")
            if not tool_name:
                raise ValueError(f"Step {i} is missing 'tool' field")
            if not isinstance(tool_name, str):
                raise ValueError(f"Step {i} 'tool' must be a string, got {type(tool_name).__name__}")

            tool_def = self.registry.get_tool_def(tool_name)
            if tool_def is None:
                raise ValueError(f"Step {i}: unknown tool '{tool_name}' — not registered in ToolRegistry")

            step_input = step.get("input", {})
            if not isinstance(step_input, dict):
                raise ValueError(f"Step {i} 'input' must be an object, got {type(step_input).__name__}")

            schema_result = SchemaGuardrail.check(tool_def, step_input)
            if not schema_result.passed:
                raise ValueError(f"Step {i} ({tool_name}): input schema validation failed — {schema_result.reason}")


class Orchestrator:
    """Executes multi-step tool plans with full Harness safety.

    Each step runs through ToolExecutor.execute(), inheriting all
    Guardrails, idempotency, and confirmation flows. When a step
    requires operator confirmation, the Orchestrator pauses and
    listens for RunResumed events via EventStore's on_append hook.
    """

    def __init__(
        self,
        store: EventStore,
        executor: ToolExecutor,
        registry: ToolRegistry,
        max_steps: int = 10,
    ):
        self.store = store
        self.executor = executor
        self.registry = registry
        self.max_steps = max_steps
        self._plan_events: dict[str, asyncio.Event] = {}
        self._confirmation_timeout_s: float = 300.0
        self.store.on_append(self._on_event)

    async def _on_event(self, event: Event) -> None:
        if event.event_type == EventType.RUN_RESUMED:
            ev = self._plan_events.get(event.run_id)
            if ev is not None:
                ev.set()

    async def execute(self, run_id: str, plan_input: dict[str, Any]) -> dict[str, Any]:
        plan_id = str(uuid.uuid4())
        intent = plan_input.get("intent", "")
        steps: list[dict[str, Any]] = plan_input.get("steps", [])

        guardrail = PlanGuardrail(self.registry, max_steps=self.max_steps)
        try:
            guardrail.validate(steps)
        except ValueError as e:
            await self.store.append_event(
                run_id,
                EventType.GUARDRAIL_TRIGGERED,
                GuardrailTriggeredPayload(
                    tool_call_id="orchestrate_" + plan_id,
                    tool_name="orchestrate",
                    guardrail_id="PlanGuardrail",
                    reason=str(e),
                ).model_dump(),
            )
            return self._fail_result(plan_id, 0, str(e))

        await self.store.append_event(
            run_id,
            EventType.ORCHESTRATION_STARTED,
            OrchestrationStartedPayload(
                plan_id=plan_id,
                intent=intent,
                steps_summary=f"{len(steps)} steps: {', '.join(s['tool'] for s in steps)}",
            ).model_dump(),
        )

        step_outputs: list[dict[str, Any]] = []
        completed = 0

        for i, step in enumerate(steps):
            tool_name = step["tool"]
            tool_input = step.get("input", {})

            tool_def = self.registry.get_tool_def(tool_name)
            tool_fn = self.registry.get_tool_fn(tool_name)
            if tool_def is None or tool_fn is None:
                return await self._fail_plan(run_id, plan_id, completed, f"Step {i}: tool '{tool_name}' vanished from registry")

            result = await self.executor.execute(
                run_id, tool_name, tool_input, tool_def, tool_fn,
            )

            match result.status:
                case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
                    await self.store.append_event(
                        run_id,
                        EventType.STEP_COMPLETED,
                        StepCompletedPayload(
                            plan_id=plan_id,
                            step_index=i,
                            tool_call_id=result.tool_call_id,
                            output=result.output,
                        ).model_dump(),
                    )
                    completed += 1
                    step_outputs.append({
                        "step_index": i,
                        "tool": tool_name,
                        "status": "completed",
                        "output": result.output,
                    })

                case ExecutionStatus.CONFIRMATION_NEEDED:
                    try:
                        await self._wait_for_step_confirmation(run_id, plan_id)
                    except RuntimeError:
                        error = f"Confirmation timed out for step {i} ({tool_name})"
                        await self.store.append_event(
                            run_id,
                            EventType.STEP_FAILED,
                            StepFailedPayload(
                                plan_id=plan_id,
                                step_index=i,
                                tool_call_id=result.tool_call_id,
                                error=error,
                            ).model_dump(),
                        )
                        return await self._fail_plan(run_id, plan_id, completed, error)
                    result = await self.executor.execute(
                        run_id, tool_name, tool_input, tool_def, tool_fn,
                    )
                    if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
                        await self.store.append_event(
                            run_id,
                            EventType.STEP_COMPLETED,
                            StepCompletedPayload(
                                plan_id=plan_id,
                                step_index=i,
                                tool_call_id=result.tool_call_id,
                                output=result.output,
                            ).model_dump(),
                        )
                        completed += 1
                        step_outputs.append({
                            "step_index": i,
                            "tool": tool_name,
                            "status": "completed",
                            "output": result.output,
                        })
                    else:
                        error = result.error or "Step failed after confirmation"
                        await self.store.append_event(
                            run_id,
                            EventType.STEP_FAILED,
                            StepFailedPayload(
                                plan_id=plan_id,
                                step_index=i,
                                tool_call_id=result.tool_call_id,
                                error=error,
                            ).model_dump(),
                        )
                        return await self._fail_plan(run_id, plan_id, completed, f"Step {i} ({tool_name}): {error}")

                case _:
                    raw = result.error or f"failed with status {result.status.value}"
                    error = f"Step {i} ({tool_name}): {raw}"
                    await self.store.append_event(
                        run_id,
                        EventType.STEP_FAILED,
                        StepFailedPayload(
                            plan_id=plan_id,
                            step_index=i,
                            tool_call_id=result.tool_call_id,
                            error=error,
                        ).model_dump(),
                    )
                    return await self._fail_plan(run_id, plan_id, completed, error)

        await self.store.append_event(
            run_id,
            EventType.ORCHESTRATION_COMPLETED,
            OrchestrationCompletedPayload(
                plan_id=plan_id,
                completed_steps=completed,
                summary=f"All {completed} steps completed successfully",
            ).model_dump(),
        )

        return {
            "status": "completed",
            "plan_id": plan_id,
            "completed_steps": completed,
            "summary": f"All {completed} steps completed successfully",
            "results": step_outputs,
        }

    async def _fail_plan(
        self, run_id: str, plan_id: str, completed: int, final_error: str,
    ) -> dict[str, Any]:
        await self.store.append_event(
            run_id,
            EventType.ORCHESTRATION_FAILED,
            OrchestrationFailedPayload(
                plan_id=plan_id,
                completed_steps=completed,
                final_error=final_error,
            ).model_dump(),
        )
        return self._fail_result(plan_id, completed, final_error)

    @staticmethod
    def _fail_result(plan_id: str, completed: int, final_error: str) -> dict[str, Any]:
        return {
            "status": "failed",
            "plan_id": plan_id,
            "completed_steps": completed,
            "error": final_error,
        }

    async def _wait_for_step_confirmation(self, run_id: str, plan_id: str) -> None:
        # Each confirmation-needing step writes a RunPaused event. Although
        # fold_events always sets state.status=PAUSED on the latest RUN_PAUSED,
        # the frontend will see the plan as paused — which is correct, since the
        # plan cannot proceed until the operator confirms the current step.
        await self.store.append_event(
            run_id,
            EventType.RUN_PAUSED,
            RunPausedPayload(reason="waiting_confirmation").model_dump(),
        )
        event = self._plan_events.setdefault(run_id, asyncio.Event())
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=self._confirmation_timeout_s)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Confirmation timed out after {self._confirmation_timeout_s}s")
        finally:
            event.clear()
