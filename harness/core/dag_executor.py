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
from uuid import uuid4

from harness.core.dag_types import StepResult, StepStatus
from harness.core.dag_vars import VariableResolutionError, resolve_variables_in_input, truncate_output
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
from harness.models.plan import DagPlan, DagStep
from harness.storage.event_store import EventStore
from harness.tools.executor import ExecutionStatus, ToolExecutor
from harness.tools.registry import ToolRegistry

_log = agent_logger("dag_executor")


class PlanSuspended(Exception):
    """Raised when plan execution is suspended because steps need confirmation.

    The scheduler catches this, writes RUN_PAUSED, waits for operator,
    then retries each suspended step.
    """

    def __init__(self, confirmations: list[tuple[str, str]]):
        self.confirmations = confirmations  # [(step_id, confirmation_id), ...]
        ids = ", ".join(f"{sid}({cid})" for sid, cid in confirmations)
        super().__init__(f"{len(confirmations)} step(s) need confirmation: {ids}")


class DagExecutor:
    """Executes a DagPlan in topological layers, with parallelism within each layer."""

    def __init__(self, executor: ToolExecutor, store: EventStore, registry: ToolRegistry, max_parallel: int = 10):
        self.executor = executor
        self.store = store
        self.registry = registry
        self._semaphore = asyncio.Semaphore(max_parallel)

    # ── Public API ──────────────────────────────────────────────────────

    async def execute(self, run_id: str, plan: DagPlan) -> dict[str, StepResult]:
        plan_id = f"plan_{run_id}_{uuid4().hex[:8]}"
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

        all_results: dict[str, StepResult] = {}
        total_completed = 0

        for layer_idx, layer in enumerate(layers):
            ok = await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)
            completed_here = sum(1 for sid in layer if sid in all_results and all_results[sid].is_done)
            hard_completed = sum(1 for sid in layer if sid in all_results and all_results[sid].is_completed)
            soft_err_here = sum(1 for sid in layer if sid in all_results and all_results[sid].has_soft_error)
            total_completed += completed_here
            _log.info("[semantic] [layer %d/%d] done=%d hard=%d soft=%d",
                      layer_idx + 1, len(layers), completed_here, hard_completed, soft_err_here)
            if not ok:
                completed_count = sum(1 for r in all_results.values() if r.is_done)
                failed = [(sid, r.error or "unknown") for sid, r in all_results.items() if r.is_failed]
                if failed:
                    first_error = failed[0][1]
                    await self.store.append_event(
                        run_id,
                        EventType.PLAN_FAILED,
                        PlanFailedPayload(
                            plan_id=plan_id,
                            completed_steps=completed_count,
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
        all_results: dict[str, StepResult],
    ) -> bool:
        """Execute a single DAG layer.

        Returns:
            True if all steps completed.
            False if any step failed (plan terminated).

        Raises:
            PlanSuspended: if a step needs human confirmation.
        """
        return await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)

    async def retry_step(
        self, run_id: str, plan: DagPlan, step_id: str,
        all_results: dict[str, StepResult],
    ) -> StepResult:
        """Re-execute a single DAG step after confirmation resume.

        Delegates to _execute_step with is_retry=True to avoid writing
        DAG_STEP_STARTED (already written during the original layer execution).
        """
        return await self._execute_step(run_id, plan, all_results, step_id, is_retry=True)

    # ── Private helpers ─────────────────────────────────────────────────

    async def _execute_layer(
        self, run_id: str, plan: DagPlan, plan_id: str,
        layer: list[str], layer_idx: int, layers: list[list[str]],
        all_results: dict[str, StepResult],
    ) -> bool:
        _log.info("[layer %d/%d] %d step(s): %s",
                  layer_idx + 1, len(layers), len(layer), ", ".join(layer))

        # Phase 1: Write all DAG_STEP_STARTED events serially (seq-guaranteed)
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

        # Phase 2: Execute all steps concurrently (pure execution, no event writing)
        tasks = [
            self._execute_step(run_id, plan, all_results, sid)
            for sid in layer
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Phase 3: Write DAG_STEP_COMPLETED/FAILED events serially (seq-guaranteed)
        #
        # Policy: if any step in the layer needs human confirmation, raise
        # PlanSuspended regardless of failures.  Failed steps already have
        # DAG_STEP_FAILED written; the scheduler's layer_failures check will
        # detect them after resume and trigger revise/recovery.
        any_failed = False
        pending_confirmations: list[tuple[str, str]] = []
        step_map = {s.id: s for s in plan.steps}
        for sid, raw in zip(layer, results):
            if isinstance(raw, Exception):
                error = f"DagExecutor layer {layer_idx}: {raw}"
                _log.error("[fail] step=%s error=%s", sid, error)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=sid, error=error,
                        tool_name=step_map.get(sid, DagStep(id=sid)).tool,
                    ).model_dump(),
                )
                all_results[sid] = StepResult(step_id=sid, status=StepStatus.EXECUTOR_ERROR, error=error)
                any_failed = True
                continue

            if raw.is_completed:
                all_results[sid] = raw
                _log.info("[step] %s completed — %s", sid, raw.summary)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id, step_id=sid,
                        output_summary=raw.summary,
                        status="completed",
                    ).model_dump(),
                )
            elif raw.has_soft_error:
                all_results[sid] = raw
                _log.warning("[semantic] [layer %d/%d] %s SOFT_ERROR — error=%s summary=%s",
                             layer_idx + 1, len(layers), sid, raw.error or "?", raw.summary[:80] if raw.summary else "?")
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id, step_id=sid,
                        output_summary=raw.summary,
                        status="soft_error",
                        error=raw.error,
                    ).model_dump(),
                )
            elif raw.needs_confirmation:
                all_results[sid] = raw
                cid = raw.confirmation_id
                pending_confirmations.append((sid, cid))
                _log.info("[step] %s needs confirmation (id=%s)", sid, cid)
            else:
                err = raw.error or "unknown"
                _log.error("[fail] step=%s error=%s", sid, err)
                all_results[sid] = raw
                any_failed = True
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=sid, error=err,
                        retryable=raw.retryable,
                        tool_name=step_map.get(sid, DagStep(id=sid)).tool,
                    ).model_dump(),
                )

        if pending_confirmations:
            raise PlanSuspended(confirmations=pending_confirmations)

        if any_failed:
            _log.warning("[layer %d/%d] %d/%d step(s) failed",
                         layer_idx + 1, len(layers),
                         sum(1 for r in results if isinstance(r, StepResult) and not r.is_completed),
                         len(layer))
            return False

        _log.info("[layer %d/%d] all %d step(s) completed", layer_idx + 1, len(layers), len(layer))
        return True

    async def _execute_step(
        self, run_id: str, plan: DagPlan,
        all_results: dict[str, StepResult], step_id: str,
        *,
        is_retry: bool = False,
    ) -> StepResult:
        """Execute a single DAG step without writing events.

        Event writing is handled by _execute_layer for initial execution.
        For retries (is_retry=True), the caller (retry_step / scheduler)
        handles event writing separately.

        Returns a typed StepResult.
        """
        step = next((s for s in plan.steps if s.id == step_id), None)
        if step is None:
            return StepResult(step_id=step_id, status=StepStatus.FAILED,
                              error=f"Step '{step_id}' not found in plan")

        # --- Build upstream context from ALL completed results ---
        upstream: dict[str, Any] = {}
        for sid, result in all_results.items():
            if isinstance(result, StepResult) and result.is_done:
                upstream[sid] = result.output
            elif isinstance(result, dict) and result.get("status") in (StepStatus.COMPLETED.value, StepStatus.SOFT_ERROR.value):
                upstream[sid] = result.get("output")

        # Legacy key mapping: older plans/tools may reference $step_id_result
        for dep_id in step.depends_on:
            legacy_key = f"{dep_id}_result"
            if legacy_key not in upstream and dep_id in upstream:
                upstream[legacy_key] = upstream[dep_id]

        # --- Resolve variables in step input ---
        try:
            merged_input = resolve_variables_in_input(step.input, upstream)
        except VariableResolutionError as e:
            _log.error("[var] %s — step %s", e, step_id)
            return StepResult(step_id=step_id, status=StepStatus.FAILED,
                              error=str(e))

        # --- Tool lookup ---
        step_def = self.registry.get_tool_def(step.tool)
        step_fn = self.registry.get_tool_fn(step.tool)

        if step_def is None or step_fn is None:
            return StepResult(step_id=step_id, status=StepStatus.FAILED,
                              error=f"Tool '{step.tool}' not registered")

        prefix = "[retry]" if is_retry else "[step]"
        _log.info("%s %s → %s with %d param(s)", prefix, step_id, step.tool, len(merged_input))

        # --- Execute via Tool Layer (semaphore-gated) ---
        async with self._semaphore:
            result = await self.executor.execute(
                run_id, step.tool, merged_input, step_def, step_fn,
            )

        # --- Status dispatch ---
        if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
            summary = truncate_output(result.output)
            if getattr(result, "has_semantic_error", False):
                _log.warning("[semantic] %s %s → %s completed SOFT_ERROR error=%s (%.0fms) call_id=%s",
                             prefix, step_id, step.tool, result.error or "?",
                             getattr(result, "duration_ms", 0),
                             getattr(result, "tool_call_id", "?"))
                return StepResult(step_id=step_id, status=StepStatus.SOFT_ERROR,
                                  output=result.output, summary=summary, error=result.error)
            _log.info("[semantic] %s %s → %s completed SUCCESS (%.0fms) call_id=%s",
                      prefix, step_id, step.tool,
                      getattr(result, "duration_ms", 0),
                      getattr(result, "tool_call_id", "?"))
            return StepResult(step_id=step_id, status=StepStatus.COMPLETED,
                              output=result.output, summary=summary)

        if result.status == ExecutionStatus.CONFIRMATION_NEEDED:
            _log.info("%s %s → %s needs confirmation (id=%s)", prefix, step_id, step.tool, result.confirmation_id)
            return StepResult(step_id=step_id, status=StepStatus.CONFIRMATION_NEEDED,
                              confirmation_id=result.confirmation_id)

        error = result.error or f"Step failed with status {result.status.value}"
        _log.warning("%s %s → %s failed: %s", prefix, step_id, step.tool, error[:200])
        return StepResult(step_id=step_id, status=StepStatus.FAILED, error=error,
                          retryable=getattr(result, "retryable", False))

    @staticmethod
    def build_dag_status_text(plan: DagPlan, results: dict[str, StepResult], current_layer: int) -> str:
        """Build a structured status summary for Planner.revise() injection.

        Marked with 【系统状态 - 不可折叠】 so Context Manager skips compression.
        Includes original step input for every step so the LLM can regenerate
        correct parameters on revise.
        """
        lines = ["【系统状态 - 不可折叠】"]
        lines.append(f"Plan: {plan.intent[:60]}")
        layers: list[list[str]] = []
        try:
            layers = plan.topological_sort()
        except ValueError:
            layers = []
        lines.append(f"Total layers: {len(layers)} | Current layer: {current_layer + 1}/{len(layers)}")
        lines.append(f"Total steps: {len(plan.steps)} | Completed: {sum(1 for r in results.values() if r.is_done)}")
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
            elif r.is_completed:
                status_tag = "[done]"
                detail = f"Summary: {r.summary[:80]}" if r.summary else "OK"
            elif r.has_soft_error:
                status_tag = "[soft-error]"
                detail = f"Summary: {r.summary[:80]} | Error: {r.error or '?'}" if r.summary else f"Error: {r.error or '?'}"
            elif r.status == StepStatus.CONFIRMATION_NEEDED:
                status_tag = "[confirming]"
                detail = f"Input: {input_str} | Awaiting confirmation"
            else:
                status_tag = "[failed]"
                detail = f"Input: {input_str} | Error: {r.error or 'unknown'}"

            deps = f" | Depends: {','.join(step.depends_on)}" if step.depends_on else ""
            lines.append(f"  - {step.id}({step.tool}): {status_tag}{deps}")
            lines.append(f"    {detail}")

        return "\n".join(lines)
