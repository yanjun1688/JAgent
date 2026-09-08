"""v3.4 (F-6): bounded step-local repair funnel.

Triggered when a DAG step fails, tool-level retries are exhausted, and the
failure is classified as ``step_repair`` (non-transient). BEFORE escalating to
a global revise, the scheduler drives a step-targeted think-act loop within a
trusted budget: the LLM only proposes a replacement action
(``Planner.propose_local_repair``); trusted :func:`validate_local_repair`
mechanically decides (round cap / tool whitelist / no mutating actions).
Failed repair or exhausted budget → escalate to the existing global revise.
"""

from __future__ import annotations

from typing import Any

from harness.core.dag_types import ExecState, StepResult
from harness.core.logger import agent_logger, guard_logger
from harness.core.recovery import RecoveryBudget, classify_failure_tier, validate_local_repair
from harness.models.events import (
    EventType,
    StepLocalRepairCompletedPayload,
    StepLocalRepairStartedPayload,
)
from harness.models.plan import DagPlan, DagStep, RequiredOperation
from harness.models.tools import ToolDefinition

_sched_ctrl = agent_logger("scheduler.control")
_sched_breaker = guard_logger("scheduler.breaker")


def local_repair_step_error(results: dict[str, StepResult], sid: str) -> str | None:
    r = results.get(sid)
    return r.error if isinstance(r, StepResult) else None


def read_only_tool_map(tool_defs: list[ToolDefinition]) -> dict[str, bool]:
    """F-6: 从工具定义机械推导"全只读"工具集（受信，无 LLM）。

    A tool is fully read-only iff its tool-level ``side_effects`` is empty AND
    every operation contract is empty of side effects. Tools with operation-level
    side effects (e.g. http POST, file write) are left out of the map so the
    recovery module's per-operation read-only checks govern them.
    """
    ro: dict[str, bool] = {}
    for td in tool_defs:
        ops = list(td.operations or ())
        op_ro = all(not (op.side_effects) for op in ops)
        if not td.side_effects and op_ro:
            ro[td.name] = True
    return ro


def local_repair_candidates(
    plan: DagPlan,
    results: dict[str, StepResult],
    contracts: list[Any],
) -> list[DagStep]:
    """受信候选：本 plan 内失败分级为 step_repair 的步骤（确定性顺序）。

    FAILED/UNSUCCESSFUL(非 probe) 且非瞬时才可进入局部修复；SKIPPED（依赖门控
    跳过，工具未执行）与瞬时失败（属工具级 retry）不在此列。read-only 探测
    步骤的 UNSUCCESSFUL 是正常结果（step_normal），不会进入。

    DeliveryContract 绑定步骤被排除：局部修复不得改动交付契约指定动作
    （DESIGN §4 红线 — 禁改交付契约），这类失败走全局 revise 由 Planner 决策。
    """
    bound_step_ids = {
        s.id
        for contract in list(contracts or ())
        for s in plan.steps
        if RequiredOperation.step_satisfies(s, contract)
    }
    candidates: list[DagStep] = []
    for step in plan.steps:
        if step.id in bound_step_ids:
            continue
        r = results.get(step.id)
        if not isinstance(r, StepResult) or r.step_normal:
            continue
        if r.exec_state not in (ExecState.FAILED, ExecState.UNSUCCESSFUL):
            continue
        if classify_failure_tier(r.error, retryable=r.retryable) != "step_repair":
            continue
        candidates.append(step)
    candidates.sort(key=lambda s: s.id)
    return candidates


class LocalRepairMixin:
    """Bounded per-step local repair before global revise (trusted budget)."""

    def _local_repair_budget(self) -> RecoveryBudget | None:
        cfg = self.config
        if not cfg.local_repair_enabled:
            return None
        tools = [t for t in cfg.local_repair_allowed_tools if self._find_tool_def(t) is not None]
        return RecoveryBudget(
            max_repair_rounds=max(1, cfg.local_repair_max_rounds),
            allowed_tools=frozenset(tools) if tools else frozenset(),
        )

    def _local_repair_candidates(
        self,
        plan: DagPlan,
        results: dict[str, StepResult],
        contracts: list[Any],
    ) -> list[DagStep]:
        return local_repair_candidates(plan, results, contracts)

    async def _attempt_step_local_repair(
        self,
        run_id: str,
        plan: DagPlan,
        plan_id: str,
        results: dict[str, StepResult],
        repair_rounds: dict[str, int],
        budget: RecoveryBudget,
        contracts: list[Any],
    ) -> DagPlan | None:
        """Attempt ONE bounded local-repair round for the first eligible failing step.

        Applies mechanically-gated decisions only. Returns the repaired plan when a
        proposal was accepted (caller must re-run the DAG layers); ``None`` when no
        step was repaired (fall through to the existing global-revise path).
        """
        candidates = self._local_repair_candidates(plan, results, contracts)
        ro_tools = read_only_tool_map(self.tool_defs)
        for step in candidates:
            used = repair_rounds.get(step.id, 0)
            if used >= budget.max_repair_rounds:
                continue
            r = results[step.id]
            whitelist = sorted(budget.allowed_tools)
            repair_rounds[step.id] = used + 1
            await self._append_run_event(
                run_id,
                EventType.STEP_LOCAL_REPAIR_STARTED,
                StepLocalRepairStartedPayload(
                    step_id=step.id,
                    plan_id=plan_id,
                    repair_round=used + 1,
                    budget_remaining=budget.max_repair_rounds - used - 1,
                    step_tool=step.tool,
                    error=r.error,
                    tool_whitelist=whitelist,
                ).model_dump(),
            )
            if not budget.allowed_tools:
                await self._append_run_event(
                    run_id,
                    EventType.STEP_LOCAL_REPAIR_COMPLETED,
                    StepLocalRepairCompletedPayload(
                        step_id=step.id,
                        plan_id=plan_id,
                        repair_round=used + 1,
                        outcome="exhausted",
                        tool_name=step.tool,
                        error=r.error,
                        reason="empty read-only whitelist — no repair tool available",
                    ).model_dump(),
                )
                continue
            try:
                proposal = await self._phase_call(
                    run_id,
                    "local_repair",
                    self.planner.propose_local_repair(
                        step,
                        r.error,
                        allowed_tools=whitelist,
                        run_id=run_id,
                    ),
                )
            except Exception as exc:  # LLM/proposal failure → escalate, never fake-fix
                _sched_breaker.warning("[local-repair] proposal failed for step=%s: %s", step.id, exc)
                proposal = None
            if proposal is None:
                await self._append_run_event(
                    run_id,
                    EventType.STEP_LOCAL_REPAIR_COMPLETED,
                    StepLocalRepairCompletedPayload(
                        step_id=step.id,
                        plan_id=plan_id,
                        repair_round=used + 1,
                        outcome="rejected",
                        tool_name=step.tool,
                        error=r.error,
                        reason="planner returned no repair proposal",
                    ).model_dump(),
                )
                continue

            decision = validate_local_repair(
                proposal,
                round_used=used + 1,
                budget=budget,
                step_tools_read_only=ro_tools,
            )
            if not decision.allowed:
                await self._append_run_event(
                    run_id,
                    EventType.STEP_LOCAL_REPAIR_COMPLETED,
                    StepLocalRepairCompletedPayload(
                        step_id=step.id,
                        plan_id=plan_id,
                        repair_round=used + 1,
                        outcome="rejected",
                        tool_name=step.tool,
                        error=r.error,
                        proposed_tool=proposal.tool_name,
                        reason=decision.reason,
                    ).model_dump(),
                )
                continue

            # ── Accepted: replace the step's action in a patched plan copy ──
            patched_steps = [
                s.model_copy(
                    update={"tool": proposal.tool_name, "input": dict(proposal.input or {})}
                )
                if s.id == step.id
                else s
                for s in plan.steps
            ]
            patched = plan.model_copy(update={"steps": patched_steps})
            results.pop(step.id, None)  # force re-run under the new action
            await self._append_run_event(
                run_id,
                EventType.STEP_LOCAL_REPAIR_COMPLETED,
                StepLocalRepairCompletedPayload(
                    step_id=step.id,
                    plan_id=plan_id,
                    repair_round=used + 1,
                    outcome="accepted",
                    tool_name=step.tool,
                    error=r.error,
                    proposed_tool=proposal.tool_name,
                    reason="accepted within trusted budget",
                ).model_dump(),
            )
            _sched_ctrl.info(
                "[local-repair] step=%s: %s → %s (round %d/%d) accepted — re-running DAG",
                step.id,
                step.tool,
                proposal.tool_name,
                used + 1,
                budget.max_repair_rounds,
            )
            return patched
        return None
