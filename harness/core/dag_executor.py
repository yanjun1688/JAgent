"""DagExecutor — DAG-based parallel step execution (V0.7, L2 replacement for serial tool execution).

Given a DagPlan, executes steps in topological order,
running independent steps in parallel via asyncio.gather().
Each step goes through the full Tool Layer pipeline.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from harness.core.logger import agent_logger
from harness.models.events import (
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepStartedPayload,
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
)
from harness.models.plan import DagPlan
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

_log = agent_logger("dag_executor")

_OUTPUT_SUMMARY_MAX_CHARS = 200


class DagExecutor:
    """Executes a DagPlan in topological layers, with parallelism within each layer."""

    def __init__(self, executor: ToolExecutor, store: EventStore, registry: ToolRegistry, max_parallel: int = 3):
        self.executor = executor
        self.store = store
        self.registry = registry
        self._semaphore = asyncio.Semaphore(max_parallel)

    async def execute(self, run_id: str, plan: DagPlan) -> dict[str, Any]:
        plan_id = f"plan_{run_id}_{int(time.time())}"
        layers = plan.topological_sort()

        _log.info("[plan] PlanCreated %s: %d steps in %d layers",
                  plan_id, len(plan.steps), len(layers))
        await self.store.append_event(
            run_id,
            EventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=plan_id,
                intent=plan.intent,
                steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                layer_count=len(layers),
            ).model_dump(),
        )

        all_results: dict[str, Any] = {}
        total_completed = 0

        for layer_idx, layer in enumerate(layers):
            ok = await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)
            completed_here = sum(1 for sid in layer if all_results.get(sid, {}).get("status") == "completed")
            total_completed += completed_here
            if not ok:
                failed = [(sid, r.get("error", "unknown")) for sid, r in all_results.items() if r.get("status", "pending") != "completed"]
                if failed:
                    first_error = failed[0][1]
                    await self.store.append_event(
                        run_id,
                        EventType.PLAN_FAILED,
                        PlanFailedPayload(
                            plan_id=plan_id,
                            completed_steps=sum(1 for r in all_results.values() if isinstance(r, dict) and r.get("status") == "completed"),
                            total_layers=len(layers), final_error=first_error,
                        ).model_dump(),
                    )
                return all_results

        _log.info("[plan] PlanCompleted %s: %d/%d steps across %d layers",
                  plan_id, total_completed, len(plan.steps), len(layers))
        await self.store.append_event(
            run_id,
            EventType.PLAN_COMPLETED,
            PlanCompletedPayload(
                plan_id=plan_id, completed_steps=total_completed,
                total_layers=len(layers),
                summary=f"Completed {total_completed}/{len(plan.steps)} steps across {len(layers)} layers",
            ).model_dump(),
        )

        return all_results

    async def execute_layer(
        self, run_id: str, plan: DagPlan, plan_id: str,
        layer: list[str], layer_idx: int, layers: list[list[str]],
        all_results: dict[str, Any],
    ) -> bool:
        """Execute a single DAG layer. Returns False if a step failed (plan terminated)."""
        return await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)

    async def _execute_layer(
        self, run_id: str, plan: DagPlan, plan_id: str,
        layer: list[str], layer_idx: int, layers: list[list[str]],
        all_results: dict[str, Any],
    ) -> bool:
        _log.info("[layer %d/%d] %d step(s): %s",
                  layer_idx + 1, len(layers), len(layer), ", ".join(layer))

        # Step 1: Write all DAG_STEP_STARTED events serially (seq-guaranteed)
        for sid in layer:
            step = next((s for s in plan.steps if s.id == sid), None)
            if step:
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_STARTED,
                    DagStepStartedPayload(
                        plan_id=plan_id, step_id=sid,
                        tool_name=step.tool, depends_on=step.depends_on,
                    ).model_dump(),
                )

        # Step 2: Execute all steps concurrently (pure execution, no event writing)
        tasks = [
            self._execute_step_only(run_id, plan, all_results, sid)
            for sid in layer
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 3: Write DAG_STEP_COMPLETED/FAILED events serially (seq-guaranteed)
        any_failed = False
        for sid, raw in zip(layer, raw_results):
            if isinstance(raw, Exception):
                error = f"DagExecutor layer {layer_idx}: {raw}"
                _log.error("[fail] step=%s error=%s", sid, error)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=sid, error=error,
                    ).model_dump(),
                )
                all_results[sid] = {"error": error, "status": "executor_error"}
                any_failed = True
                continue

            if raw.get("status") == "completed":
                all_results[sid] = raw
                _log.info("[step] %s completed — %.60s", sid, str(raw.get("summary", ""))[:60])
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id, step_id=sid,
                        output_summary=raw.get("summary", ""),
                    ).model_dump(),
                )
            else:
                err = raw.get("error", "unknown")
                _log.error("[fail] step=%s error=%s", sid, err)
                all_results[sid] = raw
                any_failed = True
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=sid, error=err,
                        retryable=raw.get("retryable", False),
                    ).model_dump(),
                )

        if any_failed:
            _log.warning("[layer %d/%d] %d/%d step(s) failed",
                         layer_idx + 1, len(layers),
                         sum(1 for r in raw_results if isinstance(r, dict) and r.get("status") != "completed"),
                         len(layer))
            return False

        _log.info("[layer %d/%d] all %d step(s) completed", layer_idx + 1, len(layers), len(layer))
        return True

    async def _execute_step_only(
        self, run_id: str, plan: DagPlan,
        all_results: dict[str, Any], step_id: str,
    ) -> dict[str, Any]:
        """Execute a single DAG step without writing events.
        
        Returns dict with status/output/error/summary.
        Event writing is handled by _execute_layer.
        """
        step = next((s for s in plan.steps if s.id == step_id), None)
        if step is None:
            return {"status": "error", "error": f"Step '{step_id}' not found in plan"}

        merged_input = {**step.input, **plan.upstream_outputs(step_id, all_results)}

        step_def = self.registry.get_tool_def(step.tool)
        step_fn = self.registry.get_tool_fn(step.tool)

        if step_def is None or step_fn is None:
            return {"status": "error", "error": f"Tool '{step.tool}' not registered"}

        _log.info("[step] %s → %s with %d param(s)", step_id, step.tool, len(merged_input))

        async with self._semaphore:
            result = await self.executor.execute(
                run_id, step.tool, merged_input, step_def, step_fn,
            )

        if result.status.value in ("completed", "idempotency_hit"):
            summary = self._truncate_output(result.output, _OUTPUT_SUMMARY_MAX_CHARS)
            _log.info("[step] %s → %s completed (%.0fms)", step_id, step.tool, getattr(result, 'duration_ms', 0))
            return {"status": "completed", "output": result.output, "summary": summary}

        error = result.error or f"Step failed with status {result.status.value}"
        _log.warning("[step] %s → %s failed: %s", step_id, step.tool, error[:200])
        return {"status": "error", "error": error, "retryable": getattr(result, "retryable", False)}

    @staticmethod
    def _truncate_output(output: Any, max_chars: int = _OUTPUT_SUMMARY_MAX_CHARS) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output[:max_chars]
        text = json.dumps(output, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            return text[:max_chars] + "..."
        return text

    @staticmethod
    def build_dag_status_text(plan: DagPlan, results: dict[str, Any], current_layer: int) -> str:
        """Build a structured status summary for Planner.revise() injection.

        Marked with 【系统状态 - 不可折叠】 so Context Manager skips compression.
        Includes original step input for every step so the LLM can regenerate
        correct parameters on revise.
        """
        lines = ["【系统状态 - 不可折叠】"]
        lines.append(f"Plan: {plan.intent[:60]}")
        layers = []
        try:
            layers = plan.topological_sort()
        except ValueError:
            layers = []
        lines.append(f"Total layers: {len(layers)} | Current layer: {current_layer + 1}/{len(layers)}")
        lines.append(f"Total steps: {len(plan.steps)} | Completed: {sum(1 for r in results.values() if r.get('status') == 'completed')}")
        lines.append("")

        input_trunc = 200
        for step in plan.steps:
            r = results.get(step.id)
            status_tag = ""
            detail = ""
            input_str = json.dumps(step.input, ensure_ascii=False)[:input_trunc]

            if r is None:
                status_tag = "[pending]"
                detail = f"Input: {input_str}"
            elif r.get("status") == "completed":
                status_tag = "[done]"
                summary = r.get("summary", "")
                detail = f"Summary: {summary[:80]}" if summary else "OK"
            elif r.get("status") == "skipped":
                status_tag = "[skipped]"
                detail = f"Input: {input_str} | {r.get('reason', 'skipped')}"
            else:
                status_tag = "[failed]"
                detail = f"Input: {input_str} | Error: {r.get('error', 'unknown')}"

            deps = f" | Depends: {','.join(step.depends_on)}" if step.depends_on else ""
            lines.append(f"  - {step.id}({step.tool}): {status_tag}{deps}")
            lines.append(f"    {detail}")

        return "\n".join(lines)
