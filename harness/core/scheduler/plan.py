"""PlanningExecutorScheduler — Plan → Execute(parallel) → Revise cycle (L3).

This module holds the orchestration class only. The trusted, pure / mechanical
rules live in sibling modules and are composed in via mixins:

- :class:`~harness.core.scheduler.completion.CompletionGateMixin` — S06 dual
  completion/deliverable gate.
- :class:`~harness.core.scheduler.classify.ClassifyMixin` — contract extraction
  and the trusted intent-classification conservative gate.
- :class:`~harness.core.scheduler.revision_guard.RevisionGuardMixin` —
  degenerate-revision signature guard, plan merge, delivery-invariant guard.
- :class:`~harness.core.scheduler.local_repair.LocalRepairMixin` — v3.4 (F-6)
  bounded step-local repair funnel.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable
from uuid import uuid4

from harness.core.dag_executor import DagExecutor, PlanSuspended, plan_steps_to_payload
from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.core.fold import RunState, RunStatus, ToolResultStatus
from harness.core.logger import agent_logger, fmtkv, guard_logger
from harness.core.planner import Planner
from harness.core.recovery import rebuild_results_from_evidence, unresolved_known_bad_steps
from harness.core.scheduler.base import BaseScheduler, SchedulerConfig
from harness.core.scheduler.classify import ClassifyMixin
from harness.core.scheduler.completion import CompletionGateMixin, CompletionVerdict
from harness.core.scheduler.local_repair import LocalRepairMixin
from harness.core.scheduler.loop import AgentLoopScheduler
from harness.core.scheduler.revision_guard import RevisionGuardMixin
from harness.models.events import (
    AgentThoughtPayload,
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepSkippedPayload,
    EventType,
    FeedbackCategory,
    FeedbackInjectedPayload,
    FeedbackSource,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
    PlanRevisedPayload,
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


class PlanningExecutorScheduler(
    CompletionGateMixin,
    ClassifyMixin,
    RevisionGuardMixin,
    LocalRepairMixin,
    BaseScheduler,
):
    """V0.7 Scheduler — Plan → Execute(parallel) → Revise cycle.

    Replaces serial think→act→observe with Planner-Executor + DAG.
    Falls back to the old serial path when Planner fails to produce a valid plan.
    """

    scheduler_mode = "planning"

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
        tracer=None,
        run_end_cb: Callable[[str], None] | None = None,
        workspace=None,
        backend=None,
    ):
        super().__init__(
            store,
            executor,
            tool_defs,
            tool_fns,
            config,
            context_manager,
            monitor,
            tracer,
            run_end_cb,
            workspace=workspace,
            backend=backend,
        )
        self.planner = planner
        self.dag_executor = dag_executor

    async def _run_loop(self, run_id: str, intent: str, conversation_context: str = "") -> RunState:
        await self._ensure_run_started(run_id, intent)
        await self._resolve_contracts(run_id, intent)

        needs_tools = await self._classify_intent(run_id, intent)
        if not needs_tools:
            _sched_ctrl.info("[classify] Intent classified as analysis-only — skipping plan/execute")
            try:
                answer = await self._generate_answer(
                    intent,
                    await self._refresh_state(run_id),
                    None,
                    run_id,
                    conversation_context=conversation_context,
                )
            except Exception as exc:
                _sched_think.error("[classify] Answer generation failed: %s", exc)
                await self._complete(run_id, "Task completed")
                return await self._refresh_state(run_id)
            await self._append_run_event(
                run_id,
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought="ANSWER: " + answer,
                    tool_choice=None,
                    token_count=0,
                    tool_calls=None,
                ).model_dump(),
            )
            await self._complete(run_id, answer)
            return await self._refresh_state(run_id)

        state = await self._plan_execute_revise_loop(run_id, intent, conversation_context)
        return state

    async def _handle_dag_confirmations(
        self,
        run_id: str,
        plan: DagPlan,
        plan_id: str,
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
                    _sched_breaker.error(
                        "[breaker] Max confirmation retries (%d) exceeded for DAG step %s",
                        self.config.max_confirm_retries,
                        confirm_sid,
                    )
                    await self._fail(
                        run_id,
                        f"Max confirmation retries ({self.config.max_confirm_retries}) exceeded for step {confirm_sid}",
                    )
                    return True, failed_step_ids, consecutive_failures
                _sched_ctrl.info(
                    "[ctrl] %s confirmation loop: RUN_PAUSED for run=%s step=%s (attempt %d/%d)",
                    tag,
                    run_id,
                    confirm_sid,
                    confirm_retries + 1,
                    self.config.max_confirm_retries,
                )
                await self._append_run_event(
                    run_id,
                    EventType.RUN_PAUSED,
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
                if retry_raw.is_completed or retry_raw.exec_state == ExecState.IDEMPOTENT:
                    results[confirm_sid] = retry_raw
                    _sched_act.info("[%s] Step %s completed after confirmation", tag, confirm_sid)
                    await self._append_run_event(
                        run_id,
                        EventType.DAG_STEP_COMPLETED,
                        DagStepCompletedPayload(
                            plan_id=plan_id,
                            step_id=confirm_sid,
                            output_summary=retry_raw.summary,
                        ).model_dump(),
                    )
                    break
                if retry_raw.needs_confirmation:
                    _sched_act.info(
                        "[%s] Step %s still needs confirmation — pausing again (attempt %d/%d)",
                        tag,
                        confirm_sid,
                        confirm_retries + 1,
                        self.config.max_confirm_retries,
                    )
                    confirm_retries += 1
                    continue
                if retry_raw.exec_state == ExecState.SKIPPED:
                    # v2.2 (P2): 确认后重试时依赖已变非 normal → 门控 SKIPPED。
                    # 不是执行失败，落 DAG_STEP_SKIPPED 记录（D9 可观测），
                    # 不追加 failed_step_ids（layer 检查会依据 step_normal=False 捕获）。
                    _sched_act.warning(
                        "[%s] Step %s SKIPPED after confirmation — %s",
                        tag,
                        confirm_sid,
                        retry_raw.error or "dep_not_normal",
                    )
                    results[confirm_sid] = retry_raw
                    await self._append_run_event(
                        run_id,
                        EventType.DAG_STEP_SKIPPED,
                        DagStepSkippedPayload(
                            plan_id=plan_id,
                            step_id=confirm_sid,
                            reason=retry_raw.error or "dep_not_normal",
                            tool_name=step_map.get(confirm_sid, DagStep(id=confirm_sid)).tool,
                        ).model_dump(),
                    )
                    break
                _sched_act.error(
                    "[%s] Step %s failed after confirmation: %s", tag, confirm_sid, retry_raw.error or "unknown"
                )
                results[confirm_sid] = retry_raw
                await self._append_run_event(
                    run_id,
                    EventType.DAG_STEP_FAILED,
                    DagStepFailedPayload(
                        plan_id=plan_id,
                        step_id=confirm_sid,
                        error=retry_raw.error or "Confirmation result failed",
                        tool_name=step_map.get(confirm_sid, DagStep(id=confirm_sid)).tool,
                    ).model_dump(),
                )
                failed_step_ids.append(confirm_sid)
                break
        return False, failed_step_ids, consecutive_failures

    async def _plan_execute_revise_loop(self, run_id: str, intent: str, conversation_context: str = "") -> RunState:
        state = await self._refresh_state(run_id)
        consecutive_failures = 0
        loop_iteration = 0

        _sched_ctrl.info("[lifecycle] Plan-Execute-Revise loop START for run=%s intent=%s", run_id, intent[:120])
        while True:
            if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return state
            loop_iteration += 1
            if loop_iteration > self.config.max_iterations:
                _sched_breaker.error("[breaker] Exceeded max iterations (%d)", self.config.max_iterations)
                await self._fail(run_id, f"Exceeded max iterations ({self.config.max_iterations})")
                return await self._refresh_state(run_id)
            if self._is_cancelled(run_id):
                await self._fail(run_id, "Run cancelled by user")
                return await self._refresh_state(run_id)

            iter_ctx = self._begin_iteration_trace(loop_iteration)
            try:
                result = await self._plan_cycle(
                    run_id,
                    intent,
                    state,
                    consecutive_failures,
                    conversation_context,
                )
            finally:
                self._end_iteration_trace(iter_ctx)

            if isinstance(result, RunState):
                return result
            consecutive_failures = result
            state = await self._refresh_state(run_id)

        return state

    async def _plan_cycle(
        self,
        run_id: str,
        intent: str,
        state: RunState,
        consecutive_failures: int,
        conversation_context: str = "",
    ) -> RunState | int:
        """Run one Plan → Execute → Revise cycle.

        Returns a terminal RunState when the run should stop, otherwise the
        updated consecutive_failures counter.
        """
        command_state = await self._handle_pending_commands(run_id)
        if command_state is not None:
            if command_state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return command_state
            if command_state.status == RunStatus.PAUSED:
                await self._handle_pause(run_id)
            return consecutive_failures

        if state.status == RunStatus.PAUSED:
            _sched_ctrl.info(
                "[ctrl] Plan loop detected PAUSED for run=%s, pause_reason=%s, pending_confirmations=%d",
                run_id,
                state.pause_reason,
                len(state.pending_confirmations),
            )
            await self._handle_pause(run_id)
            return consecutive_failures

        feedback_text = self._get_feedback_text(state)
        _sched_think.debug(
            "[plan] Planning for intent: %s %s", intent[:120], fmtkv(has_feedback=feedback_text is not None)
        )
        plan = await self._get_or_fallback(
            run_id,
            intent,
            state,
            feedback_text,
            conversation_context,
        )
        if plan is None:
            return await self._refresh_state(run_id)

        await self._append_run_event(
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
            if state.delivery_contracts or plan.declared_operations:
                verdict = self._completion_gate(plan, {}, contracts=list(state.delivery_contracts))
                reason = "Empty plan cannot satisfy delivery contracts"
                if verdict.unmet_step_ids:
                    reason += f": {', '.join(verdict.unmet_step_ids)}"
                _sched_breaker.error("[plan] %s", reason)
                await self._fail(run_id, reason)
                return await self._refresh_state(run_id)
            _sched_think.info("[plan] Empty plan — generating answer")
            try:
                answer = await self._generate_answer(
                    state.intent or intent,
                    state,
                    feedback_text,
                    run_id,
                    conversation_context=conversation_context,
                )
            except Exception as exc:
                _sched_think.error("[plan] Answer generation failed: %s", exc)
                await self._complete(run_id, "Task completed")
                return await self._refresh_state(run_id)
            await self._append_run_event(
                run_id,
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought="ANSWER: " + answer,
                    tool_choice=None,
                    token_count=0,
                    tool_calls=None,
                ).model_dump(),
            )
            await self._complete(run_id, answer)
            return await self._refresh_state(run_id)

        state, consecutive_failures = await self._execute_plan(
            run_id, plan, consecutive_failures, state_seq=state.seq, contracts=list(state.delivery_contracts)
        )
        # S11 (问题十 4): 失败计数来自事件折叠，与终态一致 — 消除 "status=failed failures=0" 矛盾。
        folded_failures = sum(
            1
            for tr in state.tool_results
            if tr.status in (ToolResultStatus.FAILED, ToolResultStatus.TIMEOUT, ToolResultStatus.GUARDRAIL_BLOCKED)
        )
        _sched_ctrl.info(
            "[lifecycle] Plan complete — status=%s failures=%d",
            state.status.value,
            folded_failures,
        )
        if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            return state

        if consecutive_failures >= self.config.max_consecutive_failures:
            _sched_breaker.error("[breaker] TRIP — consecutive_failures=%d", consecutive_failures)
            await self._fail(run_id, f"Circuit breaker: {consecutive_failures} consecutive failures")
            return await self._refresh_state(run_id)

        return consecutive_failures

    async def _execute_plan(
        self,
        run_id: str,
        plan: DagPlan,
        consecutive_failures: int,
        state_seq: int = 0,
        contracts: list[Any] | None = None,
    ) -> tuple[RunState, int]:
        root_plan = plan.model_copy(deep=True)
        step_aliases = {step.id: step.id for step in root_plan.steps}
        results: dict[str, StepResult] = {}
        self_heal_count = 0
        # v3.4 (F-6): per-step local-repair rounds used within this plan execution.
        repair_rounds: dict[str, int] = {}
        # S06: DeliveryContract（来自 RunStarted 折叠）— 完成门交付维度判定的受信输入
        contracts = list(contracts or ())

        # v3.4 (F-4, ADR-011): 从受信证据投影重建执行进度。进程崩溃/重启后 resume 时，
        # 已完成步骤从事件流复原为终态 StepResult，topological_sort 会跳过它们、不重放
        # 副作用（幂等键兜底）；未完成/失败步骤复原为可重跑。全新 run 证据为空 → 无操作。
        try:
            hist_state = await self._refresh_state(run_id)
            plan_ids = {s.id for s in plan.steps}
            prior_evidence = {sid: ev for sid, ev in hist_state.step_evidence.items() if sid in plan_ids}
            if prior_evidence:
                for sid, rebuilt in rebuild_results_from_evidence(prior_evidence).items():
                    results.setdefault(sid, rebuilt)
                # Safety: a step that was terminal under a DIFFERENT tool must not be
                # skipped — the new plan's action is different and has to run.
                plan_tool = {s.id: s.tool for s in plan.steps}
                for sid, r in list(results.items()):
                    ev = prior_evidence.get(sid)
                    if ev and r.should_not_rerun and plan_tool.get(sid) and plan_tool[sid] != ev.tool_name:
                        _sched_ctrl.info(
                            "[execute] step %s tool changed (%s -> %s); re-running instead of resuming",
                            sid,
                            ev.tool_name,
                            plan_tool[sid],
                        )
                        results[sid] = StepResult(step_id=sid, exec_state=ExecState.PENDING)
                terminal = [sid for sid, r in results.items() if r.should_not_rerun]
                _sched_ctrl.info(
                    "[execute] Rebuilt %d step result(s) from evidence projection; %d terminal (skipped on resume): %s",
                    len(results),
                    len(terminal),
                    terminal,
                )
        except Exception as exc:  # rebuild must never block a fresh run
            _sched_act.warning("[execute] Evidence rebuild skipped (%s)", exc)
        # v2.2 (E, U1 收敛闭环): 退化修订守卫由签名比对实现（_find_degenerate_revised_steps
        # + _revise_with_degenerate_guard），在 revise 返回处拒绝"重复已失败动作"的修订，
        # round 2 收敛而非 round 5 熔断。下方 breaker 仅为通用兜底。

        while True:
            if self_heal_count >= self.config.max_consecutive_failures:
                _sched_breaker.error("[breaker] Self-heal loop exceeded max (%d) attempts", self_heal_count)
                consecutive_failures = self_heal_count
                await self._fail(run_id, f"Self-heal exceeded {self_heal_count} attempts — unable to complete plan")
                return await self._refresh_state(run_id), consecutive_failures

            _sched_ctrl.info(
                "[execute] DAG execution attempt round=%d (plan=%d steps, cached=%d results)",
                self_heal_count,
                len(plan.steps),
                len([r for r in results.values() if isinstance(r, StepResult) and r.is_completed]),
            )
            completed_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.should_not_rerun}
            # v2.2 (D8): external deps for $var.field references use output_available
            # (data availability). Completion/gating uses step_normal elsewhere.
            available_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.output_available}
            layers = plan.topological_sort(
                completed_step_ids=completed_ids,
                external_deps=available_ids,
            )
            plan_id = f"plan_{run_id}_{uuid4().hex[:8]}"
            _sched_iter.info("[execute] Executing DAG plan with %d steps in %d layers", len(plan.steps), len(layers))
            _sched_iter.info("[plan] PlanCreated %s: %d steps in %d layers", plan_id, len(plan.steps), len(layers))
            await self._append_run_event(
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
            self._trace_event(
                "PlanCreated",
                metadata={"plan_id": plan_id, "step_count": len(plan.steps), "layer_count": len(layers)},
            )

            all_layers_ok = True
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
                        run_id,
                        plan,
                        plan_id,
                        layer,
                        layer_idx,
                        layers,
                        results,
                    )
                except PlanSuspended as susp:
                    _sched_act.info(
                        "[execute] %d step(s) need confirmation: %s",
                        len(susp.confirmations),
                        ", ".join(sid for sid, _ in susp.confirmations),
                    )
                    terminated, failed_step_ids, consecutive_failures = await self._handle_dag_confirmations(
                        run_id,
                        plan,
                        plan_id,
                        susp.confirmations,
                        results,
                        "execute",
                        consecutive_failures,
                    )
                    if terminated:
                        return await self._refresh_state(run_id), consecutive_failures
                    layer_failures = [sid for sid in layer if sid in results and not results[sid].step_normal]
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
                    # v3.4 (F-6): bounded step-local repair BEFORE global revise.
                    # 受信预算缺失（未启用/白名单空）→ 跳过，走既有全局 revise。
                    budget = self._local_repair_budget()
                    if budget is not None:
                        repaired_plan = await self._attempt_step_local_repair(
                            run_id,
                            plan,
                            plan_id,
                            results,
                            repair_rounds,
                            budget,
                            contracts,
                        )
                        if repaired_plan is not None:
                            plan = repaired_plan
                            self_heal_count += 1
                            _sched_ctrl.info(
                                "[local-repair] step repaired — re-running DAG (self-heal=%d)",
                                self_heal_count,
                            )
                            all_layers_ok = False
                            break
                    _sched_act.error("[execute] Layer %d had failures — revising", layer_idx)
                    sys_state = self.dag_executor.build_dag_status_text(plan, results, current_layer=layer_idx)
                    s = await self._refresh_state(run_id)
                    fb = self._get_feedback_text(s, for_revise=True, since_seq=state_seq)
                    _sched_act.info(
                        "[execute] Revise after layer failure %s",
                        fmtkv(
                            layer_idx=layer_idx,
                            has_feedback=fb is not None,
                        ),
                    )
                    revised, degen_err = await self._revise_with_degenerate_guard(
                        run_id,
                        plan,
                        results,
                        sys_state,
                        fb,
                        s.intent,
                        root_contracts=contracts,
                        intent_raw=s.intent_raw,
                        merge_context=(root_plan, plan, step_aliases),
                    )
                    if degen_err:
                        _sched_breaker.error("[breaker] %s", degen_err)
                        await self._fail(run_id, degen_err)
                        return await self._refresh_state(run_id), consecutive_failures
                    if revised is not None:
                        merged = self._merge_step_tasks(results, revised)
                        if merged:
                            _sched_think.info("[revise] Merged %d step_tasks from LLM assessment", merged)
                    if revised is None:
                        consecutive_failures += 1
                        _sched_think.error(
                            "[revise] Revise failed after layer failure, failures=%d/%d",
                            consecutive_failures,
                            self.config.max_consecutive_failures,
                        )
                        failed = [
                            (sid, r.error or "unknown")
                            for sid, r in results.items()
                            if sid in {s.id for s in plan.steps} and not r.step_normal
                        ]
                        error_msg = "; ".join(f"{sid}: {err}" for sid, err in failed) if failed else "unknown error"
                        await self._fail(run_id, f"Steps failed: {error_msg}")
                        return await self._refresh_state(run_id), consecutive_failures

                    # S08: 守卫已在不变量校验用的合并副本上验证；此处做真实合并。
                    if revised.steps:
                        revised = self._merge_revised_plan(
                            root_plan,
                            plan,
                            revised,
                            results,
                            step_aliases,
                        )
                        merged_errors = self.planner.guardrail.validate(
                            revised,
                            completed_step_ids={sid for sid, result in results.items() if result.step_normal},
                            available_step_ids={sid for sid, result in results.items() if result.output_available},
                        )
                        if merged_errors:
                            await self._fail(run_id, "Merged revision rejected: " + "; ".join(merged_errors))
                            return await self._refresh_state(run_id), consecutive_failures

                        # v3.4 (F-5, ADR-011): 受信守卫 — 合并后的修订不得把"已知坏步骤"
                        # 用同一动作静默加回（e05087b6：s3 browser 失败，补丁只修 s1/s2，
                        # merge 后 s3 browser 用同幂等键重放再败）。检测未覆盖的非瞬时失败
                        # 步骤，注入高优反馈强制下一轮修复，而不是重放已知坏动作。
                        # v3.4 review (A′): "同一动作"= (tool, 规范化 input) 全签名，与
                        # 退化修订守卫共享定义 — 同 tool 改 input 的合法修正不再误判为未修复。
                        merged_patch_steps = plan_steps_to_payload(revised)
                        original_inputs = {s.id: s.input for s in plan.steps}
                        unresolved = unresolved_known_bad_steps(
                            s.step_evidence,
                            merged_patch_steps,
                            original_inputs=original_inputs,
                        )
                        if unresolved:
                            _sched_breaker.warning(
                                "[revise] %d known-bad step(s) not addressed by revision: %s",
                                len(unresolved),
                                ", ".join(unresolved),
                            )
                            detail = ", ".join(
                                f"{sid}({s.step_evidence[sid].tool_name}: "
                                f"{(s.step_evidence[sid].error or '')[:80]})"
                                for sid in unresolved
                            )
                            await self._append_run_event(
                                run_id,
                                EventType.FEEDBACK_INJECTED,
                                FeedbackInjectedPayload(
                                    source=FeedbackSource.MONITOR,
                                    category=FeedbackCategory.TOOL_FAILURE,
                                    feedback_text=(
                                        f"Revision did NOT address {len(unresolved)} still-broken step(s): "
                                        f"{', '.join(unresolved)}. These steps re-add the SAME action "
                                        f"(same tool and same input) that already failed, so a re-run "
                                        f"unchanged will fail again. You MUST either switch them to a "
                                        f"working tool/action or explicitly give them up; do not leave "
                                        f"them on the failing tool. Details: {detail}"
                                    ),
                                    priority="high",
                                    error_type="unresolved_known_bad_steps",
                                    error_detail=detail,
                                    suggestion=(
                                        "Replace the failing tool/action for each listed step, or drop the "
                                        "step and declare task failure."
                                    ),
                                ).model_dump(),
                            )

                    if not revised.steps:
                        # v2.2 (D5, U2 根治) + S06 + v3.4 review #2: revise 空 steps 不等于
                        # 完成。空步骤结局统一走 _settle_empty_revised（与 unsuccessful 分支
                        # 同一条真源：failed 声明 / 完成门 / PLAN_FAILED / finalize 收敛一处）。
                        return await self._settle_empty_revised(
                            run_id,
                            plan_id=plan_id,
                            root_plan=root_plan,
                            results=results,
                            step_aliases=step_aliases,
                            revised=revised,
                            revision_reason="step_failure_revised",
                            contracts=contracts,
                            consecutive_failures=consecutive_failures,
                            layer_count=len(layers),
                        )

                    _sched_think.info("[revise] Continuing with %d remaining steps", len(revised.steps))
                    _sched_ctrl.info(
                        "[self-heal] Layer %d failed — revise returned %d steps → self-healing",
                        layer_idx,
                        len(revised.steps),
                    )
                    # v3.4 review #2: 事件经共享构造器，避免多处内联 PlanRevisedPayload。
                    await self._emit_plan_revised(
                        run_id,
                        plan_id,
                        revised,
                        results,
                        revision_reason="step_failure_revised",
                        remaining_summary=f"{len(revised.steps)} steps remaining",
                    )
                    # v2.2 (E): 签名比对守卫已由 _revise_with_degenerate_guard 在
                    # revise 返回处拒绝退化修订，此处只需接受并继续自愈。
                    plan = revised
                    self_heal_count += 1
                    all_layers_ok = False
                    break

            if all_layers_ok:
                # v2.2 (D5) + S06: 完成门 — 机械聚合 + 交付契约双维判定。
                # verdict 在此一次性计算；下方成功尾直接复用（v3.4 review #2 收敛，不再重复过门）。
                verdict = self._completion_gate(root_plan, results, step_aliases, contracts)
                all_normal = verdict.mechanical_complete
                total_ok = sum(
                    1
                    for sid in (s.id for s in plan.steps)
                    if isinstance(results.get(sid), StepResult) and results[sid].step_normal
                )
                unsuccessful_sids = [
                    sid
                    for sid in (s.id for s in plan.steps)
                    if isinstance(results.get(sid), StepResult) and results[sid].is_unsuccessful
                ]
                skipped_sids = [
                    sid
                    for sid in (s.id for s in plan.steps)
                    if isinstance(results.get(sid), StepResult) and results[sid].exec_state == ExecState.SKIPPED
                ]
                _sched_ctrl.info(
                    "[execute] All %d layers completed — %d/%d steps normal (self-heal rounds=%d)",
                    len(layers),
                    total_ok,
                    len(plan.steps),
                    self_heal_count,
                )
                if not all_normal and (unsuccessful_sids or skipped_sids):
                    not_normal = unsuccessful_sids + skipped_sids
                    _sched_ctrl.info(
                        "[revise] %d step(s) not normal (unsuccessful=%d skipped=%d) — triggering revise: %s",
                        len(not_normal),
                        len(unsuccessful_sids),
                        len(skipped_sids),
                        ", ".join(not_normal),
                    )
                    # v3.4 (F-6): bounded step-local repair BEFORE global revise.
                    budget = self._local_repair_budget()
                    if budget is not None:
                        repaired_plan = await self._attempt_step_local_repair(
                            run_id,
                            plan,
                            plan_id,
                            results,
                            repair_rounds,
                            budget,
                            contracts,
                        )
                        if repaired_plan is not None:
                            plan = repaired_plan
                            self_heal_count += 1
                            _sched_ctrl.info(
                                "[local-repair] step repaired — re-running DAG (self-heal=%d)",
                                self_heal_count,
                            )
                            continue
                    sys_state = self.dag_executor.build_dag_status_text(plan, results, current_layer=len(layers) - 1)
                    s = await self._refresh_state(run_id)
                    fb = self._get_feedback_text(s, for_revise=True, since_seq=state_seq)
                    revised, degen_err = await self._revise_with_degenerate_guard(
                        run_id,
                        plan,
                        results,
                        sys_state,
                        fb,
                        s.intent,
                        root_contracts=contracts,
                        intent_raw=s.intent_raw,
                        merge_context=(root_plan, plan, step_aliases),
                    )
                    if degen_err:
                        _sched_breaker.error("[breaker] %s", degen_err)
                        await self._fail(run_id, degen_err)
                        return await self._refresh_state(run_id), consecutive_failures
                    if revised is None:
                        _sched_think.warning("[revise] Revise failed after UNSUCCESSFUL — falling through to finalize")
                    else:
                        merged = self._merge_step_tasks(results, revised)
                        if merged:
                            _sched_think.info(
                                "[revise] Merged %d step_tasks from LLM assessment (unsuccessful)",
                                merged,
                            )
                        # v3.4 review #2: 事件只在 revise 仍有步骤继续时于此落（文案如实）；
                        # revise 空 steps 的结局统一由 _settle_empty_revised 过门后判定并落准确
                        # summary（消除旧 "task complete" 文案后紧跟 fail 的自相矛盾）。
                        if revised.steps:
                            await self._emit_plan_revised(
                                run_id,
                                plan_id,
                                revised,
                                results,
                                revision_reason="unsuccessful_revised",
                                remaining_summary=f"{len(revised.steps)} steps remaining",
                            )
                            # S08: 守卫已在不变量校验用的合并副本上验证；此处做真实合并。
                            revised = self._merge_revised_plan(
                                root_plan,
                                plan,
                                revised,
                                results,
                                step_aliases,
                            )
                            merged_errors = self.planner.guardrail.validate(
                                revised,
                                completed_step_ids={sid for sid, result in results.items() if result.step_normal},
                                available_step_ids={sid for sid, result in results.items() if result.output_available},
                            )
                            if merged_errors:
                                await self._fail(run_id, "Merged revision rejected: " + "; ".join(merged_errors))
                                return await self._refresh_state(run_id), consecutive_failures
                            _sched_ctrl.info(
                                "[self-heal] Unsuccessful revise returned %d steps → re-executing",
                                len(revised.steps),
                            )
                            plan = revised
                            self_heal_count += 1
                            continue
                        # 空 steps → 与 site1（层失败 revise）共用同一条完成判定/收尾真源。
                        return await self._settle_empty_revised(
                            run_id,
                            plan_id=plan_id,
                            root_plan=root_plan,
                            results=results,
                            step_aliases=step_aliases,
                            revised=revised,
                            revision_reason="unsuccessful_revised",
                            contracts=contracts,
                            consecutive_failures=consecutive_failures,
                            layer_count=len(layers),
                        )

                # v2.2 (D5) + S06: 只有完成门通过（机械 + 交付）才 finalize 完成。
                # v3.4 review #2: 判定/收尾统一走 _dispose_completion_verdict（与 revise-空同源），
                # 消除此处第 3 份内联完成门。verdict 于 all_layers_ok 入口已计算，此处不再重复过门。
                return await self._dispose_completion_verdict(
                    run_id,
                    intent=plan.intent,
                    root_plan=root_plan,
                    plan_id=plan_id,
                    verdict=verdict,
                    consecutive_failures=consecutive_failures,
                    layer_count=len(layers),
                    fallback_summary="Task completed successfully",
                )

    async def _generate_answer(
        self,
        intent: str,
        state: RunState,
        feedback: str | None,
        run_id: str | None = None,
        conversation_context: str = "",
    ) -> str:
        """Call LLM to generate a conversational answer when no tools are needed."""
        return await self._phase_call(
            run_id or "",
            "answer",
            self.planner.generate_answer(
                intent,
                state,
                feedback,
                run_id=run_id,
                conversation_context=conversation_context,
            ),
        )

    async def _finalize_with_summary(
        self,
        run_id: str,
        intent: str,
        fallback_summary: str,
        all_normal: bool = True,
        unmet_step_ids: list[str] | None = None,
        completion: CompletionVerdict | None = None,
    ) -> None:
        """Generate a conversational answer before RunCompleted, or use fallback if LLM unavailable."""
        try:
            state = await self._refresh_authoritative_state(run_id)
            feedback_text = self._get_feedback_text(state)
            answer = await self._generate_answer(
                state.intent or intent,
                state,
                feedback_text,
                run_id,
            )
            await self._append_run_event(
                run_id,
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought="ANSWER: " + answer,
                    tool_choice=None,
                    token_count=0,
                    tool_calls=None,
                ).model_dump(),
            )
            await self._complete(
                run_id,
                answer,
                all_normal=all_normal,
                unmet_step_ids=unmet_step_ids,
                completion=completion,
            )
        except Exception as exc:
            _sched_think.warning("[finalize] Summary generation failed: %s — using fallback", exc)
            await self._complete(run_id, fallback_summary, completion=completion)

    async def _emit_plan_revised(
        self,
        run_id: str,
        plan_id: str,
        revised: DagPlan,
        results: dict[str, StepResult],
        *,
        revision_reason: str,
        remaining_summary: str,
    ) -> None:
        """PlanRevised 事件 + trace 共享构造器（v3.4 review #2 收敛：消灭多处内联 payload）。

        注意：调用方需在 results 处于"该事件应反映的时点"传入（合并前/后由调用方决定），
        以保证 step_tasks 便签与剩余步骤数与调用方语义一致。
        """
        step_tasks = {
            sid: r.task_state.value
            for sid, r in results.items()
            if isinstance(r, StepResult) and r.task_state != TaskState.UNKNOWN
        }
        await self._append_run_event(
            run_id,
            EventType.PLAN_REVISED,
            PlanRevisedPayload(
                plan_id=plan_id,
                revision_reason=revision_reason,
                intent=revised.intent,
                remaining_steps_summary=remaining_summary,
                steps=plan_steps_to_payload(revised),
                step_tasks=step_tasks,
            ).model_dump(),
        )
        self._trace_event(
            "PlanRevised",
            metadata={
                "plan_id": plan_id,
                "reason": revision_reason,
                "remaining_steps": len(revised.steps),
            },
        )

    async def _dispose_completion_verdict(
        self,
        run_id: str,
        *,
        intent: str,
        root_plan: DagPlan,
        plan_id: str,
        verdict: CompletionVerdict,
        consecutive_failures: int,
        layer_count: int,
        fallback_summary: str,
    ) -> tuple[RunState, int]:
        """S06 完成门单一分派（v3.4 review #2 收敛）—— 全路径同一条判定/收尾真源。

        mechanical 不全 → PLAN_FAILED + fail；deliverable failed → PLAN_FAILED + fail；
        通过 → emit PLAN_COMPLETED + maybe_compress + _finalize_with_summary。
        正常完成尾 / 层失败后 revise 空 / unsuccessful revise 空全部汇于此，杜绝完成门
        规则在多处内联导致漏改一处、行为漂移。fail-safe：绝不假绿（C-02）。
        通过路径一律发 PLAN_COMPLETED（任何到达此处的收尾都是完成判定通过）。
        """
        root_total = len(root_plan.steps)
        if not verdict.mechanical_complete:
            final_error = f"Steps not achieved: {', '.join(verdict.unmet_step_ids)}"
            _sched_think.error(
                "[execute] Completion gate FAILED — %d unmet step(s): %s. Failing run (no fake-green).",
                len(verdict.unmet_step_ids),
                ", ".join(verdict.unmet_step_ids),
            )
            await self._append_run_event(
                run_id,
                EventType.PLAN_FAILED,
                PlanFailedPayload(
                    plan_id=plan_id,
                    completed_steps=root_total - len(verdict.unmet_step_ids),
                    total_layers=layer_count,
                    final_error=final_error,
                ).model_dump(),
            )
            await self._fail(run_id, final_error)
            return await self._refresh_state(run_id), consecutive_failures
        if verdict.deliverable_status == "failed":
            # 契约存在但未达成 → 绝不宣称交付达成（C-02），fail run。
            unmet_contracts = [v.contract_id for v in verdict.deliverables if v.status != "met"]
            final_error = f"Deliverable not met: {', '.join(unmet_contracts)}"
            _sched_think.error(
                "[execute] Deliverable gate FAILED — %d contract(s) unmet: %s. Failing run (no fake-green).",
                len(unmet_contracts),
                ", ".join(unmet_contracts),
            )
            await self._append_run_event(
                run_id,
                EventType.PLAN_FAILED,
                PlanFailedPayload(
                    plan_id=plan_id,
                    completed_steps=root_total - len(verdict.unmet_step_ids),
                    total_layers=layer_count,
                    final_error=final_error,
                ).model_dump(),
            )
            await self._fail(run_id, final_error)
            return await self._refresh_state(run_id), consecutive_failures
        root_completed = root_total - len(verdict.unmet_step_ids)
        _sched_iter.info("[plan] PlanCompleted %s: %d/%d root steps", plan_id, root_completed, root_total)
        await self._append_run_event(
            run_id,
            EventType.PLAN_COMPLETED,
            PlanCompletedPayload(
                plan_id=plan_id,
                completed_steps=root_completed,
                total_layers=layer_count,
                summary=f"Completed {root_completed}/{root_total} root steps",
            ).model_dump(),
        )
        if self.context_manager:
            state = await self._refresh_state(run_id)
            await self.context_manager.maybe_compress(run_id, state.seq, state)
        await self._finalize_with_summary(
            run_id, intent, fallback_summary, all_normal=True, unmet_step_ids=[], completion=verdict
        )
        return await self._refresh_state(run_id), 0

    async def _settle_empty_revised(
        self,
        run_id: str,
        *,
        plan_id: str,
        root_plan: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str],
        revised: DagPlan,
        revision_reason: str,
        contracts: list[Any] | None,
        consecutive_failures: int,
        layer_count: int,
    ) -> tuple[RunState, int]:
        """revise 返回空 steps 的唯一分派（site1/site2 共用，v3.4 review #2 收敛）。

        revised.failed（LLM 声明任务不可完成）→ 如实落 "task failed" PLAN_REVISED + fail；
        否则过完成门 → 按结果落准确 summary 的 PLAN_REVISED → _dispose_completion_verdict
        （mechanical / deliverable / 成功三态与正常完成路径同源，绝不假绿 C-02）。
        """
        if revised.failed:
            _sched_think.error("[revise] LLM declares task cannot be completed: %s", revised.intent)
            await self._emit_plan_revised(
                run_id,
                plan_id,
                revised,
                results,
                revision_reason=revision_reason,
                remaining_summary=f"task failed: {revised.intent}",
            )
            await self._fail(run_id, f"Task cannot be completed: {revised.intent}")
            return await self._refresh_state(run_id), consecutive_failures

        # v2.2 (D5, U2 根治): revise 返回空 steps 不等于完成。
        # S06: 完成门 = 机械聚合 + 交付契约双维判定。
        verdict = self._completion_gate(root_plan, results, step_aliases, contracts)
        all_normal = verdict.mechanical_complete
        if all_normal:
            _sched_think.info("[revise] Task complete after revision (completion gate: all normal)")
        else:
            _sched_think.error(
                "[revise] Revise returned empty steps but %d step(s) NOT normal — %s",
                len(verdict.unmet_step_ids),
                ", ".join(verdict.unmet_step_ids),
            )
        await self._emit_plan_revised(
            run_id,
            plan_id,
            revised,
            results,
            revision_reason=revision_reason,
            remaining_summary=(
                "task complete" if all_normal else f"NOT complete — unmet: {', '.join(verdict.unmet_step_ids)}"
            ),
        )
        return await self._dispose_completion_verdict(
            run_id,
            intent=revised.intent,
            root_plan=root_plan,
            plan_id=plan_id,
            verdict=verdict,
            consecutive_failures=consecutive_failures,
            layer_count=layer_count,
            fallback_summary="Task completed after revision",
        )

    async def _get_or_fallback(
        self,
        run_id: str,
        intent: str,
        state: RunState,
        feedback_text: str | None,
        conversation_context: str = "",
    ) -> DagPlan | None:
        plan = await self._phase_call(
            run_id,
            "plan",
            self.planner.plan(
                intent,
                state,
                feedback=feedback_text,
                conversation_context=conversation_context,
                run_id=run_id,
            ),
        )
        if plan is not None:
            return plan

        _sched_ctrl.warning("[fallback] Planner failed — falling back to serial AgentLoopScheduler")
        from harness.core.agent_kernel import LLMAgentKernel

        fallback_kernel = LLMAgentKernel(self.planner.llm)
        serial = AgentLoopScheduler(
            self.store,
            self.executor,
            fallback_kernel,
            self.tool_defs,
            self.tool_fns,
            self.config,
            self.context_manager,
            self.monitor,
            self.tracer,
            workspace=self.workspace,
            backend=self.backend,
        )
        run_state = await serial.run(run_id, intent)
        _sched_ctrl.info("[fallback] Serial scheduler completed with status=%s", run_state.status.value)
        return None
