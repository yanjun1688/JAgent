"""PlanningExecutorScheduler — Plan → Execute(parallel) → Revise cycle (L3)."""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from uuid import uuid4
from harness.core.dag_executor import DagExecutor, PlanSuspended
from harness.core.dag_types import StepResult, StepStatus
from harness.core.fold import fold_events, RunState, RunStatus
from harness.core.logger import fmtkv
from harness.core.logger import agent_logger, guard_logger
from harness.core.planner import Planner
from harness.core.scheduler.base import BaseScheduler, SchedulerConfig
from harness.core.scheduler.fallback_kernel import _FallbackKernel
from harness.core.scheduler.loop import AgentLoopScheduler
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import (
    AgentThoughtPayload,
    DagStepCompletedPayload,
    DagStepFailedPayload,
    EpisodeSummary,
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
    PlanRevisedPayload,
    RunCompletedPayload,
    RunPausedPayload,
)
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor

_sched_iter = agent_logger("scheduler.iter")
_sched_think = agent_logger("scheduler.think")
_sched_act = agent_logger("scheduler.act")
_sched_ctrl = agent_logger("scheduler.control")
_sched_breaker = guard_logger("scheduler.breaker")


class PlanningExecutorScheduler(BaseScheduler):
    """V0.7 Scheduler — Plan → Execute(parallel) → Revise cycle.

    Replaces serial think→act→observe with Planner-Executor + DAG.
    Falls back to the old serial path when Planner fails to produce a valid plan.
    """

    def __init__(
        self,
        store: EventStore,
        executor: ToolExecutor,
        planner: Planner,
        dag_executor: DagExecutor,
        tool_defs: list[ToolDefinition],
        tool_fns: dict[str, Callable[[dict[str, Any]], Any]],
        config: SchedulerConfig | None = None,
        context_manager=None,
        monitor=None,
        run_end_cb: Callable[[str], None] | None = None,
    ):
        super().__init__(store, executor, tool_defs, tool_fns, config, context_manager, monitor, run_end_cb)
        self.planner = planner
        self.dag_executor = dag_executor

    async def _run_loop(self, run_id: str, intent: str) -> RunState:
        await self._ensure_run_started(run_id, intent)

        needs_tools = await self._classify_intent(run_id, intent)
        if not needs_tools:
            _sched_ctrl.info("[classify] Intent classified as analysis-only — skipping plan/execute")
            try:
                answer = await self._generate_answer(intent, await self._refresh_state(run_id), None)
            except Exception as exc:
                _sched_think.error("[classify] Answer generation failed: %s", exc)
                await self._complete(run_id, "Task completed")
                return await self._refresh_state(run_id)
            await self.store.append_event(
                run_id, EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought="ANSWER: " + answer,
                    tool_choice=None, token_count=0, tool_calls=None,
                ).model_dump(),
            )
            await self._complete(run_id, answer)
            return await self._refresh_state(run_id)

        state = await self._plan_execute_revise_loop(run_id, intent)
        return state

    async def _classify_intent(self, run_id: str, intent: str) -> bool:
        """Return True if the intent needs external tools, False if analysis-only."""
        truncated = intent[:500] if len(intent) > 500 else intent
        prompt = get_prompt(AgentPhase.CLASSIFY, intent=truncated)
        _sched_ctrl.info("[classify] phase=%s len=%d intent_truncated=%s",
                         AgentPhase.CLASSIFY.value, len(prompt), truncated[:80])
        try:
            response = await self.planner.llm.chat(
                [{"role": "system", "content": prompt}],
                temperature=0.0,
                max_tokens=4,
            )
        except Exception as exc:
            _sched_think.warning("[classify] LLM call failed: %s — assuming needs_tools=True", exc)
            return True
        result = response.strip().lower()
        needs = result != "no"
        _sched_ctrl.info("[classify] intent=%s needs_tools=%s raw=%s", truncated[:80], needs, result[:20])
        return needs

    async def _handle_dag_confirmations(
        self, run_id: str, plan: DagPlan, plan_id: str,
        confirmations: list[tuple[str, str]],
        results: dict[str, StepResult],
        tag: str,
        consecutive_failures: int,
    ) -> tuple[bool, list[str], int]:
        """Handle confirmation retry loop for DAG steps.

        Returns (terminated, failed_step_ids, consecutive_failures).
        Caller must return immediately if terminated=True.
        """
        failed_step_ids: list[str] = []
        step_map = {s.id: s for s in plan.steps}
        for confirm_sid, confirm_cid in confirmations:
            confirm_retries = 0
            while True:
                if self._is_cancelled(run_id):
                    await self._fail(run_id, "Run cancelled by user")
                    return True, failed_step_ids, consecutive_failures
                if confirm_retries >= self.config.max_confirm_retries:
                    _sched_breaker.error("[breaker] Max confirmation retries (%d) exceeded for DAG step %s",
                                         self.config.max_confirm_retries, confirm_sid)
                    await self._fail(run_id, f"Max confirmation retries ({self.config.max_confirm_retries}) exceeded for step {confirm_sid}")
                    return True, failed_step_ids, consecutive_failures
                _sched_ctrl.info("[ctrl] %s confirmation loop: RUN_PAUSED for run=%s step=%s (attempt %d/%d)",
                                 tag, run_id, confirm_sid, confirm_retries + 1, self.config.max_confirm_retries)
                await self.store.append_event(
                    run_id, EventType.RUN_PAUSED,
                    RunPausedPayload(reason="waiting_confirmation").model_dump(),
                )
                await self._wait_for_resume(run_id)
                if self._is_cancelled(run_id):
                    await self._fail(run_id, "Run cancelled by user")
                    return True, failed_step_ids, consecutive_failures
                state = await self._refresh_state(run_id)
                if state.status in (RunStatus.FAILED, RunStatus.COMPLETED):
                    return True, failed_step_ids, consecutive_failures
                retry_raw = await self.dag_executor.retry_step(run_id, plan, confirm_sid, results)
                if retry_raw.is_completed:
                    results[confirm_sid] = retry_raw
                    _sched_act.info("[%s] Step %s completed after confirmation", tag, confirm_sid)
                    await self.store.append_event(
                        run_id, EventType.DAG_STEP_COMPLETED,
                        DagStepCompletedPayload(
                            plan_id=plan_id, step_id=confirm_sid,
                            output_summary=retry_raw.summary,
                        ).model_dump(),
                    )
                    break
                if retry_raw.needs_confirmation:
                    _sched_act.info("[%s] Step %s still needs confirmation — pausing again (attempt %d/%d)",
                                    tag, confirm_sid, confirm_retries + 1, self.config.max_confirm_retries)
                    confirm_retries += 1
                    continue
                _sched_act.error("[%s] Step %s failed after confirmation: %s",
                                 tag, confirm_sid, retry_raw.error or "unknown")
                results[confirm_sid] = retry_raw
                await self.store.append_event(
                    run_id, EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id, step_id=confirm_sid,
                        error=retry_raw.error or "Confirmation result failed",
                        tool_name=step_map.get(confirm_sid, DagStep(id=confirm_sid)).tool,
                    ).model_dump(),
                )
                failed_step_ids.append(confirm_sid)
                break
        return False, failed_step_ids, consecutive_failures

    async def _plan_execute_revise_loop(self, run_id: str, intent: str) -> RunState:
        state = await self._refresh_state(run_id)
        consecutive_failures = 0
        loop_iteration = 0

        _sched_ctrl.info("[lifecycle] Plan-Execute-Revise loop START for run=%s intent=%s", run_id, intent[:120])
        while state.status == RunStatus.RUNNING:
            loop_iteration += 1
            if loop_iteration > self.config.max_iterations:
                _sched_breaker.error("[breaker] Exceeded max iterations (%d)", self.config.max_iterations)
                await self._fail(run_id, f"Exceeded max iterations ({self.config.max_iterations})")
                return await self._refresh_state(run_id)
            if self._is_cancelled(run_id):
                await self._fail(run_id, "Run cancelled by user")
                return await self._refresh_state(run_id)

            if state.status == RunStatus.PAUSED:
                _sched_ctrl.info("[ctrl] Plan loop detected PAUSED for run=%s, pause_reason=%s, pending_confirmations=%d",
                                 run_id, state.pause_reason, len(state.pending_confirmations))
                await self._handle_pause(run_id)
                state = await self._refresh_state(run_id)
                _sched_ctrl.info("[ctrl] Plan loop after _handle_pause for run=%s, folded status=%s",
                                 run_id, state.status.value)
                continue

            feedback_text = self._get_feedback_text(state)
            _sched_think.info("[plan] Planning for intent: %s %s", intent[:120],
                              fmtkv(has_feedback=feedback_text is not None))
            plan = await self._get_or_fallback(run_id, intent, state, feedback_text)
            if plan is None:
                return await self._refresh_state(run_id)

            await self.store.append_event(
                run_id,
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought=self.planner.last_raw_response[:500] or f"Plan: {plan.intent[:200]}",
                    tool_choice="plan",
                    token_count=0,
                    tool_calls=[s.tool for s in plan.steps[:5]],
                ).model_dump(),
            )

            if not plan.steps:
                _sched_think.info("[plan] Empty plan — generating answer")
                try:
                    answer = await self._generate_answer(state.intent or intent, state, feedback_text)
                except Exception as exc:
                    _sched_think.error("[plan] Answer generation failed: %s", exc)
                    await self._complete(run_id, "Task completed")
                    return await self._refresh_state(run_id)
                await self.store.append_event(
                    run_id, EventType.AGENT_THOUGHT,
                    AgentThoughtPayload(
                        thought="ANSWER: " + answer,
                        tool_choice=None,
                        token_count=0,
                        tool_calls=None,
                    ).model_dump(),
                )
                await self._complete(run_id, answer)
                return await self._refresh_state(run_id)

            if plan.dynamic:
                state = await self._execute_dynamic_plan(run_id, intent, plan, state)
                continue

            state, consecutive_failures = await self._execute_static_plan(run_id, plan, consecutive_failures, state_seq=state.seq)
            _sched_ctrl.info("[lifecycle] Static plan complete — status=%s failures=%d", state.status.value, consecutive_failures)
            if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return state

            if consecutive_failures >= self.config.max_consecutive_failures:
                _sched_breaker.error("[breaker] TRIP — consecutive_failures=%d", consecutive_failures)
                await self._fail(run_id, f"Circuit breaker: {consecutive_failures} consecutive failures")
                return await self._refresh_state(run_id)

            state = await self._refresh_state(run_id)

        return state

    async def _execute_dynamic_plan(
        self, run_id: str, intent: str, plan: DagPlan, state: RunState,
    ) -> RunState:
        _sched_ctrl.info("[plan] Dynamic plan — serial step-by-step execution")
        results: dict[str, StepResult] = {}
        dyn_plan_id = f"plan_{run_id}_{uuid4().hex[:8]}"
        layers = plan.topological_sort(
            completed_step_ids=set(),
        )
        state_seq = state.seq
        await self.store.append_event(
            run_id, EventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=dyn_plan_id, intent=plan.intent,
                steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                layer_count=len(layers),
            ).model_dump(),
        )
        for step_idx, step in enumerate(plan.steps):
            if self._is_cancelled(run_id):
                await self._fail(run_id, "Run cancelled by user")
                return await self._refresh_state(run_id)
            if self.context_manager:
                await self.context_manager.maybe_compress(run_id, state.seq, state)
                await self.context_manager.try_checkpoint(run_id, state.seq, state)
            step_layers = DagPlan(intent=intent, steps=[step]).topological_sort()
            try:
                ok = await self.dag_executor.execute_layer(
                    run_id, plan, dyn_plan_id, step_layers[0], step_idx, step_layers, results,
                )
            except PlanSuspended as susp:
                _sched_act.info("[dynamic] %d step(s) need confirmation: %s",
                                len(susp.confirmations),
                                ", ".join(sid for sid, _ in susp.confirmations))
                terminated, failed_step_ids, _ = await self._handle_dag_confirmations(
                    run_id, plan, dyn_plan_id,
                    susp.confirmations, results, "dynamic", 0,
                )
                if terminated:
                    return await self._refresh_state(run_id)
                if failed_step_ids:
                    ok = False
                else:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _sched_act.error("[dynamic] Step %s failed: %s", step.id, exc)
                await self._fail(run_id, f"Dynamic step '{step.id}' failed: {exc}")
                return await self._refresh_state(run_id)
            if not ok:
                _sched_act.error("[dynamic] Step %s had failures — revising", step.id)
                sys_state = self.dag_executor.build_dag_status_text(
                    plan, results, current_layer=step_idx,
                )
                state = await self._refresh_state(run_id)
                # Track the seq at which state was folded for feedback filtering
                step_state_seq = state.seq
                fb = self._get_feedback_text(state, for_revise=True, since_seq=state_seq)
                _sched_act.info("[dynamic] Revise after failure %s", fmtkv(
                    step_id=step.id, has_feedback=fb is not None,
                ))
                revised = await self.planner.revise(plan, results, sys_state, feedback=fb, intent_fallback=state.intent)
                if revised is None:
                    _sched_act.error("[dynamic] Revise failed — terminating")
                    await self._fail(run_id, f"Dynamic step '{step.id}' failed")
                    return await self._refresh_state(run_id)
                await self.store.append_event(
                    run_id, EventType.PLAN_REVISED,
                    PlanRevisedPayload(
                        plan_id=dyn_plan_id, revision_reason="dynamic_step_failure",
                        intent=revised.intent,
                        remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                    ).model_dump(),
                )
                if not revised.steps or revised.dynamic:
                    total_ok = sum(1 for sid in (s.id for s in plan.steps) if isinstance(results.get(sid), StepResult) and results[sid].is_completed)
                    revised_layers = revised.topological_sort() if revised.steps else []
                    await self.store.append_event(
                        run_id, EventType.PLAN_COMPLETED,
                        PlanCompletedPayload(
                            plan_id=dyn_plan_id, completed_steps=total_ok,
                            total_layers=len(revised_layers), summary="Dynamic task completed after revision",
                        ).model_dump(),
                    )
                    await self._finalize_with_summary(run_id, plan.intent, "Dynamic task completed after revision")
                    return await self._refresh_state(run_id)
                plan = revised
                continue
            sys_state = self.dag_executor.build_dag_status_text(
                plan, results, current_layer=step_idx,
            )
            s = await self._refresh_state(run_id)
            step_state_seq = s.seq
            fb = self._get_feedback_text(s, for_revise=True, since_seq=state_seq)
            _sched_act.info("[dynamic] Revise after layer complete %s", fmtkv(
                step_id=step.id, has_feedback=fb is not None,
            ))
            revised = await self.planner.revise(plan, results, sys_state, feedback=fb, intent_fallback=s.intent)
            if revised is None:
                _sched_think.error("[dynamic] Revise failed — terminating")
                await self._fail(run_id, "Planner revise failed during dynamic execution")
                return await self._refresh_state(run_id)
            await self.store.append_event(
                run_id, EventType.PLAN_REVISED,
                PlanRevisedPayload(
                    plan_id=dyn_plan_id, revision_reason="dynamic_step_completed",
                    intent=revised.intent,
                    remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                ).model_dump(),
            )
            if not revised.steps or revised.dynamic:
                total_ok = sum(1 for sid in (s.id for s in plan.steps) if isinstance(results.get(sid), StepResult) and results[sid].is_completed)
                revised_layers = revised.topological_sort() if revised.steps else []
                await self.store.append_event(
                    run_id, EventType.PLAN_COMPLETED,
                    PlanCompletedPayload(
                        plan_id=dyn_plan_id, completed_steps=total_ok,
                        total_layers=len(revised_layers), summary="Dynamic task completed",
                    ).model_dump(),
                )
                await self._finalize_with_summary(run_id, plan.intent, "Dynamic task completed")
                return await self._refresh_state(run_id)
            plan = revised
        return await self._refresh_state(run_id)

    async def _execute_static_plan(
        self, run_id: str, plan: DagPlan, consecutive_failures: int,
        state_seq: int = 0,
    ) -> tuple[RunState, int]:
        results: dict[str, StepResult] = {}
        self_heal_count = 0

        while True:
            if self_heal_count >= self.config.max_consecutive_failures:
                _sched_breaker.error("[breaker] Self-heal loop exceeded max (%d) attempts",
                                     self_heal_count)
                consecutive_failures = self_heal_count
                await self._fail(run_id, f"Self-heal exceeded {self_heal_count} attempts — unable to complete plan")
                return await self._refresh_state(run_id), consecutive_failures
            _sched_ctrl.info("[execute] DAG execution attempt round=%d (plan=%d steps, cached=%d results)",
                             self_heal_count, len(plan.steps),
                             len([r for r in results.values() if isinstance(r, StepResult) and r.is_completed]))
            completed_ids = {
                sid for sid, r in results.items()
                if isinstance(r, StepResult) and r.is_completed
                and sid not in {s.id for s in plan.steps}
            }
            layers = plan.topological_sort(completed_step_ids=completed_ids)
            plan_id = f"plan_{run_id}_{uuid4().hex[:8]}"
            _sched_iter.info("[execute] Executing DAG plan with %d steps in %d layers",
                             len(plan.steps), len(layers))
            _sched_iter.info("[plan] PlanCreated %s: %d steps in %d layers",
                             plan_id, len(plan.steps), len(layers))
            await self.store.append_event(
                run_id, EventType.PLAN_CREATED,
                PlanCreatedPayload(
                    plan_id=plan_id, intent=plan.intent,
                    steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                    layer_count=len(layers),
                ).model_dump(),
            )

            for layer_idx, layer in enumerate(layers):
                if self._is_cancelled(run_id):
                    await self._fail(run_id, "Run cancelled by user")
                    return await self._refresh_state(run_id), consecutive_failures
                if self.context_manager:
                    state = await self._refresh_state(run_id)
                    await self.context_manager.maybe_compress(run_id, state.seq, state)
                    await self.context_manager.try_checkpoint(run_id, state.seq, state)
                try:
                    ok = await self.dag_executor.execute_layer(
                        run_id, plan, plan_id, layer, layer_idx, layers, results,
                    )
                except PlanSuspended as susp:
                    _sched_act.info("[execute] %d step(s) need confirmation: %s",
                                    len(susp.confirmations),
                                    ", ".join(sid for sid, _ in susp.confirmations))
                    terminated, failed_step_ids, consecutive_failures = await self._handle_dag_confirmations(
                        run_id, plan, plan_id,
                        susp.confirmations, results, "execute", consecutive_failures,
                    )
                    if terminated:
                        return await self._refresh_state(run_id), consecutive_failures
                    layer_failures = [
                        sid for sid in layer
                        if sid in results and not results[sid].is_completed
                    ]
                    if not layer_failures:
                        continue
                    ok = False
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _sched_act.error("[execute] DAG execution failed: %s", exc)
                    await self._fail(run_id, f"DAG execution failed: {exc}")
                    return await self._refresh_state(run_id), consecutive_failures

                if not ok:
                    _sched_act.error("[execute] Layer %d had failures — revising", layer_idx)
                    sys_state = self.dag_executor.build_dag_status_text(plan, results, current_layer=layer_idx)
                    s = await self._refresh_state(run_id)
                    fb = self._get_feedback_text(s, for_revise=True, since_seq=state_seq)
                    _sched_act.info("[execute] Revise after layer failure %s", fmtkv(
                        layer_idx=layer_idx, has_feedback=fb is not None,
                    ))
                    revised = await self.planner.revise(plan, results, sys_state, feedback=fb, intent_fallback=s.intent)
                    if revised is None:
                        consecutive_failures += 1
                        _sched_think.error("[revise] Revise failed after layer failure, failures=%d/%d", consecutive_failures, self.config.max_consecutive_failures)
                        failed = [(sid, r.error or "unknown") for sid, r in results.items() if sid in {s.id for s in plan.steps} and not r.is_completed]
                        error_msg = "; ".join(f"{sid}: {err}" for sid, err in failed) if failed else "unknown error"
                        await self._fail(run_id, f"Steps failed: {error_msg}")
                        return await self._refresh_state(run_id), consecutive_failures

                    if not revised.steps:
                        _sched_think.info("[revise] Task complete after revision")
                        await self.store.append_event(
                            run_id, EventType.PLAN_REVISED,
                            PlanRevisedPayload(
                                plan_id=plan_id, revision_reason="step_failure_revised",
                                intent=revised.intent,
                                remaining_steps_summary="task complete",
                            ).model_dump(),
                        )
                        await self._finalize_with_summary(run_id, plan.intent, "Task completed after revision")
                        return await self._refresh_state(run_id), consecutive_failures

                    _sched_think.info("[revise] Continuing with %d remaining steps", len(revised.steps))
                    _sched_ctrl.info("[self-heal] Layer %d failed — revise returned %d steps → self-healing",
                                     layer_idx, len(revised.steps))
                    await self.store.append_event(
                        run_id, EventType.PLAN_REVISED,
                        PlanRevisedPayload(
                            plan_id=plan_id, revision_reason="step_failure_revised",
                            intent=revised.intent,
                            remaining_steps_summary=f"{len(revised.steps)} steps remaining",
                        ).model_dump(),
                    )
                    plan = revised
                    self_heal_count += 1
                    break

            else:
                total_ok = sum(1 for sid in (s.id for s in plan.steps) if isinstance(results.get(sid), StepResult) and results[sid].is_completed)
                soft_error_sids = [
                    sid for sid in (s.id for s in plan.steps)
                    if isinstance(results.get(sid), StepResult) and results[sid].has_soft_error
                ]
                _sched_ctrl.info("[execute] All %d layers completed — %d/%d steps successful (self-heal rounds=%d)",
                                 len(layers), total_ok, len(plan.steps), self_heal_count)
                if soft_error_sids:
                    _sched_ctrl.info("[revise] %d step(s) had SOFT_ERROR — triggering revise: %s",
                                     len(soft_error_sids), ", ".join(soft_error_sids))
                    sys_state = self.dag_executor.build_dag_status_text(plan, results, current_layer=len(layers) - 1)
                    s = await self._refresh_state(run_id)
                    fb = self._get_feedback_text(s, for_revise=True, since_seq=state_seq)
                    revised = await self.planner.revise(plan, results, sys_state, feedback=fb, intent_fallback=s.intent)
                    if revised is None:
                        _sched_think.warning("[revise] Revise failed after SOFT_ERROR — falling through to finalize")
                    else:
                        await self.store.append_event(
                            run_id, EventType.PLAN_REVISED,
                            PlanRevisedPayload(
                                plan_id=plan_id, revision_reason="soft_error_revised",
                                intent=revised.intent,
                                remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                            ).model_dump(),
                        )
                        if revised.steps:
                            _sched_ctrl.info("[self-heal] Soft-error revise returned %d steps → re-executing", len(revised.steps))
                            plan = revised
                            self_heal_count += 1
                            break
                        _sched_ctrl.info("[revise] Soft-error revise returned empty steps — task complete")
                _sched_iter.info("[plan] PlanCompleted %s: %d/%d steps",
                                 plan_id, total_ok, len(plan.steps))
                await self.store.append_event(
                    run_id, EventType.PLAN_COMPLETED,
                    PlanCompletedPayload(
                        plan_id=plan_id, completed_steps=total_ok,
                        total_layers=len(layers),
                        summary=f"Completed {total_ok}/{len(plan.steps)} steps",
                    ).model_dump(),
                )
                if self.context_manager:
                    state = await self._refresh_state(run_id)
                    await self.context_manager.maybe_compress(run_id, state.seq, state)

                consecutive_failures = 0
                await self._finalize_with_summary(run_id, plan.intent, "Task completed successfully")
                return await self._refresh_state(run_id), consecutive_failures

    async def _generate_answer(self, intent: str, state: RunState, feedback: str | None) -> str:
        """Call LLM to generate a conversational answer when no tools are needed."""
        return await self.planner.generate_answer(intent, state, feedback)

    async def _finalize_with_summary(self, run_id: str, intent: str, fallback_summary: str) -> None:
        """Generate a conversational answer before RunCompleted, or use fallback if LLM unavailable."""
        try:
            state = await self._refresh_state(run_id)
            feedback_text = self._get_feedback_text(state)
            answer = await self._generate_answer(state.intent or intent, state, feedback_text)
            await self.store.append_event(
                run_id, EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought="ANSWER: " + answer,
                    tool_choice=None, token_count=0, tool_calls=None,
                ).model_dump(),
            )
            await self._complete(run_id, answer)
        except Exception as exc:
            _sched_think.warning("[finalize] Summary generation failed: %s — using fallback", exc)
            await self._complete(run_id, fallback_summary)

    async def _get_or_fallback(
        self, run_id: str, intent: str,
        state: RunState, feedback_text: str | None,
    ) -> DagPlan | None:
        plan = await self.planner.plan(intent, state, feedback=feedback_text)
        if plan is not None:
            return plan

        _sched_ctrl.warning("[fallback] Planner failed — falling back to serial AgentLoopScheduler")
        fallback_kernel = _FallbackKernel(self.planner.llm)
        serial = AgentLoopScheduler(
            self.store, self.executor, fallback_kernel,
            self.tool_defs, self.tool_fns, self.config,
            self.context_manager, self.monitor,
        )
        run_state = await serial.run(run_id, intent)
        _sched_ctrl.info("[fallback] Serial scheduler completed with status=%s", run_state.status.value)
        return None
