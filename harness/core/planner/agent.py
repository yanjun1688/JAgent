"""Planner (V0.7, L4) — generates and revises DAG Plans via LLM.

Non-trusted component. Plan output is validated by PlanGuardrail before
execution; repair proposals are mechanically re-validated by the Scheduler.
"""

from __future__ import annotations

import json
import time
from typing import Any

from harness.core.dag_types import StepResult
from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger, fmtkv
from harness.core.planner.guardrail import PlanGuardrail
from harness.core.planner.parsing import parse_local_repair_proposal, parse_plan_response
from harness.core.planner.prompts import (
    build_answer_user_content,
    build_feedback_section,
    build_plan_prompt,
    build_tool_descriptions,
    build_tool_descriptions_subset,
)
from harness.core.planner.schema_contract import build_step_schema_text, retry_prompt
from harness.core.recovery import LocalRepairProposal
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.plan import DagPlan, DagStep
from harness.storage.event_store import EventStore
from harness.tools.registry import ToolRegistry

_log = agent_logger("planner")


class Planner:
    """Generates and revises DAG Plans via LLM.

    Non-trusted component. Output is validated by PlanGuardrail before execution.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        store: EventStore | None = None,
        max_plan_retries: int = 2,
    ):
        self.llm = llm_client
        self.registry = registry
        self.store = store
        self.max_plan_retries = max_plan_retries
        self.guardrail = PlanGuardrail(registry, store)
        self.last_raw_response: str = ""

    async def _chat_structured(
        self,
        phase: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ):
        """S11 (问题八 观测侧): LLM 调用结构化日志 — run_id/phase/耗时/异常类型。"""
        _t0 = time.monotonic()
        try:
            resp = await self.llm.chat(messages, run_id=run_id, **kwargs)
            _log.info(
                "[llm] phase=%s run=%s duration_ms=%d chars=%d",
                phase,
                run_id or "",
                int((time.monotonic() - _t0) * 1000),
                len(resp.content) if resp and resp.content else 0,
            )
            return resp
        except Exception as exc:
            _log.error(
                "[llm] phase=%s run=%s duration_ms=%d error=%s:%s",
                phase,
                run_id or "",
                int((time.monotonic() - _t0) * 1000),
                type(exc).__name__,
                str(exc)[:200],
            )
            raise

    async def plan(
        self,
        intent: str,
        state: RunState | None = None,
        feedback: str | None = None,
        conversation_context: str = "",
        run_id: str | None = None,
    ) -> DagPlan | None:
        prompt = build_plan_prompt(
            self.registry,
            intent,
            feedback=feedback,
            conversation_context=conversation_context,
        )
        _log.info(
            "[plan] phase=%s len=%d %s",
            AgentPhase.PLAN.value,
            len(prompt),
            fmtkv(intent=intent[:80], feedback_len=len(feedback) if feedback else 0, has_feedback=feedback is not None),
        )
        last_error = ""

        for attempt in range(1, self.max_plan_retries + 2):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": retry_prompt(last_error)})

            _log.info("[plan] Attempt %d/%d for intent: %.80s", attempt, self.max_plan_retries + 1, intent)
            chat_resp = await self._chat_structured(AgentPhase.PLAN.value, run_id, messages, temperature=0.0)
            response = chat_resp.content
            _log.info(
                "[plan] LLM response (%d chars): %.200s%s",
                len(response),
                response,
                "..." if len(response) > 200 else "",
            )

            self.last_raw_response = response
            plan, last_error = parse_plan_response(response)
            if plan is None:
                _log.warning("[plan] Parse failed on attempt %d: %s", attempt, last_error)
                continue
            plan.user_intent = intent

            errors = self.guardrail.validate(plan)
            if errors:
                last_error = "; ".join(errors)
                _log.warning("[plan] Guardrail failed on attempt %d: %s", attempt, last_error)
                continue

            _log.info("[plan] Valid plan with %d steps", len(plan.steps))
            return plan

        _log.error("[plan] All %d attempts failed. Last error: %s", self.max_plan_retries + 1, last_error)
        return None

    async def revise(
        self,
        plan: DagPlan,
        results: dict[str, Any],
        system_state: str,
        feedback: str | None = None,
        intent_fallback: str = "",
        run_id: str | None = None,
    ) -> DagPlan | None:
        intent = plan.intent[:200] if plan.intent else (intent_fallback[:200] if intent_fallback else "(unknown)")
        user_intent = plan.user_intent[:200] if plan.user_intent else intent
        feedback_section = build_feedback_section(feedback)
        prompt = get_prompt(
            AgentPhase.REVISE,
            step_schema=build_step_schema_text(),
            user_intent=user_intent,
            intent=intent,
            system_state=system_state,
            tool_descriptions=build_tool_descriptions(self.registry),
            feedback_section=feedback_section or "",
        )
        _log.info(
            "[revise] phase=%s len=%d %s\n=== REVISE SYSTEM STATE ===\n%s\n=== END REVISE SYSTEM STATE ===",
            AgentPhase.REVISE.value,
            len(prompt),
            fmtkv(intent=intent[:80], has_feedback=feedback is not None, feedback_len=len(feedback) if feedback else 0),
            system_state,
        )
        total_attempts = self.max_plan_retries + 1
        # Steps that must NOT be re-run (tool already executed with a settled
        # outcome). UNSUCCESSFUL steps are excluded — they may be re-run.
        executed_step_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.should_not_rerun}
        # Steps whose recorded output is available for $var.field references in
        # a revised plan — includes UNSUCCESSFUL (output_available), whose error
        # text is exactly what a summary step needs to report.
        available_step_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.output_available}

        last_error = ""
        for attempt in range(1, total_attempts + 1):
            messages = [{"role": "system", "content": prompt}]
            if last_error:
                messages.append({"role": "user", "content": retry_prompt(last_error)})

            chat_resp = await self._chat_structured(AgentPhase.REVISE.value, run_id, messages, temperature=0.0)
            revised, last_error = parse_plan_response(chat_resp.content, executed_step_ids)

            if revised is None:
                _log.warning("[revise] Parse failed on attempt %d: %s", attempt, last_error)
                continue
            if not revised.user_intent:
                revised.user_intent = plan.user_intent

            if not revised.steps:
                _log.info("[revise] Attempt %d — task complete (empty steps)", attempt)
                return revised.model_copy(
                    update={
                        "steps": [],
                        "user_intent": plan.user_intent,
                        "declared_operations": list(plan.declared_operations),
                    }
                )

            errors = self.guardrail.validate(
                revised,
                completed_step_ids=executed_step_ids,
                available_step_ids=available_step_ids,
            )
            if errors:
                last_error = "; ".join(errors)
                _log.warning("[revise] Guardrail failed on attempt %d: %s", attempt, last_error)
                continue

            _log.info("[revise] Attempt %d — valid plan with %d steps", attempt, len(revised.steps))
            return revised

        _log.error("[revise] All %d attempts failed", total_attempts)
        return None

    async def propose_local_repair(
        self,
        step: DagStep,
        error: str | None,
        *,
        allowed_tools: list[str],
        run_id: str | None = None,
    ) -> LocalRepairProposal | None:
        """v3.4 (F-6): LLM 建议单个失败步骤的局部替代动作（非受信，可被否决）。

        只面向一个失败步骤产出 ``LocalRepairProposal``，工具被限制在
        ``allowed_tools``（受信 read-only 白名单）内；任何越权/mutating 建议
        由 Scheduler 的 ``validate_local_repair`` 机械拒绝。LLM 无法给出可修复
        建议（如环境不允许）时返回 ``None``，调度器随后升级到全局 revise。
        """
        descriptions = build_tool_descriptions_subset(self.registry, allowed_tools)
        prompt = get_prompt(
            AgentPhase.LOCAL_REPAIR,
            step_id=step.id,
            step_tool=step.tool,
            step_input=json.dumps(step.input or {}, ensure_ascii=False)[:500],
            step_description=(step.description or "").strip()[:200],
            step_error=(error or "unknown")[:400],
            tool_descriptions=descriptions,
        )
        _log.info(
            "[local_repair] phase=%s step=%s tool=%s error=%.160s",
            AgentPhase.LOCAL_REPAIR.value,
            step.id,
            step.tool,
            error or "?",
        )
        for attempt in range(1, self.max_plan_retries + 2):
            try:
                chat_resp = await self._chat_structured(
                    AgentPhase.LOCAL_REPAIR.value, run_id, [{"role": "system", "content": prompt}], temperature=0.0
                )
            except Exception:
                _log.exception("[local_repair] LLM call failed for step=%s", step.id)
                return None
            response = chat_resp.content or ""
            self.last_raw_response = response
            proposal = parse_local_repair_proposal(response)
            if proposal is not None:
                return proposal
            _log.warning(
                "[local_repair] Unparseable proposal on attempt %d for step=%s (%.200s)",
                attempt,
                step.id,
                response.strip(),
            )
        _log.warning(
            "[local_repair] No valid proposal after %d attempts for step=%s",
            self.max_plan_retries + 1,
            step.id,
        )
        return None

    async def generate_answer(
        self,
        intent: str,
        state: RunState,
        feedback: str | None,
        run_id: str | None = None,
        conversation_context: str = "",
    ) -> str:
        """Generate a conversational final answer when no tools are needed.

        All context (tool results, summary, feedback) is packed into a single
        user message so the LLM sees everything as content to answer, regardless
        of how different models handle multiple system messages.
        """
        prompt = get_prompt(AgentPhase.ANSWER)
        _log.info("[answer] phase=%s len=%d", AgentPhase.ANSWER.value, len(prompt))
        messages = [{"role": "system", "content": prompt}]

        n_tool_results = len(state.tool_results)
        user_content = build_answer_user_content(intent, state, conversation_context)
        messages.append({"role": "user", "content": user_content})
        total_chars = sum(len(m["content"]) for m in messages)
        _log.info(
            "[answer] Sending %d messages (%d tool_results, %d chars) to LLM",
            len(messages),
            n_tool_results,
            total_chars,
        )

        chat_resp = await self._chat_structured(
            AgentPhase.ANSWER.value,
            run_id,
            messages,
            temperature=0.7,
            max_tokens=16384,
        )
        _log.info(
            "[answer] LLM response: %d chars: %.200s%s",
            len(chat_resp.content),
            chat_resp.content,
            "..." if len(chat_resp.content) > 200 else "",
        )
        return chat_resp.content.strip()
