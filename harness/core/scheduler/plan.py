"""PlanningExecutorScheduler — Plan → Execute(parallel) → Revise cycle (L3)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from uuid import uuid4

from harness.core.dag_executor import DagExecutor, PlanSuspended, plan_steps_to_payload
from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.core.fold import RunState, RunStatus, ToolResultStatus
from harness.core.logger import agent_logger, fmtkv, guard_logger
from harness.core.planner import Planner, revision_invariant_feedback, validate_revision_invariants
from harness.core.scheduler.base import BaseScheduler, SchedulerConfig
from harness.core.scheduler.loop import AgentLoopScheduler
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import (
    AgentThoughtPayload,
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepSkippedPayload,
    DeliveryContractsResolvedPayload,
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
    PlanRevisedPayload,
    RunPausedPayload,
)
from harness.models.plan import DagPlan, DagStep, RequiredOperation
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor

_sched_iter = agent_logger("scheduler.iter")
_sched_think = agent_logger("scheduler.think")
_sched_act = agent_logger("scheduler.act")
_sched_ctrl = agent_logger("scheduler.control")
_sched_breaker = guard_logger("scheduler.breaker")


def _normalize_input(inp: dict[str, Any]) -> str:
    """规范化工具输入，用于退化修订守卫的签名比对。"""
    return json.dumps(inp or {}, sort_keys=True, default=str)


def _step_signature(step: DagStep) -> tuple[str, str]:
    """步骤动作签名：(tool, 规范化 input)。退化修订守卫据此比对。"""
    return (step.tool, _normalize_input(step.input))


# ── S06: 完成门双维判定（D-03 / D-04 / D-05 / C-02 / C-06）────────


@dataclass
class DeliverableVerdict:
    contract_id: str
    status: Literal["met", "unmet", "unverified"]
    matched_step_ids: list[str]


@dataclass
class CompletionVerdict:
    mechanical_complete: bool
    unmet_step_ids: list[str]
    deliverables: list[DeliverableVerdict] = field(default_factory=list)
    deliverable_met: bool = False
    deliverable_status: str = "unverified"  # "met" | "unverified" | "failed"

    @classmethod
    def compute(
        cls,
        plan: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str] | None = None,
        contracts: list[Any] | None = None,
    ) -> "CompletionVerdict":
        aliases = step_aliases or {}
        unmet = [
            sid
            for sid in (s.id for s in plan.steps)
            if not (
                isinstance(results.get(aliases.get(sid, sid)), StepResult)
                and results[aliases.get(sid, sid)].step_normal
            )
        ]
        # Q-02 (ADR-009): LLM 自报 declared_operations 仍属机械维度（不依赖契约来源，
        # 不代表交付达成；交付维度只由 DeliveryContract 驱动）。
        for i, req in enumerate(plan.declared_operations):
            matching_normal = any(
                isinstance(results.get(aliases.get(s.id, s.id)), StepResult)
                and results[aliases.get(s.id, s.id)].step_normal
                for s in plan.steps
                if RequiredOperation.step_satisfies(s, req)
            )
            if not matching_normal:
                unmet.append(f"declared_op#{i}:{req.tool} {req.input}")
        mechanical_complete = len(unmet) == 0

        contracts = list(contracts or ())
        deliverables = verify_deliverables(contracts, plan, results, aliases)
        if not contracts:
            deliverable_met = False
            deliverable_status = "unverified"  # D-04
        else:
            deliverable_met = bool(deliverables) and all(v.status == "met" for v in deliverables)
            deliverable_status = "met" if deliverable_met else "failed"
        return cls(
            mechanical_complete=mechanical_complete,
            unmet_step_ids=unmet,
            deliverables=deliverables,
            deliverable_met=deliverable_met,
            deliverable_status=deliverable_status,
        )


def verify_deliverables(
    contracts: list[Any],
    plan: DagPlan,
    results: dict[str, StepResult],
    step_aliases: dict[str, str] | None = None,
) -> list[DeliverableVerdict]:
    """C-06: 受信校验器 — 只对照已存在的契约验证达成度，绝不推断用户意图。

    判定规则（D-03）：契约 tool/input 匹配 step 且该 step step_normal → met。
    匹配复用 ``RequiredOperation.step_satisfies``（结构化子集匹配）。
    """
    aliases = step_aliases or {}
    verdicts: list[DeliverableVerdict] = []
    for contract in contracts:
        matched = [
            s.id
            for s in plan.steps
            if RequiredOperation.step_satisfies(s, contract)
            and isinstance(results.get(aliases.get(s.id, s.id)), StepResult)
            and results[aliases.get(s.id, s.id)].step_normal
        ]
        verdicts.append(
            DeliverableVerdict(
                contract_id=contract.contract_id,
                status="met" if matched else "unmet",
                matched_step_ids=matched,
            )
        )
    return verdicts


# ── classify 受信保守门（Bug JAGENT-2026-P1-13）──────────────────────
# 不能依赖 LLM 的 "no" 来决定是否进入 Tool Layer —— 真实模型曾对
# "写入 ../blackbox-escape.txt" 返回 no，导致文件写入请求绕过整个
# Guardrail/Tool Layer。这里是受信组件：确定性规则命中即强制
# needs_tools=True，LLM 的 "no" 只在意图无任何工具信号时才生效。

_TOOL_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 文件操作：文件/路径/workspace 语义
    re.compile(r"\b(file|write|read|create|delete|append|list|cat|mkdir|rm|touch)\b", re.IGNORECASE),
    re.compile(r"\.(txt|md|json|yaml|yml|toml|ini|log|csv|py|js|ts|html|css)\b", re.IGNORECASE),
    re.compile(r"(workspace|directory|folder|path|directories|父目录|工作区|目录|路径|文件)", re.IGNORECASE),
    # 路径越界信号
    re.compile(r"(\.\./|\.\.\\|parent dir)", re.IGNORECASE),
    re.compile(r"[a-zA-Z]:[\\/]", re.IGNORECASE),
    # 网络操作
    re.compile(r"https?://|www\.|\.com|\.org|\.io|\.net", re.IGNORECASE),
    re.compile(r"\b(web|search|fetch|http|browser|navigate|api|url|download|upload|curl|request)\b", re.IGNORECASE),
    # MCP / 外部服务
    re.compile(r"\b(mcp|playwright|memory|screenshot|query|execute)\b", re.IGNORECASE),
)

# 上述模式内的词语 —— 纯闲聊也可能出现，故需"强信号"独立判定。
# 若只匹配到这些弱词，仍交由 LLM 决定（但最终仍以 needs_tools 保守为准）。
_WEAK_TOOL_SIGNALS: tuple[str, ...] = ("file", "write", "read", "create", "search", "list")


def _intent_requires_tools(intent: str) -> bool:
    """受信保守门：意图是否含确定性工具操作信号。

    Returns True 表示必须进入 Tool Layer（Guardrail 才有机会拦截），
    即使 LLM classify 返回 "no" 也不能绕过。
    """
    if not intent:
        return False
    lowered = intent.lower()
    return any(p.search(lowered) for p in _TOOL_SIGNAL_PATTERNS)


class PlanningExecutorScheduler(BaseScheduler):
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

    async def _resolve_contracts(self, run_id: str, intent: str) -> None:
        """S07 (D-02 / 方案 B): 首轮 plan 前的契约解析（run 内异步前置）。

        API 层不再在 HTTP 请求内同步等待抽取（不再阻塞 create_run 响应）。
        本方法在 scheduler 后台生命周期内强制执行契约解析，保证完成门在
        plan/execute 前拿到 settle 后的契约。超时/失败 → contracts=[] +
        unverified（D-04 兜底，不阻断 Run）。
        """
        state = await self._refresh_state(run_id)
        if not state.requires_contract_extraction:
            return
        if self.planner.llm is None or self.planner.registry is None:
            _sched_ctrl.info("[extract] LLM/registry unavailable — skipping extraction (D-04)")
            return
        from harness.core.contract_extractor import CONTRACT_EXTRACT_TIMEOUT, ContractExtractor

        extractor = ContractExtractor(self.planner.llm, self.planner.registry)
        remaining = self._run_remaining_s(run_id)
        cap = CONTRACT_EXTRACT_TIMEOUT if remaining is None else min(CONTRACT_EXTRACT_TIMEOUT, remaining)
        try:
            extracted = await asyncio.wait_for(extractor.extract(intent), timeout=cap)
            await self._append_run_event(
                run_id,
                EventType.DELIVERY_CONTRACTS_RESOLVED,
                DeliveryContractsResolvedPayload(
                    contracts=extracted,
                    source="extracted",
                ).model_dump(),
            )
        except asyncio.TimeoutError:
            _sched_ctrl.warning("[extract] Contract extraction timed out for run=%s — unverified (D-04)", run_id)
            await self._append_run_event(
                run_id,
                EventType.DELIVERY_CONTRACTS_RESOLVED,
                DeliveryContractsResolvedPayload(
                    contracts=[],
                    source="extracted",
                    timed_out=True,
                    error="contract extraction timed out",
                ).model_dump(),
            )
        except Exception as exc:
            _sched_think.warning("[extract] Contract extraction failed for run=%s: %s — unverified (D-04)", run_id, exc)
            await self._append_run_event(
                run_id,
                EventType.DELIVERY_CONTRACTS_RESOLVED,
                DeliveryContractsResolvedPayload(
                    contracts=[],
                    source="extracted",
                    error=repr(exc),
                ).model_dump(),
            )

    async def _classify_intent(self, run_id: str, intent: str) -> bool:
        """Return True if the intent needs external tools, False if analysis-only.

        Bug JAGENT-2026-P1-13: LLM classify='no' 曾绕过整个 Tool Layer/Guardrail
        （文件写入请求被当成分析请求）。受信保守门先于 LLM 判定：
        若意图含确定性工具操作信号（文件/路径/URL/浏览器/workspace），
        直接返回 needs_tools=True，LLM 的 "no" 不再生效。
        """
        truncated = intent[:500] if len(intent) > 500 else intent
        if _intent_requires_tools(truncated):
            _sched_ctrl.info(
                "[classify] TRUSTED GATE forced needs_tools=True (tool signal detected): %s",
                truncated[:80],
            )
            return True

        prompt = get_prompt(AgentPhase.CLASSIFY, intent=truncated)
        _sched_ctrl.debug(
            "[classify] phase=%s current_request=%s context_len=0",
            AgentPhase.CLASSIFY.value,
            truncated[:80],
        )
        try:
            _t0 = time.monotonic()
            chat_resp = await self._phase_call(
                run_id,
                "classify",
                self.planner.llm.chat(
                    [{"role": "system", "content": prompt}],
                    temperature=0.0,
                    max_tokens=4,
                    run_id=run_id,
                ),
            )
            _sched_ctrl.info(
                "[llm] phase=classify run=%s duration_ms=%d chars=%d",
                run_id,
                int((time.monotonic() - _t0) * 1000),
                len(chat_resp.content) if chat_resp and chat_resp.content else 0,
            )
        except asyncio.TimeoutError:
            _sched_think.warning("[classify] Phase timed out — assuming needs_tools=True (conservative)")
            return True
        except Exception as exc:
            _sched_think.warning("[classify] LLM call failed: %s — assuming needs_tools=True", exc)
            return True
        result = chat_resp.content.strip().lower()
        needs = result != "no"
        _sched_ctrl.info(
            "[classify] current_request=%s needs_tools=%s raw=%s",
            truncated[:80],
            needs,
            result[:20],
        )
        return needs

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
        # S06: DeliveryContract（来自 RunStarted 折叠）— 完成门交付维度判定的受信输入
        contracts = list(contracts or ())
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

                    if not revised.steps:
                        if revised.failed:
                            _sched_think.error("[revise] LLM declares task cannot be completed: %s", revised.intent)
                            await self._append_run_event(
                                run_id,
                                EventType.PLAN_REVISED,
                                PlanRevisedPayload(
                                    plan_id=plan_id,
                                    revision_reason="step_failure_revised",
                                    intent=revised.intent,
                                    remaining_steps_summary=f"task failed: {revised.intent}",
                                    steps=plan_steps_to_payload(revised),
                                    step_tasks={
                                        sid: r.task_state.value
                                        for sid, r in results.items()
                                        if isinstance(r, StepResult) and r.task_state != TaskState.UNKNOWN
                                    },
                                ).model_dump(),
                            )
                            self._trace_event(
                                "PlanRevised",
                                level="WARNING",
                                metadata={"plan_id": plan_id, "reason": "task_failed", "intent": revised.intent[:120]},
                            )
                            await self._fail(run_id, f"Task cannot be completed: {revised.intent}")
                            return await self._refresh_state(run_id), consecutive_failures

                        # v2.2 (D5, U2 根治): revise 返回空 steps 不等于完成。
                        # S06: 完成门 = 机械聚合 + 交付契约双维判定。
                        verdict = self._completion_gate(root_plan, results, step_aliases, contracts)
                        all_normal = verdict.mechanical_complete
                        unmet = verdict.unmet_step_ids
                        if all_normal:
                            _sched_think.info("[revise] Task complete after revision (completion gate: all normal)")
                        else:
                            _sched_think.error(
                                "[revise] Revise returned empty steps but %d step(s) NOT normal — %s",
                                len(unmet),
                                ", ".join(unmet),
                            )
                        await self._append_run_event(
                            run_id,
                            EventType.PLAN_REVISED,
                            PlanRevisedPayload(
                                plan_id=plan_id,
                                revision_reason="step_failure_revised",
                                intent=revised.intent,
                                remaining_steps_summary=(
                                    "task complete" if all_normal else f"NOT complete — unmet: {', '.join(unmet)}"
                                ),
                                steps=plan_steps_to_payload(revised),
                                step_tasks={
                                    sid: r.task_state.value
                                    for sid, r in results.items()
                                    if isinstance(r, StepResult) and r.task_state != TaskState.UNKNOWN
                                },
                            ).model_dump(),
                        )
                        self._trace_event(
                            "PlanRevised",
                            metadata={
                                "plan_id": plan_id,
                                "reason": "step_failure_revised",
                                "task_complete": all_normal,
                            },
                        )
                        # fail-safe：宁可标未达成，绝不假绿（C-02）。
                        return await self._finalize_or_fail_verdict(
                            run_id, plan.intent, "Task completed after revision", verdict, consecutive_failures
                        )

                    _sched_think.info("[revise] Continuing with %d remaining steps", len(revised.steps))
                    _sched_ctrl.info(
                        "[self-heal] Layer %d failed — revise returned %d steps → self-healing",
                        layer_idx,
                        len(revised.steps),
                    )
                    await self._append_run_event(
                        run_id,
                        EventType.PLAN_REVISED,
                        PlanRevisedPayload(
                            plan_id=plan_id,
                            revision_reason="step_failure_revised",
                            intent=revised.intent,
                            remaining_steps_summary=f"{len(revised.steps)} steps remaining",
                            steps=plan_steps_to_payload(revised),
                            step_tasks={
                                sid: r.task_state.value
                                for sid, r in results.items()
                                if isinstance(r, StepResult) and r.task_state != TaskState.UNKNOWN
                            },
                        ).model_dump(),
                    )
                    self._trace_event(
                        "PlanRevised",
                        metadata={
                            "plan_id": plan_id,
                            "reason": "step_failure_revised",
                            "remaining_steps": len(revised.steps),
                        },
                    )
                    # v2.2 (E): 签名比对守卫已由 _revise_with_degenerate_guard 在
                    # revise 返回处拒绝退化修订，此处只需接受并继续自愈。
                    plan = revised
                    self_heal_count += 1
                    all_layers_ok = False
                    break

            if all_layers_ok:
                # v2.2 (D5) + S06: 完成门 — 机械聚合 + 交付契约双维判定。
                verdict = self._completion_gate(root_plan, results, step_aliases, contracts)
                all_normal = verdict.mechanical_complete
                unmet = verdict.unmet_step_ids
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
                        # v2.2 (D11): task_state 审计便签随 PLAN_REVISED 落事件。
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
                                revision_reason="unsuccessful_revised",
                                intent=revised.intent,
                                remaining_steps_summary=(
                                    f"{len(revised.steps)} steps remaining" if revised.steps else "task complete"
                                ),
                                steps=plan_steps_to_payload(revised),
                                step_tasks=step_tasks,
                            ).model_dump(),
                        )
                        self._trace_event(
                            "PlanRevised",
                            level="WARNING",
                            metadata={
                                "plan_id": plan_id,
                                "reason": "unsuccessful_revised",
                                "remaining_steps": len(revised.steps),
                            },
                        )
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
                            _sched_ctrl.info(
                                "[self-heal] Unsuccessful revise returned %d steps → re-executing",
                                len(revised.steps),
                            )
                            plan = revised
                            self_heal_count += 1
                            continue
                        if revised.failed:
                            _sched_think.error(
                                "[revise] LLM declares task cannot be completed after unsuccessful: %s",
                                revised.intent,
                            )
                            await self._fail(run_id, f"Task cannot be completed: {revised.intent}")
                            return await self._refresh_state(run_id), consecutive_failures

                        # v2.2 (D5, U2 根治) + S06: revise 空 steps 后仍须过完成门。
                        verdict3 = self._completion_gate(root_plan, results, step_aliases, contracts)
                        if not verdict3.mechanical_complete:
                            _sched_think.error(
                                "[revise] Unsuccessful revise returned empty steps but %d unmet — "
                                "failing (no fake-green): %s",
                                len(verdict3.unmet_step_ids),
                                ", ".join(verdict3.unmet_step_ids),
                            )
                            error_msg = f"Steps not achieved: {', '.join(verdict3.unmet_step_ids)}"
                            await self._fail(run_id, error_msg)
                            return await self._refresh_state(run_id), consecutive_failures
                        _sched_ctrl.info(
                            "[revise] Unsuccessful revise returned empty steps — completion gate: all normal"
                        )

                # v2.2 (D5) + S06: 只有完成门通过（机械 + 交付）才 finalize 完成。
                verdict = self._completion_gate(root_plan, results, step_aliases, contracts)
                if not verdict.mechanical_complete:
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
                            completed_steps=total_ok,
                            total_layers=len(layers),
                            final_error=f"Steps not achieved: {', '.join(verdict.unmet_step_ids)}",
                        ).model_dump(),
                    )
                    await self._fail(run_id, f"Steps not achieved: {', '.join(verdict.unmet_step_ids)}")
                    return await self._refresh_state(run_id), consecutive_failures
                if verdict.deliverable_status == "failed":
                    # 契约存在但未达成 → 绝不宣称交付达成（C-02），fail run。
                    unmet_contracts = [v.contract_id for v in verdict.deliverables if v.status != "met"]
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
                            completed_steps=total_ok,
                            total_layers=len(layers),
                            final_error=f"Deliverable not met: {', '.join(unmet_contracts)}",
                        ).model_dump(),
                    )
                    await self._fail(run_id, f"Deliverable not met: {', '.join(unmet_contracts)}")
                    return await self._refresh_state(run_id), consecutive_failures

                root_total = len(root_plan.steps)
                root_completed = root_total - len(verdict.unmet_step_ids)
                _sched_iter.info("[plan] PlanCompleted %s: %d/%d root steps", plan_id, root_completed, root_total)
                await self._append_run_event(
                    run_id,
                    EventType.PLAN_COMPLETED,
                    PlanCompletedPayload(
                        plan_id=plan_id,
                        completed_steps=root_completed,
                        total_layers=len(layers),
                        summary=f"Completed {root_completed}/{root_total} root steps",
                    ).model_dump(),
                )
                if self.context_manager:
                    state = await self._refresh_state(run_id)
                    await self.context_manager.maybe_compress(run_id, state.seq, state)

                consecutive_failures = 0
                await self._finalize_with_summary(
                    run_id,
                    plan.intent,
                    "Task completed successfully",
                    all_normal=True,
                    unmet_step_ids=[],
                    completion=verdict,
                )
                return await self._refresh_state(run_id), consecutive_failures

    @staticmethod
    def _completion_gate(
        plan: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str] | None = None,
        contracts: list[Any] | None = None,
    ) -> CompletionVerdict:
        """S06: 完成门双维判定（mechanical + deliverable）。

        v2.2 (D5/D12): 机械完成 = 全局原始步骤 step_normal 聚合 + LLM 自报
        declared_operations 达成（Q-02，机械维度，不代表交付）。S06 (D-03/D-04):
        交付维度 = DeliveryContract 逐条判定（tool/input 匹配 + step_normal）。
        空契约 → deliverable_status="unverified"。绝不宣称交付达成（C-02 fail-safe）。
        """
        return CompletionVerdict.compute(plan, results, step_aliases, contracts)

    @staticmethod
    def _merge_revised_plan(
        root_plan: DagPlan,
        current_plan: DagPlan,
        revised: DagPlan,
        results: dict[str, StepResult],
        step_aliases: dict[str, str],
    ) -> DagPlan:
        """Merge a revision without losing original downstream work.

        The LLM may return only a replacement for a failed step. The trusted
        scheduler restores original downstream steps, rewrites dependencies
        through the replacement alias, and clears stale SKIPPED results.
        """
        root_steps = {step.id: step for step in root_plan.steps}
        revised_steps = {step.id: step for step in revised.steps}
        current_ids = {step.id for step in current_plan.steps}
        unresolved = [
            step.id
            for step in root_plan.steps
            if not (
                isinstance(results.get(step_aliases.get(step.id, step.id)), StepResult)
                and results[step_aliases.get(step.id, step.id)].step_normal
            )
        ]
        replacement_ids = [sid for sid in revised_steps if sid not in root_steps and sid not in current_ids]
        # Prefer a unique exact signature match for every unresolved step. This
        # prevents LLM output ordering from deciding which failed step a
        # replacement satisfies. Signature matching is intentionally exact:
        # aliases such as HTTP ``method``/``action`` are not normalized here
        # because guessing tool equivalence could merge side-effecting actions.
        unmatched_replacements = set(replacement_ids)
        unmatched_originals: list[str] = []
        for original_id in unresolved:
            prior = results.get(step_aliases.get(original_id, original_id))
            if not isinstance(prior, StepResult) or prior.exec_state not in (
                ExecState.FAILED,
                ExecState.UNSUCCESSFUL,
                ExecState.SKIPPED,
            ):
                continue
            original_step = root_steps[original_id]
            matches = [
                sid
                for sid in unmatched_replacements
                if _step_signature(revised_steps[sid]) == _step_signature(original_step)
            ]
            if len(matches) == 1:
                step_aliases[original_id] = matches[0]
                unmatched_replacements.remove(matches[0])
            else:
                unmatched_originals.append(original_id)

        # D12 unambiguous 1:1 binding (replaces D12 "B replacement" in a serial
        # chain): when after signature matching exactly ONE replacement and
        # exactly ONE ran-and-failed (FAILED/UNSUCCESSFUL, NOT SKIPPED) original
        # step remain, the mapping is forced — bind them even though the input
        # changed. SKIPPED steps are excluded: they never ran, so they are
        # restored instead of replaced. This is NOT positional guesswork: with
        # 2+ candidates on either side the binding is refused (fail-safe, M4).
        ran_and_failed = [
            sid
            for sid in unmatched_originals
            if results[step_aliases.get(sid, sid)].exec_state in (ExecState.FAILED, ExecState.UNSUCCESSFUL)
        ]
        if len(unmatched_replacements) == 1 and len(ran_and_failed) == 1:
            replacement_id = next(iter(unmatched_replacements))
            step_aliases[ran_and_failed[0]] = replacement_id
            unmatched_replacements.discard(replacement_id)

        merged: dict[str, DagStep] = dict(revised_steps)
        for original_id, original_step in root_steps.items():
            alias = step_aliases.get(original_id, original_id)
            if alias != original_id or original_id in revised_steps:
                continue
            prior = results.get(original_id)
            if isinstance(prior, StepResult) and prior.step_normal:
                continue
            rewritten_deps = [step_aliases.get(dep, dep) for dep in original_step.depends_on]
            merged[original_id] = original_step.model_copy(update={"depends_on": rewritten_deps})

        for sid in list(merged):
            prior = results.get(sid)
            if isinstance(prior, StepResult) and not prior.step_normal:
                results.pop(sid, None)
        for original_id, alias in step_aliases.items():
            if alias != original_id:
                # The original result is no longer a canonical dependency.
                # Leaving it available would let a later revision consume a
                # stale UNSUCCESSFUL output through external_deps.
                results.pop(original_id, None)

        return revised.model_copy(update={"steps": list(merged.values())})

    @staticmethod
    def _find_degenerate_revised_steps(
        plan: DagPlan,
        results: dict[str, StepResult],
        revised: DagPlan,
    ) -> list[str]:
        """v2.2 (E/U1): 签名比对 — 找出修订计划中必然重复失败的步骤（退化修订）。

        A revised step is degenerate iff:
          1. its (tool, normalized input) matches a step in ``results`` that is
             FAILED or UNSUCCESSFUL (non-probe), AND
          2. its transitive dependency closure (within the revised plan) adds no
             NEW step — every closure member's signature was already in ``plan``.

        Condition 2 means: re-running the same action with the same input cannot
        possibly yield a different outcome (no new upstream data source). This
        converges U1 at round 2 (reject the degenerate revision, force the LLM
        to change strategy) instead of round 5 (outer breaker).

        Returns the ids of degenerate revised steps (empty = acceptable revision).
        """
        seen_sigs = {_step_signature(s) for s in plan.steps}
        failed_sigs = {
            _step_signature(s)
            for s in plan.steps
            if (
                isinstance(results.get(s.id), StepResult)
                and (
                    results[s.id].exec_state == ExecState.FAILED
                    or (results[s.id].exec_state == ExecState.UNSUCCESSFUL and not results[s.id].probe)
                )
            )
        }
        if not failed_sigs:
            return []

        revised_map = {s.id: s for s in revised.steps}

        def _closure(sid: str) -> set[str]:
            seen: set[str] = set()
            stack = [sid]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                step = revised_map.get(cur)
                if step:
                    stack.extend(d for d in step.depends_on if d in revised_map)
            return seen

        degenerate: list[str] = []
        for rs in revised.steps:
            if _step_signature(rs) not in failed_sigs:
                continue
            cl = _closure(rs.id)
            cl_sigs = {_step_signature(revised_map[cid]) for cid in cl}
            if cl_sigs <= seen_sigs:  # 依赖闭包无新步骤
                degenerate.append(rs.id)
        return degenerate

    @staticmethod
    def _degenerate_feedback(degenerate: list[str]) -> str:
        """构造告知 LLM 修订被拒绝的原因（用于下一次 revise 的 feedback）。"""
        return (
            "\n[SYSTEM REJECTION] The previous revision repeats step(s) "
            + ", ".join(degenerate)
            + " with the same tool and input that already failed, and adds no new "
            "upstream step that could change the outcome. Re-running them cannot "
            "succeed. Remove or change these steps, or add a new upstream step."
        )

    async def _revise_with_degenerate_guard(
        self,
        run_id: str,
        plan: DagPlan,
        results: dict[str, StepResult],
        sys_state: str,
        feedback: str | None,
        intent_fallback: str,
        root_contracts: list[Any] | None = None,
        intent_raw: str = "",
        merge_context: tuple[DagPlan, DagPlan, dict[str, str]] | None = None,
    ) -> tuple[DagPlan | None, str | None]:
        """planner.revise 包装 E 阶段退化修订守卫（U1）+ S08 交付不变量守卫。

        每次 revise 后：
          - 退化守卫：签名比对（拒绝"重复已失败动作、依赖闭包无新步骤"的修订）；
          - S08 不变量：合并原始步骤后校验交付契约未被弱化（validate_revision_invariants）。
        任一项失败 → 拒绝并重试（上限 config.max_revise_retries）。

        Returns (revised_or_None, error_or_None)。error 非空表示重试预算已耗尽，
        调用方应以该消息 fail run。merge_context=(root_plan, current_plan, step_aliases)
        时在守卫内先做修订合并（合并后校验），调用方不再二次合并。
        """
        fb = feedback
        degenerate: list[str] = []
        invariant_errors: list[str] = []
        for attempt in range(self.config.max_revise_retries + 1):
            revised = await self._phase_call(
                run_id,
                "revise",
                self.planner.revise(
                    plan,
                    results,
                    sys_state,
                    feedback=fb,
                    intent_fallback=intent_fallback,
                    run_id=run_id,
                ),
            )
            if revised is None:
                return None, None
            degenerate = self._find_degenerate_revised_steps(plan, results, revised)

            # S08: 不变量校验在"合并后"的计划上判定。用深拷贝合并，避免重试循环中
            # 真实 results/step_aliases 被 _merge_revised_plan 变异（导致退化检测漂移）。
            invariant_errors = []
            if revised.steps and merge_context is not None and not degenerate:
                root_plan, current_plan, step_aliases = merge_context
                check_results = copy.deepcopy(results)
                check_aliases = dict(step_aliases)
                merged_check = self._merge_revised_plan(root_plan, current_plan, revised, check_results, check_aliases)
                invariant_errors = validate_revision_invariants(
                    list(root_contracts or ()),
                    intent_raw,
                    merged_check,
                    registry=self.planner.registry,
                )

            if not degenerate and not invariant_errors:
                return revised, None

            if degenerate:
                _sched_breaker.error(
                    "[breaker] Degenerate revision rejected (attempt %d/%d): step(s) %s "
                    "repeat a failed action (same tool+input, no new dep)",
                    attempt + 1,
                    self.config.max_revise_retries + 1,
                    ", ".join(degenerate),
                )
                fb = (fb or "") + self._degenerate_feedback(degenerate)
            if invariant_errors:
                _sched_breaker.error(
                    "[breaker] Revision invariant rejected (attempt %d/%d): %s",
                    attempt + 1,
                    self.config.max_revise_retries + 1,
                    "; ".join(invariant_errors),
                )
                fb = (fb or "") + revision_invariant_feedback(invariant_errors)
        _sched_breaker.error("[breaker] Degenerate revision budget exhausted for run=%s", run_id)
        return None, (
            f"Degenerate self-heal: step(s) {', '.join(degenerate)} kept repeating a "
            f"failed action (same tool+input, no new dependency) — not converging"
            + (f"; invariant violations: {'; '.join(invariant_errors)}" if invariant_errors else "")
        )

    @staticmethod
    def _merge_step_tasks(results: dict[str, StepResult], revised: DagPlan) -> int:
        """Merge the LLM's step_tasks annotations into results.

        v2.1 (Bug S1.1): purely observational. task_state does NOT affect any
        scheduling decision — should_not_rerun / step_normal are pure ExecState
        functions that never read it (AGENTS.md constraint 4).
        v2.2 (D11): task_state remains an audit-only note; it is persisted in
        PlanRevisedPayload for audit and future "LLM self-judgment vs system
        mechanical judgment" comparison. It NEVER participates in any trusted
        decision (constraint 4).

        Returns the number of annotations merged.
        """
        merged = 0
        if not revised.step_tasks:
            return 0
        for sid, ts_str in revised.step_tasks.items():
            if sid in results:
                try:
                    results[sid].task_state = TaskState(ts_str)
                    merged += 1
                except ValueError:
                    pass
        return merged

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

    async def _finalize_or_fail_verdict(
        self,
        run_id: str,
        intent: str,
        fallback_summary: str,
        verdict: CompletionVerdict,
        consecutive_failures: int = 0,
    ) -> tuple[RunState, int]:
        """S06: 完成门判定落点 — 绝不宣称交付达成（C-02 fail-safe）。

        机械不全 → fail；契约存在但未达成 → fail；无契约（unverified）或
        全部达成 → RunCompleted 携带显式 deliverable 标记（D-04）。
        """
        if not verdict.mechanical_complete:
            error_msg = f"Steps not achieved: {', '.join(verdict.unmet_step_ids)}"
            await self._fail(run_id, error_msg)
            return await self._refresh_state(run_id), consecutive_failures
        if verdict.deliverable_status == "failed":
            unmet_contracts = [v.contract_id for v in verdict.deliverables if v.status != "met"]
            await self._fail(run_id, f"Deliverable not met: {', '.join(unmet_contracts)}")
            return await self._refresh_state(run_id), consecutive_failures
        await self._finalize_with_summary(
            run_id, intent, fallback_summary, all_normal=True, unmet_step_ids=[], completion=verdict
        )
        return await self._refresh_state(run_id), consecutive_failures

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
