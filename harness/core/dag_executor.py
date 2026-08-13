"""DagExecutor — DAG-based parallel step execution (V0.7, L2 replacement for serial tool execution).

Given a DagPlan, executes steps in topological order,
running independent steps in parallel via asyncio.gather().
Each step goes through the full Tool Layer pipeline.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from harness.core.dag_types import ExecState, StepResult
from harness.core.dag_vars import VariableResolutionError, resolve_variables_in_input, truncate_output
from harness.core.logger import agent_logger
from harness.core.planner import PlanGuardrail
from harness.execution.base import ExecutionBackend
from harness.models.events import (
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepSkippedPayload,
    DagStepStartedPayload,
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
)
from harness.models.plan import DagPlan, DagStep
from harness.models.workspace import Workspace
from harness.storage.event_store import EventStore
from harness.tools.executor import ExecutionStatus, ToolExecutor
from harness.tools.registry import ToolRegistry

_log = agent_logger("dag_executor")


def plan_steps_to_payload(plan: DagPlan) -> list[dict[str, Any]]:
    """Serialize plan steps into a JSON-safe structure for PlanCreated/PlanRevised events.

    v2.2 (C, D6): 计划结构落事件 — 每步的 tool/input/depends_on/description/probe
    落事件存储，事后可从事件流重建 DAG 蓝图。
    """
    return [
        {
            "step_id": s.id,
            "tool_name": s.tool,
            "input": s.input,
            "depends_on": s.depends_on,
            "description": s.description or "",
            "probe": s.probe,
        }
        for s in plan.steps
    ]


class PlanSuspendedError(Exception):
    """Raised when plan execution is suspended because steps need confirmation.

    The scheduler catches this, writes RUN_PAUSED, waits for operator,
    then retries each suspended step.
    """

    def __init__(self, confirmations: list[tuple[str, str]]):
        self.confirmations = confirmations  # [(step_id, confirmation_id), ...]
        ids = ", ".join(f"{sid}({cid})" for sid, cid in confirmations)
        super().__init__(f"{len(confirmations)} step(s) need confirmation: {ids}")


PlanSuspended = PlanSuspendedError


class DagExecutor:
    """Executes a DagPlan in topological layers, with parallelism within each layer."""

    def __init__(
        self,
        executor: ToolExecutor,
        store: EventStore,
        registry: ToolRegistry,
        max_parallel: int = 10,
        workspace: Workspace | None = None,
        backend: ExecutionBackend | None = None,
    ):
        self.executor = executor
        self.store = store
        self.registry = registry
        self._semaphore = asyncio.Semaphore(max_parallel)
        self.workspace = workspace
        self.backend = backend
        self._guardrail = PlanGuardrail(registry, store)

    # ── Public API ──────────────────────────────────────────────────────

    async def execute(self, run_id: str, plan: DagPlan) -> dict[str, StepResult]:
        plan_id = f"plan_{run_id}_{uuid4().hex[:8]}"
        errors = self._guardrail.validate(plan)
        if errors:
            await self.store.append_event(
                run_id,
                EventType.PLAN_FAILED,
                PlanFailedPayload(
                    plan_id=plan_id,
                    completed_steps=0,
                    total_layers=0,
                    final_error="; ".join(errors),
                ).model_dump(),
            )
            return {}
        layers = plan.topological_sort()

        _log.info("[plan] PlanCreated %s: %d steps in %d layers", plan_id, len(plan.steps), len(layers))
        await self.store.append_event(
            run_id,
            EventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=plan_id,
                intent=plan.intent,
                steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                layer_count=len(layers),
                steps=plan_steps_to_payload(plan),
            ).model_dump(),
        )

        all_results: dict[str, StepResult] = {}
        total_completed = 0

        for layer_idx, layer in enumerate(layers):
            ok = await self._execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results)
            completed_here = sum(1 for sid in layer if sid in all_results and all_results[sid].step_normal)
            hard_completed = sum(1 for sid in layer if sid in all_results and all_results[sid].is_completed)
            unsuccess_here = sum(1 for sid in layer if sid in all_results and all_results[sid].is_unsuccessful)
            total_completed += completed_here
            _log.info(
                "[semantic] [layer %d/%d] normal=%d hard=%d unsuccessful=%d",
                layer_idx + 1,
                len(layers),
                completed_here,
                hard_completed,
                unsuccess_here,
            )
            if not ok:
                completed_count = sum(1 for r in all_results.values() if r.step_normal)
                failed = [(sid, r.error or "unknown") for sid, r in all_results.items() if r.is_failed]
                if failed:
                    first_error = failed[0][1]
                    await self.store.append_event(
                        run_id,
                        EventType.PLAN_FAILED,
                        PlanFailedPayload(
                            plan_id=plan_id,
                            completed_steps=completed_count,
                            total_layers=len(layers),
                            final_error=first_error,
                        ).model_dump(),
                    )
                return all_results

        _log.info(
            "[plan] PlanCompleted %s: %d/%d steps across %d layers",
            plan_id,
            total_completed,
            len(plan.steps),
            len(layers),
        )
        await self.store.append_event(
            run_id,
            EventType.PLAN_COMPLETED,
            PlanCompletedPayload(
                plan_id=plan_id,
                completed_steps=total_completed,
                total_layers=len(layers),
                summary=f"Completed {total_completed}/{len(plan.steps)} steps across {len(layers)} layers",
            ).model_dump(),
        )

        return all_results

    async def execute_layer(
        self,
        run_id: str,
        plan: DagPlan,
        plan_id: str,
        layer: list[str],
        layer_idx: int,
        layers: list[list[str]],
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
        self,
        run_id: str,
        plan: DagPlan,
        step_id: str,
        all_results: dict[str, StepResult],
    ) -> StepResult:
        """Re-execute a single DAG step after confirmation resume.

        Delegates to _execute_step with is_retry=True to avoid writing
        DAG_STEP_STARTED (already written during the original layer execution).
        """
        return await self._execute_step(run_id, plan, all_results, step_id, is_retry=True)

    # ── Private helpers ─────────────────────────────────────────────────

    async def _execute_layer(
        self,
        run_id: str,
        plan: DagPlan,
        plan_id: str,
        layer: list[str],
        layer_idx: int,
        layers: list[list[str]],
        all_results: dict[str, StepResult],
    ) -> bool:
        _log.info("[layer %d/%d] %d step(s): %s", layer_idx + 1, len(layers), len(layer), ", ".join(layer))

        step_map = {s.id: s for s in plan.steps}

        # Phase 1 (v2.2, D7/D9): 预判下游门控。拓扑序保证依赖已在上层/上轮，
        # gate 可在此完全确定。被跳过的步骤不写 DAG_STEP_STARTED（工具未执行），
        # 直接写 DAG_STEP_SKIPPED 记录，不进入 Phase 2 执行。
        to_skip: dict[str, str] = {}
        for sid in layer:
            step = step_map.get(sid)
            if not step:
                continue
            reason = self._gate_skip_reason(step, all_results)
            if reason is not None:
                to_skip[sid] = reason
                _log.warning("[gate] [layer %d/%d] %s SKIPPED — %s", layer_idx + 1, len(layers), sid, reason)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_SKIPPED,
                    DagStepSkippedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        reason=reason,
                        tool_name=step.tool,
                    ).model_dump(),
                )
                all_results[sid] = StepResult(step_id=sid, exec_state=ExecState.SKIPPED, error=reason, probe=step.probe)

        # Phase 1.5: Write DAG_STEP_STARTED events serially (seq-guaranteed),
        # only for steps that will actually execute.
        for sid in layer:
            if sid in to_skip:
                continue
            step = step_map.get(sid)
            if step:
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_STARTED,
                    DagStepStartedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        tool_name=step.tool,
                        depends_on=step.depends_on,
                    ).model_dump(),
                )

        # Phase 2: Execute non-skipped steps concurrently (pure execution, no event writing)
        tasks = [self._execute_step(run_id, plan, all_results, sid) for sid in layer if sid not in to_skip]
        exec_layer = [sid for sid in layer if sid not in to_skip]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Phase 3: Write DAG_STEP_COMPLETED/FAILED events serially (seq-guaranteed)
        #
        # Policy: if any step in the layer needs human confirmation, raise
        # PlanSuspended regardless of failures.  Failed steps already have
        # DAG_STEP_FAILED written; the scheduler's layer_failures check will
        # detect them after resume and trigger revise/recovery.
        any_failed = False
        pending_confirmations: list[tuple[str, str]] = []
        for sid, raw in zip(exec_layer, results):
            if isinstance(raw, Exception):
                error = f"DagExecutor layer {layer_idx}: {raw}"
                _log.error("[fail] step=%s error=%s", sid, error)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        error=error,
                        tool_name=step_map.get(sid, DagStep(id=sid)).tool,
                    ).model_dump(),
                )
                all_results[sid] = StepResult(step_id=sid, exec_state=ExecState.FAILED, error=error)
                any_failed = True
                continue

            if raw.is_completed:
                all_results[sid] = raw
                _log.info("[step] %s completed — %.200s%s", sid, raw.summary, "..." if len(raw.summary) > 200 else "")
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        output_summary=raw.summary,
                        status="completed",
                        tool_call_id=raw.tool_call_id,
                    ).model_dump(),
                )
            elif raw.exec_state == ExecState.IDEMPOTENT:
                all_results[sid] = raw
                _log.info("[step] %s idempotent (cached) — %s", sid, raw.summary)
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        output_summary=raw.summary,
                        status="idempotent",
                        tool_call_id=raw.tool_call_id,
                    ).model_dump(),
                )
            elif raw.is_unsuccessful:
                all_results[sid] = raw
                _log.warning(
                    "[semantic] [layer %d/%d] %s UNSUCCESSFUL — error=%s summary=%s",
                    layer_idx + 1,
                    len(layers),
                    sid,
                    raw.error or "?",
                    raw.summary[:80] if raw.summary else "?",
                )
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_COMPLETED,
                    DagStepCompletedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        output_summary=raw.summary,
                        status="unsuccessful",
                        error=raw.error,
                        tool_call_id=raw.tool_call_id,
                    ).model_dump(),
                )
            elif raw.needs_confirmation:
                all_results[sid] = raw
                cid = raw.confirmation_id
                pending_confirmations.append((sid, cid))
                _log.info("[step] %s needs confirmation (id=%s)", sid, cid)
            elif raw.exec_state == ExecState.SKIPPED:
                all_results[sid] = raw
                _log.warning(
                    "[gate] [layer %d/%d] %s SKIPPED — %s",
                    layer_idx + 1,
                    len(layers),
                    sid,
                    raw.error or "dep_not_normal",
                )
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_SKIPPED,
                    DagStepSkippedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        reason=raw.error or "dep_not_normal",
                        tool_name=step_map.get(sid, DagStep(id=sid)).tool,
                    ).model_dump(),
                )
            else:
                err = raw.error or "unknown"
                _log.error("[fail] step=%s error=%s", sid, err)
                all_results[sid] = raw
                any_failed = True
                await self.store.append_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id,
                        step_id=sid,
                        error=err,
                        retryable=raw.retryable,
                        tool_name=step_map.get(sid, DagStep(id=sid)).tool,
                        tool_call_id=raw.tool_call_id,
                    ).model_dump(),
                )

        if pending_confirmations:
            raise PlanSuspended(confirmations=pending_confirmations)

        if any_failed:
            _log.warning(
                "[layer %d/%d] %d/%d step(s) failed",
                layer_idx + 1,
                len(layers),
                sum(1 for r in results if isinstance(r, StepResult) and not r.step_normal),
                len(layer),
            )
            return False

        _log.info("[layer %d/%d] all %d step(s) completed", layer_idx + 1, len(layers), len(layer))
        return True

    @staticmethod
    def _gate_skip_reason(step: DagStep, all_results: dict[str, StepResult]) -> str | None:
        """v2.2 (D7/D9, P0-03): 下游门控预判。

        Returns the skip reason if this step must be SKIPPED, else None.
        若依赖步骤非 normal（UNSUCCESSFUL 非 probe / FAILED / SKIPPED）→ SKIP。
        门控条件**唯一** = step_normal；不读 task_state（约束 4）；不猜下游能否消费
        probe 否定答案（D7）。
        """
        for dep_id in step.depends_on:
            dep_result = all_results.get(dep_id)
            if isinstance(dep_result, StepResult) and not dep_result.step_normal:
                return f"dep '{dep_id}' not normal (exec_state={dep_result.exec_state.value})"
        return None

    async def _execute_step(
        self,
        run_id: str,
        plan: DagPlan,
        all_results: dict[str, StepResult],
        step_id: str,
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
            return StepResult(step_id=step_id, exec_state=ExecState.FAILED, error=f"Step '{step_id}' not found in plan")

        # --- Downstream gate (v2.2 D7/D9, P0-03 fix) ---------------------
        # If any dependency step is NOT normal (UNSUCCESSFUL non-probe / FAILED /
        # SKIPPED), this step is SKIPPED — do not carry bad data downstream.
        # Gate condition is UNIQUELY step_normal; no task_state read (constraint 4).
        for dep_id in step.depends_on:
            dep_result = all_results.get(dep_id)
            if isinstance(dep_result, StepResult) and not dep_result.step_normal:
                _log.warning(
                    "[gate] step=%s SKIPPED — dep=%s not normal (exec_state=%s)",
                    step_id,
                    dep_id,
                    dep_result.exec_state.value,
                )
                return StepResult(
                    step_id=step_id,
                    exec_state=ExecState.SKIPPED,
                    error=f"dep '{dep_id}' not normal (exec_state={dep_result.exec_state.value})",
                    probe=step.probe,
                )

        # --- Build upstream context from ALL step_normal results ---
        # v2.2 (D8): upstream injection uses step_normal — UNSUCCESSFUL (non-probe)
        # results are not injected (their downstream is gated above anyway).
        upstream: dict[str, Any] = {}
        for sid, result in all_results.items():
            if isinstance(result, StepResult) and result.step_normal:
                upstream[sid] = result.output

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
            return StepResult(step_id=step_id, exec_state=ExecState.FAILED, error=str(e))

        # --- Tool lookup ---
        step_def = self.registry.get_tool_def(step.tool)
        step_fn = self.registry.get_tool_fn(step.tool)

        if step_def is None or step_fn is None:
            return StepResult(step_id=step_id, exec_state=ExecState.FAILED, error=f"Tool '{step.tool}' not registered")

        prefix = "[retry]" if is_retry else "[step]"
        _log.info("%s %s → %s with %d param(s)", prefix, step_id, step.tool, len(merged_input))

        # --- Execute via Tool Layer (semaphore-gated) ---
        async with self._semaphore:
            result = await self.executor.execute(
                run_id,
                step.tool,
                merged_input,
                step_def,
                step_fn,
                step_id=step_id,
                workspace_scope=self.workspace.scope if self.workspace else None,
                backend=self.backend,
                workspace_id=self.workspace.workspace_id if self.workspace else None,
            )

        # --- Status dispatch ---
        tc_id = getattr(result, "tool_call_id", None)
        if result.status == ExecutionStatus.COMPLETED:
            summary = truncate_output(result.output)
            if getattr(result, "has_semantic_error", False):
                _log.warning(
                    "[semantic] %s %s → %s completed UNSUCCESSFUL error=%s (%.0fms) call_id=%s",
                    prefix,
                    step_id,
                    step.tool,
                    result.error or "?",
                    getattr(result, "duration_ms", 0),
                    tc_id or "?",
                )
                return StepResult(
                    step_id=step_id,
                    exec_state=ExecState.UNSUCCESSFUL,
                    output=result.output,
                    summary=summary,
                    error=result.error,
                    probe=step.probe,
                    tool_call_id=tc_id,
                )
            _log.info(
                "[semantic] %s %s → %s completed SUCCESS (%.0fms) call_id=%s",
                prefix,
                step_id,
                step.tool,
                getattr(result, "duration_ms", 0),
                tc_id or "?",
            )
            return StepResult(
                step_id=step_id,
                exec_state=ExecState.COMPLETED,
                output=result.output,
                summary=summary,
                probe=step.probe,
                tool_call_id=tc_id,
            )

        if result.status == ExecutionStatus.IDEMPOTENCY_HIT:
            summary = truncate_output(result.output)
            if getattr(result, "has_semantic_error", False):
                _log.warning(
                    "[semantic] %s %s → %s idempotent UNSUCCESSFUL error=%s (%.0fms) call_id=%s",
                    prefix,
                    step_id,
                    step.tool,
                    result.error or "?",
                    getattr(result, "duration_ms", 0),
                    tc_id or "?",
                )
                return StepResult(
                    step_id=step_id,
                    exec_state=ExecState.UNSUCCESSFUL,
                    output=result.output,
                    summary=summary,
                    error=result.error,
                    probe=step.probe,
                    tool_call_id=tc_id,
                )
            _log.info(
                "[semantic] %s %s → %s idempotent (cached) (%.0fms) call_id=%s",
                prefix,
                step_id,
                step.tool,
                getattr(result, "duration_ms", 0),
                tc_id or "?",
            )
            return StepResult(
                step_id=step_id,
                exec_state=ExecState.IDEMPOTENT,
                output=result.output,
                summary=summary,
                probe=step.probe,
                tool_call_id=tc_id,
            )

        if result.status == ExecutionStatus.CONFIRMATION_NEEDED:
            _log.info("%s %s → %s needs confirmation (id=%s)", prefix, step_id, step.tool, result.confirmation_id)
            return StepResult(
                step_id=step_id, exec_state=ExecState.PENDING, confirmation_id=result.confirmation_id, probe=step.probe
            )

        error = result.error or f"Step failed with status {result.status.value}"
        _log.warning("%s %s → %s failed: %s", prefix, step_id, step.tool, error[:200])
        return StepResult(
            step_id=step_id,
            exec_state=ExecState.FAILED,
            error=error,
            retryable=getattr(result, "retryable", False),
            tool_call_id=tc_id,
        )

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
        lines.append(
            f"Total steps: {len(plan.steps)} | Executed: {sum(1 for r in results.values() if r.should_not_rerun)}"
        )
        lines.append("")

        input_trunc = 200
        for step in plan.steps:
            r = results.get(step.id)
            status_tag = ""
            detail = ""
            exec_tag = ""
            rerun_tag = ""
            input_str = json.dumps(step.input, ensure_ascii=False)[:input_trunc]
            task_desc = (step.description or "").strip()[:80]

            if r is None:
                status_tag = "[pending]"
                detail = f"Input: {input_str}"
                exec_tag = "exec=pending"
                task_tag = ""
            else:
                task_tag = f"task={r.task_state.value}"
                if r.is_completed:
                    status_tag = "[done]"
                    detail = f"Summary: {r.summary[:80]}" if r.summary else "OK"
                    if isinstance(r.output, dict) and r.output:
                        output_keys = list(r.output.keys())
                        detail += f" → outputs: {output_keys}"
                    exec_tag = f"exec={r.exec_state.value}"
                    rerun_tag = "replan=NO" if r.should_not_rerun else "replan=MAYBE"
                elif r.is_unsuccessful:
                    # v2.2: normal if probe declared ("没有"就是正确答案), else not normal.
                    status_tag = "[probe-normal]" if r.step_normal else "[unsuccessful]"
                    detail = (
                        f"Summary: {r.summary[:80]} | Error: {r.error or '?'}"
                        if r.summary
                        else f"Error: {r.error or '?'}"
                    )
                    exec_tag = f"exec={r.exec_state.value}"
                    rerun_tag = "replan=NO" if r.should_not_rerun else "replan=MAYBE"
                elif r.exec_state == ExecState.SKIPPED:
                    status_tag = "[skipped]"
                    detail = f"Reason: {r.error or 'dep_not_normal'}"
                    exec_tag = "exec=skipped"
                elif r.needs_confirmation:
                    status_tag = "[confirming]"
                    detail = f"Input: {input_str} | Awaiting confirmation"
                    exec_tag = "exec=pending"
                elif r.exec_state == ExecState.IDEMPOTENT:
                    status_tag = "[cached]"
                    detail = f"Summary: {r.summary[:80]}" if r.summary else "OK (from cache)"
                    if isinstance(r.output, dict) and r.output:
                        output_keys = list(r.output.keys())
                        detail += f" → outputs: {output_keys}"
                    exec_tag = f"exec={r.exec_state.value}"
                    rerun_tag = "replan=NO" if r.should_not_rerun else "replan=MAYBE"
                else:
                    status_tag = "[failed]"
                    detail = f"Input: {input_str} | Error: {r.error or 'unknown'}"
                    exec_tag = f"exec={r.exec_state.value}"
                    rerun_tag = "replan=NO" if r.should_not_rerun else "replan=MAYBE"

            deps = f" | Depends: {','.join(step.depends_on)}" if step.depends_on else ""
            meta = f" | {exec_tag}" if exec_tag else ""
            if rerun_tag:
                meta += f" | {rerun_tag}"
            if task_tag:
                meta += f" | {task_tag}"
            lines.append(f"  - {step.id}({step.tool}): {status_tag}{deps}{meta}")
            if task_desc:
                lines.append(f"    Task: {task_desc}")
            lines.append(f"    {detail}")

        return "\n".join(lines)
