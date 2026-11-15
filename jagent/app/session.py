"""JAgent v8 · Session 与 turn 驱动。

Session 只做三件事：记住 turn 序列、槽位、阶梯计数。它不做判定、不写事件（通过 Writer）。
一个 turn 的流向严格按图：

    Router ─┬─ 模糊 → Clarify ─► Propose ─► 合规校验 ─┬─ 通过 ─► 输出建议
            │                              ▲          │
            │                              └── 打回重问 ┘
            └─ 确定 → Clarify ─► Planner ─► Executor …

「澄清后露出确定性意图」由下一次 Router 判定实现（每 turn 重路由），不是内部 while。
每个 turn 都有出口（answered / needs_input / handed_off），不存在挂在半路的 turn。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from .. import logsupport
from ..agent.delivery import DEFAULT_DISCLAIMER, fallback, render
from ..agent.planner import PlanningError
from ..agent.propose import ProposeError
from ..agent.router import AskRouter
from ..contracts import (
    Advice,
    AdviceDraft,
    Budget,
    Citation,
    ClarifyDraft,
    Delivery,
    EventType,
    EvidenceBundle,
    EvidenceItem,
    IntentDecision,
    IntentType,
    JudgeRecord,
    MemoryDraft,
    Question,
    Regime,
    ReplyDraft,
    Result,
    digest_of,
    plan_to_dict,
)
from ..evidence import missing
from ..gate.rules import AdviceGate, scan_reply
from ..grounding import CHECK_CITATIONS, CHECK_DIRECTIVES
from ..harness.fold import World
from ..harness.human import HumanCenter
from ..harness.plancheck import PlanIssue, first_message
from ..harness.writer import Writer
from .report_delivery import build_run_delivery, delivery_dict
from .runner import Runner

PATH_SLOT = "__path__"

_NO_MODEL_PLAN_TEXT = (
    "这台服务现在没接模型，我拆不了可执行的计划。"
    "配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_NAME 之后我再试。"
)

_NO_MODEL_ADVICE_TEXT = (
    "判断类的建议需要接模型才能给，这台服务现在没接模型。"
    "配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_NAME 之后我再试。"
)

#: 模型层故障（配额耗尽 / 网络 / 鉴权）：不是计划质量问题，要说清是环境问题。
_MODEL_OUTAGE_TEXT = "模型暂时不可用（配额、网络或鉴权），这次没能开始。稍后再试。"
#: U2 收尾（D-028）：取消的 run 没有结论可给，会话流以这条 reply 通知收尾。
_CANCEL_NOTICE = "任务已取消，未产出结论。"
#: D-030 L2：长期记忆确认/取消词（MVP 精确匹配；条目待确认时先于路由消费）。
_MEMORY_CONFIRM_WORDS = frozenset({"确认", "确定", "好", "要", "记下"})
_MEMORY_CANCEL_WORDS = frozenset({"取消", "不要", "不用", "算了"})
_MEMORY_SECTIONS = ("user", "feedback", "project", "reference")

#: 没有真正发生语义评审时追加到系统声明段的标记（T3 系统段，模型正文不写）。
_UNSEMANTIC_MARK = "（未经语义评审）"


@dataclass
class TurnOutcome:
    status: str                       # answered | needs_input | waiting_human
    text: str = ""
    question: Question | None = None
    run_id: str | None = None
    result: Result | None = None
    decision: IntentDecision | None = None
    error_code: str = ""
    #: 对外文本的分级与来源（宪法 v2 I3/I8）：API/UI 的气泡与徽标都读它。
    delivery: Delivery | None = None


class Session:
    def __init__(
        self,
        *,
        writer: Writer,
        router: Any,
        gate: AdviceGate,
        planner: Any,
        runner: Runner,
        human: HumanCenter,
        budget: Budget,
        workspace: Path,
        registry: Any = None,
        fallback_router: Any = None,
        session_id: str | None = None,
        artifacts: Any = None,
        propose: Any = None,
        root: Path | None = None,
        summarizer: Any = None,
    ) -> None:
        self.writer = writer
        self.router = router
        self.gate = gate
        self.planner = planner
        self.propose = propose
        self.runner = runner
        self.human = human
        self.budget = budget
        self.workspace = workspace
        self.registry = registry
        self.artifacts = artifacts
        #: run 收尾复述（D-042 / I8）：终态之后才调用，模型不进终态事务。
        self.summarizer = summarizer
        #: 应用数据根（D-030 L2）：memory.md 写在 `<root>/memory/`，不是任务
        #: workspace（那会被清空/替换）；None = 记忆落盘不可用（只起草不持久）。
        self.root = Path(root) if root is not None else None
        self.session_id = session_id or ("s_" + uuid.uuid4().hex[:8])
        self._pending: Question | None = None
        #: 因人工确认挂起的 turn 上下文（run_id -> decision/text）：恢复后还要合成建议。
        self._pending_turns: dict[str, tuple[IntentDecision, str]] = {}
        #: 上一轮被合规校验拦下的违规码。必须原样回传给 Propose，否则"打回重问"
        #: 只会得到和上一轮逐字相同的建议，三振后落兜底（红队/审计 G-03）。
        self._violations: tuple[Any, ...] = ()
        #: 用户在被拦后选择"只看框架"，本轮强制 framework_only。
        self._force_framework = False
        #: 模型路由挂了（confidence=0 的 fallback）就退回确定性兜底：不猜意图，
        #: 直接问用户「要建议还是要执行」。降级要在事件的 reason 里留痕。
        self.fallback_router = fallback_router or AskRouter()
        #: planner 问过的动态槽位名。用户的回答只允许写进这些槽
        #: （RT-H2 的防污染性质：白名单由 planner 现场声明，不由静态词表守）。
        self._dynamic_slots: set[str] = set()

    @property
    def _draft_capable(self) -> bool:
        """planner 是否支持 clarify/reply/advice 三种草稿模式（即 LLMPlanner）。"""
        return bool(getattr(self.planner, "draft_capable", False))

    @property
    def world(self) -> World:
        return self.writer.world

    @property
    def state(self):
        return self.world.session(self.session_id)

    # ------------------------------------------------------------------ turn --
    def handle_turn(self, text: str, answers: dict[str, str] | None = None) -> TurnOutcome:
        answers = dict(answers or {})
        # TTL 过期的挂起 run 先收走（BUG2-08 余项）：确定性扫描，无后台线程。
        self._reap_expired_approvals()
        # 输入上限：事件日志是持久化的，不能让一次请求把它撑大（红队 RT-H1）。
        text = text[:2000]
        self.writer.emit(EventType.TURN_RECEIVED, {"text_hash": digest_of(text), "text": text})

        # D-030 L2：有待确认的记忆条目时，先看用户是不是在确认/取消——
        # 这两个词不当普通输入路由（M6-A：起草-确认流）。
        pending = self.state.pending_memory
        if pending:
            verdict = text.strip()
            if verdict in _MEMORY_CONFIRM_WORDS:
                return self._confirm_memory(pending)
            if verdict in _MEMORY_CANCEL_WORDS:
                return self._cancel_memory()

        path_answer = answers.pop(PATH_SLOT, None)
        # 被拦后的回答不是槽位，而是"要不要降级成只看框架"。若把它当未知槽位丢掉，
        # 打回重问就等于空转（审计 G-03）。
        violation_answer = answers.pop("__violation__", None)
        if violation_answer is not None:
            self._force_framework = "框架" in str(violation_answer)
        # 未知槽位名一律忽略，否则可以跨 turn 污染槽位账本（红队 RT-H2）。
        # 白名单 = planner 本会话澄清时问过的动态槽位（域知识不写死在词表里）
        # ∪ 事件日志里持久化的历史白名单（BUG2-04：重启后内存是空的）。
        allowed = self._dynamic_slots | self.state.dynamic_slots
        for name, value in answers.items():
            if name not in allowed:
                continue
            self.writer.emit(EventType.SLOT_UPDATED, {"name": name, "value": value, "source": "user"})

        # Router：每个 turn 都跑（长对话里意图会漂移）
        if path_answer is not None:
            intent = IntentType.ADVISE if "建议" in path_answer else IntentType.EXECUTE
            decision = IntentDecision(
                type=intent, confidence=0.9, missing_params=(), reason="path_choice",
            )
            self.writer.emit(EventType.INTENT_CONFIRMED, {"type": intent.value, "via": "path_choice"})
        else:
            decision = self.router.route(
                text=text,
                slots=self.state.slots,
                turns=self._history_lines(),
                memory=self._memory_lines(),
            )
            # 模型路由失败（返回 confidence=0 的 fallback）时退回规则路由：
            # 把"跑测试"判成建议，比承认模型挂了更糟。
            if decision.confidence <= 0.0 and "fallback" in (decision.reason or ""):
                rule_decision = self.fallback_router.route(
                    text=text,
                    slots=self.state.slots,
                    turns=self._history_lines(),
                    memory=self._memory_lines(),
                )
                decision = replace(
                    rule_decision, reason=f"router_degraded_to_rules:{decision.reason}"
                )

        self.writer.emit(
            EventType.INTENT_ROUTED,
            {
                "type": decision.type.value,
                "confidence": decision.confidence,
                "missing_params": list(decision.missing_params),
                "single_step": decision.single_step,
                "phase": "initial",
                "reason": decision.reason,
            },
        )
        logsupport.event(
            "route", session=self.session_id, intent=decision.type.value,
            single_step=decision.single_step, conf=decision.confidence,
            reason=decision.reason, missing=list(decision.missing_params),
        )

        # 置信度不够：先问「要建议还是要执行」，不猜。
        # 这道闸只服务降级装配（UnavailablePlanner）：draft_capable 的 planner
        # 自己用 mode 决定路径（steps/clarify/advice/reply 本来就是路径），
        # 再问一遍只会白白多一轮。
        if (
            path_answer is None
            and decision.needs_path_choice
            and not self._draft_capable
        ):
            question = self._ask_path_choice()
            return self._needs_input(question, decision)

        if decision.type is IntentType.ADVISE and not self._draft_capable:
            # 降级装配没有建议能力（模板建议层已删）：诚实说明，而不是给模板话术。
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
            return TurnOutcome(
                status="answered",
                text=_NO_MODEL_ADVICE_TEXT,
                decision=decision,
            )
        return self._execute(decision, text)

    # -------------------------------------------------------------- 执行路径 --
    def _execute(self, decision: IntentDecision, text: str) -> TurnOutcome:
        tools = self.registry.descriptors() if self.registry is not None else []
        if not self._draft_capable:
            try:
                draft = self.planner.plan(
                    decision=decision, slots=self.state.slots, tools=tools, text=text,
                    turns=self._history_lines(), memory=self._memory_lines(),
                )
            except Exception as exc:  # 规划失败要给用户一句人话，不能抛 500
                return self._planning_failure(exc, decision, attempt=1)
            attempt = 1
        else:
            draft, attempt, failure = self._plan_with_feedback(decision, text, tools)
            if failure is not None:
                return failure
            assert draft is not None

        # planner 的草稿模式：澄清 / 直接回答 / 建议 / 长期记忆起草。计划本身走下面的原流程。
        if isinstance(draft, ClarifyDraft):
            return self._clarify_draft(draft, decision)
        if isinstance(draft, ReplyDraft):
            return self._reply_draft(draft, decision)
        if isinstance(draft, AdviceDraft):
            return self._advice_draft(draft, decision)
        if isinstance(draft, MemoryDraft):
            return self._memory_draft(draft, decision, text)

        plan = draft
        self.writer.emit(
            EventType.PLAN_PROPOSED,
            {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "attempt": attempt},
        )

        # E19：空计划没有取数路径，直接 Propose（不建 run；mode 由 gate 判 framework）。
        trigger = self._e19_trigger(decision, plan)
        if trigger is not None and not plan.steps:
            synthesized = self._compose_advice(
                decision, text, plan, run_id=None, trigger=trigger
            )
            if synthesized is not None:
                return synthesized
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
            return TurnOutcome(status="answered", text=_NO_MODEL_ADVICE_TEXT, decision=decision)

        outcome = self.runner.start_and_run(plan)
        if outcome.status == "waiting_human":
            # 挂起不合成；resume 之后再用同一份请求上下文收尾（E19 / D-002）。
            self._pending_turns[outcome.run_id] = (decision, text)
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "handed_off"})
            return TurnOutcome(status="waiting_human", run_id=outcome.run_id, decision=decision)
        if outcome.status == "cancelled":
            # 边界取消消费完毕（D-028）：turn 以 reply 通知收尾（U2）——
            # 不合成、不兜底，Propose 不得因取消被调用。
            self._emit_cancel_notice()
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
            return TurnOutcome(
                status="answered", run_id=outcome.run_id, text=_CANCEL_NOTICE, decision=decision,
            )

        # E19（D-002 / D-022）：触发以意图为准（GATHER）或显式 `then="advice"`，
        # 且 run 终态 + 所有步 settled -> 证据回 Propose。普通执行仍走"模型总结"。
        if trigger is not None and self._e19_eligible(outcome):
            synthesized = self._compose_advice(
                decision, text, plan, run_id=outcome.run_id, trigger=trigger
            )
            if synthesized is not None:
                # 合成只替"文本"，不替事实：run 的判定/ID 必须留在 TurnOutcome 上，
                # 否则真实模型下 API 的 body["result"] 会消失（在线链路回归）。
                return replace(synthesized, run_id=outcome.run_id, result=outcome.result)
            # E19 已尝试但合成失败（Propose 不可用 / 输出不合法）：**不得**回落到
            # `_answer_from_evidence` -> `Planner.plan` 的总结路径（D-004：gather
            # 的收尾只能走 Propose）。直接按普通执行结果收尾。
            return self._handed_off(decision, outcome)

        # run 到终态 → 收尾投递（输出分级 R2，D-042）：终态事件已经落完，模型在这里才参与。
        # 复述通过接地检查就带 regime 投递，不合格降级为模板文本（仍 verified）。
        if outcome.result is not None and outcome.status in ("completed", "failed"):
            return self._report_delivery(decision, outcome)

        return self._handed_off(decision, outcome)

    def _handed_off(self, decision: IntentDecision | None, outcome: Any) -> TurnOutcome:
        """普通收尾：只回 run 的结果摘要（模板文本，verified），不调模型。

        E19 合成失败时用它收尾，避免 gather 回落到 ``Planner.plan``（D-004）。
        """

        result = getattr(outcome, "result", None)
        text = result.summary if result is not None else ""
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "handed_off"})
        return TurnOutcome(
            status="answered",
            text=text,
            run_id=outcome.run_id,
            result=result,
            decision=decision,
            delivery=Delivery(text=text, regime=Regime.VERIFIED) if text else None,
        )

    def _report_delivery(self, decision: IntentDecision | None, outcome: Any) -> TurnOutcome:
        """run 终态之后的人话投递（R2）：草稿 + 接地检查 + regime，落一条事件。

        模型在这里才被调用（I8：不进终态事务）。草稿不合格时投递模板摘要（verified），
        ``fallback{bool,reason}`` 说明原因；两种情况都不会改写 run 的判定（Result 只读）。
        """

        run = self.world.runs.get(outcome.run_id or "")
        delivery = build_run_delivery(
            run, outcome.result, summarizer=self.summarizer, budget=self.budget
        )
        artifact_id = self._store_delivery_text(delivery.text)
        delivery = replace(delivery, artifact_id=artifact_id)
        self.writer.emit(
            EventType.ADVICE_DELIVERED,
            {
                "advice": {"body": delivery.text},
                "mode": "report",
                "delivery": delivery_dict(delivery),
                "artifact_id": artifact_id,
            },
        )
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
        return TurnOutcome(
            status="answered",
            text=delivery.text,
            run_id=outcome.run_id,
            result=outcome.result,
            decision=decision,
            delivery=delivery,
        )

    # -------------------------------------------------- planner 草稿三种模式 --
    def _plan_with_feedback(
        self, decision: IntentDecision, text: str, tools: list[dict[str, Any]]
    ) -> tuple[Any | None, int, TurnOutcome | None]:
        """草稿循环在 Session：每拒一稿落 plan.rejected，并把 issues 喂回 Planner。"""
        issues: list[PlanIssue] = []
        attempts = max(1, int(self.budget.max_plan_attempts))
        for attempt in range(1, attempts + 1):
            try:
                draft = self.planner.plan(
                    decision=decision,
                    slots=self.state.slots,
                    tools=tools,
                    text=text,
                    feedback=issues,
                    attempt=attempt,
                    turns=self._history_lines(),
                    memory=self._memory_lines(),
                )
                return draft, attempt, None
            except Exception as exc:
                if _is_outage(exc):
                    # 模型层不可用不是草稿质量问题（D-012）：不重试、不落 plan.rejected、
                    # 不计 plan_rejects；turn 的出口用独立 outcome 说清楚。
                    self.writer.emit(EventType.TURN_ENDED, {"outcome": "model_unavailable"})
                    return None, attempt, TurnOutcome(
                        status="answered",
                        text=_MODEL_OUTAGE_TEXT,
                        decision=decision,
                        error_code="model_unavailable",
                    )
                if not _is_planning_failure(exc):
                    raise
                issues = self._issues_of(exc)
                self._emit_plan_rejected(exc, issues, attempt)
                if attempt >= attempts:
                    self.writer.emit(EventType.TURN_ENDED, {"outcome": "plan_rejected"})
                    return None, attempt, TurnOutcome(
                        status="answered",
                        text=self._plan_rejected_text(issues, attempts),
                        decision=decision,
                        error_code="plan_rejected",
                    )
        raise AssertionError("unreachable: plan loop must return or raise")

    def _planning_failure(
        self, exc: Exception, decision: IntentDecision, attempt: int
    ) -> TurnOutcome:
        """非 draft-capable 装配（没接模型）的失败路径：不重试，诚实说明边界。"""
        if _is_outage(exc):
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "model_unavailable"})
            return TurnOutcome(
                status="answered",
                text=_MODEL_OUTAGE_TEXT,
                decision=decision,
                error_code="model_unavailable",
            )
        if not _is_planning_failure(exc):
            raise exc
        self._emit_plan_rejected(
            exc, [PlanIssue("PLANNER_FAILED", "", str(exc)[:300])], attempt
        )
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
        return TurnOutcome(status="answered", text=_NO_MODEL_PLAN_TEXT, decision=decision)

    def _issues_of(self, exc: Exception) -> list[PlanIssue]:
        issues = list(getattr(exc, "issues", ()) or ())
        return issues or [PlanIssue("PLANNER_FAILED", "", str(exc)[:300])]

    def _emit_plan_rejected(
        self, exc: Exception, issues: list[PlanIssue], attempt: int
    ) -> None:
        raw = str(getattr(exc, "raw", "") or "")
        artifact_id = ""
        if raw and self.artifacts is not None:
            artifact_id = self.artifacts.put_text(raw).artifact_id
        plan = getattr(exc, "plan", None)
        self.writer.emit(
            EventType.PLAN_REJECTED,
            {
                "attempt": attempt,
                "plan_hash": plan.hash() if plan is not None else "",
                "issues": [issue.as_dict() for issue in issues],
                "summary": first_message(issues) if issues else type(exc).__name__,
                "draft_artifact_id": artifact_id,
            },
        )

    def _plan_rejected_text(self, issues: list[PlanIssue], attempts: int) -> str:
        head = first_message(issues) if issues else "草稿不合格"
        return (
            f"这个计划我试了 {attempts} 次都没过自己的检查（{head}）。"
            "下一步：把目标拆成更小、每步都有可验证完成态的动作；或者告诉我第一步先做什么。"
        )

    # ------------------------------------------------------- E19 证据合成 --
    def _e19_trigger(self, decision: IntentDecision | None, plan: Any) -> str | None:
        """E19 的唯一触发判据（D-022）：路由意图为 ``GATHER``，或显式 ``then="advice"``。

        ``then`` 只是补充声明，不是"要不要过建议闸门"的开关：gather 意图必须收尾，
        模型少写一个字段不能把它静默降级成执行交付。返回写进 ``gate.checked.trigger``
        的取值（``"intent"`` / ``"then"``），不触发时为 ``None``。
        恢复路径可能拿不到本轮 decision（进程重启后上下文丢失）：此时只剩 ``then``
        可用，与既有"上下文丢了就从事件日志取原文"的降级保持一致。
        """

        if decision is not None and decision.type is IntentType.GATHER:
            return "intent"
        if getattr(plan, "then", None) == "advice":
            return "then"
        return None

    def _e19_eligible(self, outcome: Any) -> bool:
        """E19 触发条件（D-002）：run 终态且所有步 settled，非挂起/取消/恢复中。

        取消（run.cancelled）延期到 P1（D-020）：这里不实现取消分支，只要求
        run 已到 ``completed`` / ``failed``；等待人工或仍在跑的 run 不合成。
        """
        run = self.world.runs.get(getattr(outcome, "run_id", ""))
        if run is None:
            return False
        if run.status not in ("completed", "failed"):
            return False
        if run.approval:
            return False
        # 只有 passed / failed 算 settled：failed run 的下游 pending 步意味着
        # 计划并没有走完，此时合成会把"没执行的步"当成"没证据"（C4-01）。
        return all(
            state.status in ("passed", "failed") for state in run.steps.values()
        )

    def _compose_advice(
        self,
        decision: IntentDecision | None,
        text: str,
        plan: Any,
        *,
        run_id: str | None,
        trigger: str,
    ) -> TurnOutcome | None:
        """E19 收尾：``Propose.compose`` -> 回填 seq -> gate -> 投递/兜底。

        - ``missing`` 只从 ``jagent.evidence.missing`` 取（不在 Session 重算）；
        - ``mode`` / ``partial`` 只复制 gate 的结论（不在 Session 重算）；
        - ``trigger`` 是 ``intent`` / ``then``，原样写进 ``gate.checked``（D-022）；
        - 合成失败（模型不可用 / 输出不合法）返回 ``None``，由调用方走普通收尾。
        """
        if self.propose is None:
            return None
        evidence = (
            self._evidence_bundle(run_id, plan)
            if run_id
            else EvidenceBundle(run_id="", items=())
        )
        unmet = missing(plan, evidence)
        try:
            advice = self.propose.compose(
                slots=self.state.slots,
                text=text or "",
                evidence=evidence,
                missing=unmet,
                then=plan.then,
            )
        except Exception as exc:  # noqa: BLE001 - 合成失败不推翻已完成的 run
            if _is_outage(exc) or isinstance(exc, ProposeError):
                return None
            raise

        advice = self.backfill_citation_seqs(advice, run_id)
        verdict = self.gate.check(advice, evidence, plan)
        unreviewed = _unreviewed(verdict.judge)
        verdict, advice = self._review_state(verdict, advice)
        self._log_gate(verdict, run_id=run_id)
        self.writer.emit(
            EventType.GATE_CHECKED,
            self._gate_payload(
                verdict,
                advice_hash=digest_of(_advice_dict(advice)),
                trigger=trigger,
            ),
        )

        if verdict.passed:
            delivered = advice
        else:
            # 兜底模板也是"给用户看的建议"：评审没真正发生时同样要标未经语义评审。
            delivered = fallback(verdict.violations)
            if unreviewed:
                delivered = _mark_unsemantic(delivered)
        if verdict.partial:
            # #16：这一行由系统生成（不交给模型），写在投递前的系统声明段里。
            delivered = _append_system_line(
                delivered, f"{self._failed_step_count(run_id, evidence)} 步未成功"
            )

        self.writer.emit(
            EventType.ADVICE_DELIVERED,
            {
                "advice": _advice_dict(delivered),
                "mode": verdict.mode,
                "partial": verdict.partial,
                "fallback": _fallback_dict(verdict.fallback),
                "artifact_id": self._store_delivery_text(render(delivered)),
                "delivery": delivery_dict(_advice_delivery(render(delivered), verdict)),
                "evidence": {
                    "run_id": evidence.run_id,
                    "citations": [_citation_dict(c) for c in verdict.citations],
                },
            },
        )
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
        return TurnOutcome(
            status="answered",
            text=render(delivered),
            run_id=run_id,
            decision=decision,
        )

    def _failed_step_count(self, run_id: str | None, evidence: EvidenceBundle) -> int:
        """系统行"N 步未成功"里的 N：优先数 run 的失败步（不受证据条数上限影响）。"""
        if run_id:
            run = self.world.runs.get(run_id)
            if run is not None:
                return sum(1 for state in run.steps.values() if state.status == "failed")
        return sum(1 for item in evidence.items if item.status != "ok")

    def _evidence_bundle(self, run_id: str, plan: Any) -> EvidenceBundle:
        """从 ``step.finished`` 事件构造 ``EvidenceBundle``（失败步也带上）。

        同一 step 只保留最后一次执行；按计划的拓扑序排列，超过
        ``budget.evidence_max_steps`` 步时保留尾部，省掉的条数写进
        ``omitted_steps``（进 Propose 的 prompt）。
        """
        latest: dict[str, Any] = {}
        for event in self.writer.store.read(run_id=run_id):
            if event.type is not EventType.STEP_FINISHED:
                continue
            step_id = event.payload.get("step_id")
            if isinstance(step_id, str):
                latest[step_id] = event
        items = [
            self._evidence_item(latest[step.id])
            for step in plan.order()
            if step.id in latest
        ]
        limit = self.budget.evidence_max_steps
        omitted = max(0, len(items) - limit)
        kept = items[-limit:] if limit else []
        return EvidenceBundle(run_id=run_id, items=tuple(kept), omitted_steps=omitted)

    def _evidence_item(self, event: Any) -> EvidenceItem:
        clip = self.budget.evidence_clip_chars
        observations: dict[str, str] = {}
        truncated: list[str] = []
        for key, value in (event.payload.get("observations") or {}).items():
            name = str(key)
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str
            )
            if len(text) > clip:
                truncated.append(name)
            observations[name] = text[:clip]
        return EvidenceItem(
            step_id=event.payload.get("step_id", ""),
            seq=event.seq,
            status=event.payload.get("status", ""),
            observations=observations,
            truncated=tuple(truncated),
            artifact_ids=tuple(event.payload.get("artifact_refs") or ()),
        )

    # ------------------------------------------------------- 引用序号回填 --
    def _seq_index(
        self, run_id: str
    ) -> tuple[dict[str, tuple[int, dict[str, Any]]], dict[tuple[str, str], int]]:
        """``(step_id -> (seq, observations))`` 与 ``(step_id, artifact_id) -> seq``。

        同一 step 多次执行时取**最后一次** ``step.finished``：引用要指向最终观测，
        而不是被重试覆盖掉的那一次。工件的序号来自 ``artifact.written``。
        """
        steps: dict[str, tuple[int, dict[str, Any]]] = {}
        artifacts: dict[tuple[str, str], int] = {}
        for event in self.writer.store.read(run_id=run_id):
            if event.type is EventType.STEP_FINISHED:
                step_id = event.payload.get("step_id")
                if isinstance(step_id, str):
                    steps[step_id] = (
                        event.seq,
                        dict(event.payload.get("observations") or {}),
                    )
            elif event.type is EventType.ARTIFACT_WRITTEN:
                artifact_id = event.payload.get("artifact_id")
                step_id = event.step_id or event.payload.get("step_id")
                if isinstance(artifact_id, str) and isinstance(step_id, str):
                    artifacts[(step_id, artifact_id)] = event.seq
        return steps, artifacts

    def backfill_citation_seqs(self, advice: Advice, run_id: str | None) -> Advice:
        """按 ``(step_id, field)`` 从 ``step.finished`` / artifact 回填 ``Citation.seq``。

        模型输出的 ``seq`` **一律丢弃**（模型看不到事件序号，给了就能伪造）；
        解析不到时保持 ``None``，交给 gate 的 ``citation_unresolved`` 判。
        """
        if not advice.refs:
            return advice
        steps, artifacts = self._seq_index(run_id) if run_id else ({}, {})
        refs: list[Citation] = []
        for citation in advice.refs:
            seq: int | None = None
            found = steps.get(citation.step_id)
            if found is not None and citation.field in found[1]:
                seq = found[0]
            elif (citation.step_id, citation.field) in artifacts:
                seq = artifacts[(citation.step_id, citation.field)]
            refs.append(replace(citation, seq=seq))
        return replace(advice, refs=tuple(refs))

    def _evidence_of(self, run_id: str | None) -> dict[str, Any]:
        if not run_id:
            return {"steps": []}
        clip = self.budget.evidence_clip_chars
        steps: list[dict[str, Any]] = []
        for event in self.writer.store.read(run_id=run_id):
            if event.type is not EventType.STEP_FINISHED:
                continue
            observations: dict[str, str] = {}
            for key, value in (event.payload.get("observations") or {}).items():
                text = value if isinstance(value, str) else json.dumps(
                    value, ensure_ascii=False, default=str
                )
                observations[key] = text[:clip]
            steps.append(
                {
                    "step_id": event.payload.get("step_id"),
                    "status": event.payload.get("status"),
                    "observations": observations,
                }
            )
        limit = self.budget.evidence_max_steps
        return {
            "run_id": run_id,
            "steps": steps[-limit:] if limit else [],
        }

    def _clarify_draft(self, draft: ClarifyDraft, decision: IntentDecision) -> TurnOutcome:
        if self.state.clarify_round >= self.budget.max_clarify_rounds:
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
            return TurnOutcome(
                status="answered",
                text="这个请求我还需要更多信息才能动手，但我们已经澄清了几轮还没有对齐。"
                     "换个更具体的说法（要查什么、写到哪、哪一步先做），我再试一次。",
                decision=decision,
            )
        questions = list(draft.questions)[:3]
        # 记下 planner 问过的槽位名：下一轮用户的回答才允许写进这些槽。
        self._dynamic_slots.update(question.slot for question in questions)
        question = self._ask(questions, kind="planner_clarify")
        return self._needs_input(question, decision)

    def _reply_draft(self, draft: ReplyDraft, decision: IntentDecision) -> TurnOutcome:
        violations = scan_reply(draft.text)
        passed = not violations
        self.writer.emit(
            EventType.GATE_CHECKED,
            {
                "profile": "reply",
                "pass": passed,
                "violations": [
                    {"code": v.code, "rule_id": v.rule_id, "detail": v.detail} for v in violations
                ],
                "ruleset": "reply.v8.1",
                "text_hash": digest_of(draft.text),
            },
        )
        if passed:
            self.writer.emit(
                EventType.ADVICE_DELIVERED,
                {
                    "advice": {"body": draft.text},
                    "mode": "reply",
                    "artifact_id": self._store_delivery_text(draft.text),
                    # R3：没有证据包、没有判据 → 只标注，不当通过（I3）。
                    "delivery": delivery_dict(
                        Delivery(
                            text=draft.text,
                            regime=Regime.UNVERIFIED,
                            checks=(CHECK_DIRECTIVES,),
                            fallback={"bool": False, "reason": ""},
                        )
                    ),
                },
            )
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
            return TurnOutcome(
                status="answered", text=draft.text, decision=decision,
                delivery=Delivery(text=draft.text, regime=Regime.UNVERIFIED,
                                  checks=(CHECK_DIRECTIVES,)),
            )
        # 命中指令性表述/无来源数字声明：落兜底文本，不让合规风险绕过闸门。
        safe = fallback(violations)
        text = render(safe)
        delivery = Delivery(
            text=text,
            regime=Regime.VERIFIED,
            fallback={"bool": True, "reason": violations[0].code if violations else "reply_blocked"},
        )
        self.writer.emit(
            EventType.ADVICE_DELIVERED,
            {
                "advice": _advice_dict(safe),
                "mode": "reply_fallback",
                "artifact_id": self._store_delivery_text(text),
                "delivery": delivery_dict(delivery),
            },
        )
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
        return TurnOutcome(status="answered", text=text, decision=decision, delivery=delivery)

    def _advice_draft(self, draft: AdviceDraft, decision: IntentDecision) -> TurnOutcome:
        verdict = self.gate.check(draft.advice, framework_only=False)
        unreviewed = _unreviewed(verdict.judge)
        self._log_gate(verdict, session_only=True)
        verdict, advice = self._review_state(verdict, draft.advice)
        self.writer.emit(
            EventType.GATE_CHECKED,
            self._gate_payload(verdict, advice_hash=digest_of(_advice_dict(advice))),
        )
        if verdict.passed:
            text = render(advice)
            delivery = _advice_delivery(text, verdict)
            self.writer.emit(
                EventType.ADVICE_DELIVERED,
                {
                    "advice": _advice_dict(advice),
                    "mode": "gated",
                    "partial": verdict.partial,
                    "fallback": _fallback_dict(verdict.fallback),
                    "judge": _judge_dict(verdict.judge),
                    "artifact_id": self._store_delivery_text(text),
                    "delivery": delivery_dict(delivery),
                },
            )
            self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
            return TurnOutcome(status="answered", text=text, decision=decision, delivery=delivery)
        # 不过闸：一次裁决，直接落兜底（fail-closed；内容质量的重试预算已由 planner 内部用掉）。
        # 兜底模板也是投递给用户的建议：评审没真正发生时同样要标未经语义评审。
        safe = fallback(verdict.violations)
        if unreviewed:
            safe = _mark_unsemantic(safe)
        text = render(safe)
        delivery = _advice_delivery(text, verdict)
        self.writer.emit(
            EventType.ADVICE_DELIVERED,
            {
                "advice": _advice_dict(safe),
                "mode": "fallback",
                "partial": verdict.partial,
                "fallback": _fallback_dict(verdict.fallback),
                "judge": _judge_dict(verdict.judge),
                "artifact_id": self._store_delivery_text(text),
                "delivery": delivery_dict(delivery),
            },
        )
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
        return TurnOutcome(status="answered", text=text, decision=decision, delivery=delivery)

    # ------------------------------------------------------------ 评审记录 --
    def _review_state(self, verdict: Any, advice: Advice) -> tuple[Any, Advice]:
        """没真正发生语义评审时，给投递文本加系统段标记。

        gate 是只读组件（不在这里改 gate 的判定）：``none``（没配 judge）与
        ``unavailable``（配了但调不通）都表示评审没有发生，投递文本由系统声明段
        标注 ``（未经语义评审）``；``undecidable`` / ``reject`` 是真实评审结论，
        不标。judge 缺席不等于通过。``judge is None`` 只出现在 T1 已失败、judge
        未被问到的情形（或非 conforming gate）：同样没有评审，按未评审标注。
        """
        if _unreviewed(verdict.judge):
            return verdict, _mark_unsemantic(advice)
        return verdict, advice

    def _log_gate(self, verdict: Any, *, run_id: str | None = None, session_only: bool = False) -> None:
        """gate 结论进日志：通过一行，违规逐条（为什么被拦一目了然）。"""
        fields: dict[str, Any] = {"mode": verdict.mode}
        if run_id:
            fields["run"] = run_id
        if verdict.passed:
            if verdict.judge is not None:
                fields["judge"] = f"{verdict.judge.verdict}:{verdict.judge.reason}"
            logsupport.event("gate pass", session=self.session_id, **fields)
            return
        for violation in verdict.violations:
            logsupport.warn(
                "gate violation", session=self.session_id,
                code=violation.code, rule=violation.rule_id, detail=violation.detail,
                **({"run": run_id} if run_id else {}),
            )
        judge = verdict.judge
        if judge is not None and judge.verdict != "none":
            logsupport.warn(
                "judge verdict", session=self.session_id,
                verdict=judge.verdict, reason=judge.reason, model=judge.model,
                **({"run": run_id} if run_id else {}),
            )
        if session_only:
            logsupport.event(
                "advice fallback delivered", session=self.session_id,
                fallback=verdict.fallback.reason if verdict.fallback else "-",
            )

    def _gate_payload(
        self,
        verdict: Any,
        *,
        advice_hash: str = "",
        trigger: str | None = None,
    ) -> dict[str, Any]:
        """``gate.checked`` 的公共载荷：只复制 gate 的结论，不在这里重算。

        ``trigger`` 只由 E19 的 ``_compose_advice`` 传入（``intent`` / ``then``）；
        其它路径不写这个键，事件里就不会出现"没有触发的触发方式"。
        """
        payload: dict[str, Any] = {
            "profile": verdict.profile,
            "pass": verdict.passed,
            "violations": [
                {"code": v.code, "rule_id": v.rule_id, "detail": v.detail}
                for v in verdict.violations
            ],
            "ruleset": verdict.ruleset,
            "mode": verdict.mode,
            "partial": verdict.partial,
            "fallback": _fallback_dict(verdict.fallback),
            "citations": [_citation_dict(c) for c in verdict.citations],
            "judge": _judge_dict(verdict.judge),
        }
        if advice_hash:
            payload["advice_hash"] = advice_hash
        if trigger is not None:
            payload["trigger"] = trigger
        return payload

    # ------------------------------------------------------------------ 人工 --
    def resume_run(self, run_id: str, decision: str = "approve", *, comment: str = "") -> TurnOutcome:
        run = self.world.runs[run_id]
        if run.status in ("completed", "failed", "cancelled"):
            # 终态 run 不可 resume：取消后旧 token 的 approve 必须被拒（D-028）。
            raise RuntimeError(f"run {run_id} is already {run.status}")
        if not run.approval:
            raise RuntimeError(f"run {run_id} has no pending approval")
        context = self._pending_turns.pop(run_id, None)
        token = run.approval["token"]
        step_id = run.approval["step_id"]
        ok = self.human.resolve(run_id, token, decision, comment=comment)
        if not ok:
            return TurnOutcome(status="waiting_human", run_id=run_id)

        # 拒绝：这一步不做，整个 run 就此结束。不能让它回到"再问一次确认"的死循环。
        if decision == "reject":
            state = run.steps.get(step_id)
            if state is not None and state.status == "running":
                # in-doubt 的步骤停在 running：先作废（running→stale→pending），再跳过。
                # 直接 pending→skipped 不是合法转移，硬跳会写一条 illegal 事件然后抛。
                self.writer.emit(
                    EventType.STEP_INVALIDATED,
                    {"step_id": step_id, "reason": "rejected_in_doubt"},
                    run_id=run_id,
                    step_id=step_id,
                )
                self.writer.step_to(run_id, step_id, "pending", "rejected_in_doubt")
            self.writer.step_to(run_id, step_id, "skipped", "rejected_by_human")
            self.writer.emit(
                EventType.RUN_FAILED, {"reason": "rejected_by_human"}, run_id=run_id
            )
            return TurnOutcome(
                status="answered",
                run_id=run_id,
                text="好的，这一步我跳过了，这次任务就停在这里。需要我做别的事随时说。",
            )

        approved = frozenset({step_id})
        outcome = self.runner.resume(run_id, approved_steps=approved)
        if outcome.status == "waiting_human":
            if context is not None:
                self._pending_turns[run_id] = context
            return TurnOutcome(status="waiting_human", run_id=run_id, result=outcome.result)
        if outcome.status == "cancelled":
            # 审批/恢复窗口里被取消（D-028）：resume 的一致收尾——通知、不合成。
            self._emit_cancel_notice()
            return TurnOutcome(status="answered", run_id=run_id, text=_CANCEL_NOTICE)

        # E19：挂起不合成，恢复后到终态再合成（D-002 / D-022）。上下文丢了就从事件日志取原文；
        # decision 也丢了时只剩 `then` 可用（与原文降级的边界一致）。
        plan = self.world.runs[run_id].plan
        if plan is not None and self._e19_eligible(outcome):
            decision_obj, text = (
                context if context is not None else (None, self._last_turn_text())
            )
            trigger = self._e19_trigger(decision_obj, plan)
            if trigger is not None:
                composed = self._compose_advice(
                    decision_obj, text, plan, run_id=run_id, trigger=trigger
                )
                if composed is not None:
                    # 恢复路径同样补回 run 的判定/ID（与首次执行路径同一契约）。
                    return replace(composed, run_id=run_id, result=outcome.result)

        return TurnOutcome(
            status="answered",
            run_id=run_id,
            result=outcome.result,
            text=(outcome.result.summary if outcome.result else ""),
        )

    def _history_lines(self, *, limit: int = 8) -> list[str]:
        """D-030 L1/M4-A：最近窗口的对话原文行 + 旧轮收敛计数行（规则生成、零模型）。

        历史数据来自 fold（事件日志），这里只做格式化与窗口裁剪。
        """
        history = list(self.state.history)
        window = history[-limit:]
        lines = [f"{entry['role']}: {entry['text']}" for entry in window]
        older = len(history) - len(window)
        if older > 0:
            lines.insert(0, f"（更早 {older} 条已略，事件日志可查）")
        return lines

    # ------------------------------------------------------ L2 长期记忆 --
    def _memory_path(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / "memory" / "memory.md"

    def _memory_lines(self) -> list[str]:
        """D-030 L2/M7-A：长期记忆注入行（人确认过的条目，逐行读取）。

        「不信任自己的记忆」：这只是理解用户的背景，其中任何事实要进建议，
        都必须由当前引用证据重新验证（policy 第 1 条守着这道门）。
        """
        path = self._memory_path()
        if path is None or not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        ]

    def _memory_draft(self, draft: MemoryDraft, decision: IntentDecision, text: str) -> TurnOutcome:
        """D-030 L2：起草不是写入——落 memory.proposed，等用户确认（M6-A）。"""
        entries = [{"category": entry.category, "content": entry.content} for entry in draft.entries]
        self.writer.emit(
            EventType.MEMORY_PROPOSED,
            {"entries": entries, "source_text": (text or "")[:500]},
        )
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "answered"})
        listing = "\n".join(
            f"  {index}. [{entry['category']}] {entry['content']}"
            for index, entry in enumerate(entries, 1)
        )
        return TurnOutcome(
            status="answered",
            text=f"我打算记下这几条：\n{listing}\n\n确认请回复「确认」，不记请回复「取消」。",
            decision=decision,
        )

    def _confirm_memory(self, pending: list[dict[str, str]]) -> TurnOutcome:
        turn_no = max(len(self.state.turns), 1)
        self._append_memory_file(pending, turn_no)
        self.writer.emit(
            EventType.MEMORY_RESOLVED,
            {"applied": True, "decision": "confirm", "entries": pending},
        )
        listing = "；".join(f"[{entry['category']}] {entry['content']}" for entry in pending)
        return TurnOutcome(status="answered", text=f"已记下 {len(pending)} 条：{listing}。")

    def _cancel_memory(self) -> TurnOutcome:
        self.writer.emit(EventType.MEMORY_RESOLVED, {"applied": False, "decision": "cancel"})
        return TurnOutcome(status="answered", text="好，这几条不记了。")

    def _append_memory_file(self, entries: list[dict[str, str]], turn_no: int) -> None:
        """把确认过的条目写进 `<root>/memory/memory.md`（四类 section，人可编辑）。"""
        path = self._memory_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        sections: dict[str, list[str]] = {name: [] for name in _MEMORY_SECTIONS}
        if path.exists():
            current: str | None = None
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped in sections:
                    current = stripped
                    continue
                if current and stripped:
                    sections[current].append(line)
        for entry in entries:
            sections.setdefault(entry["category"], []).append(
                f"- [{date.today().isoformat()}] {entry['content']}（来源：第 {turn_no} 轮对话）"
            )
        blocks = ["# JAgent 记忆（人确认过的条目；可手工编辑，注入时逐行读取）"]
        for name in _MEMORY_SECTIONS:
            blocks.append(f"## {name}")
            blocks.extend(sections[name] or ["（暂无）"])
        path.write_text("\n".join(blocks) + "\n", encoding="utf-8")

    # ---------------------------------------------------------- 投递全文落盘 --
    def _store_delivery_text(self, text: str) -> str | None:
        """缺口 5：投递全文落产物库，事件里只留 artifact_id（正文可按 id 取回）。

        未注入 artifacts 的测试/手工装配跳过——产物库是可选依赖，投递不是。
        """
        if self.artifacts is None:
            return None
        return self.artifacts.put_text(text).artifact_id

    # ---------------------------------------------------------------- cancel --
    def _reap_expired_approvals(self) -> None:
        """TTL 过期的挂起 run 自动取消（BUG2-08 余项 / 架构 §3.2 TTL 行）。

        每个 turn 边界扫一次——确定性、无后台线程；语义完全复用 cancel_run
        的 waiting_human 分支（D-028：run.cancelled + token 作废 + reply 通知）。
        """
        with self.world.lock:  # D-045：快照与 Writer 的写互斥
            pending = [
                (run_id, run.approval)
                for run_id, run in self.world.runs.items()
                if run.status == "waiting_human" and run.approval
            ]
        for run_id, approval in pending:
            expires_at = float((approval or {}).get("expires_at") or 0.0)
            if expires_at and time.time() > expires_at:
                self.cancel_run(run_id, reason="ttl_expired")

    def cancel_run(self, run_id: str, reason: str = "user cancelled") -> TurnOutcome:
        """取消入口（D-028）：按 run 当前状态分派，事件只写一次。

        - completed / failed → RuntimeError（入口映射 409）：终态不可取消；
        - cancelled → 幂等：返回当前态，不落第二条事件；
        - waiting_human → 直接落 run.cancelled + reply 通知（没有循环可协作；
          原 turn 已以 handed_off 结束，这里不写 turn 事件）；
        - running / planning → runner.cancel 设标记，`drive` 在步边界消费，
          消费方负责 run.cancelled 与通知（边界取消，S2）。
        """
        run = self.world.runs[run_id]
        if run.status in ("completed", "failed"):
            raise RuntimeError(f"run {run_id} is already {run.status}")
        if run.status == "cancelled":
            return TurnOutcome(status="answered", run_id=run_id, text=_CANCEL_NOTICE)
        if run.status == "waiting_human":
            self.writer.emit(EventType.RUN_CANCELLED, {"reason": reason}, run_id=run_id)
            self._emit_cancel_notice()
            self._pending_turns.pop(run_id, None)
            return TurnOutcome(status="answered", run_id=run_id, text=_CANCEL_NOTICE)
        # running / planning：协作取消；这里不写事件，顺序由 runner 保证。
        self.runner.cancel(run_id, reason)
        return TurnOutcome(
            status="answered",
            run_id=run_id,
            text="取消已请求：当前步骤完成后停止。",
        )

    def _emit_cancel_notice(self) -> None:
        """U2 收尾（D-028）：取消没有结论，给会话流一条 reply 通知。

        复用 reply 侧的合规扫描与事件形状（gate.checked{profile:reply} +
        advice.delivered{mode:reply}），不发明新事件形状；**不带 run_id**——
        run.cancelled 已把 run 送进终态，带 run_id 的事件会被 I1 终态守卫吸收。
        """
        violations = scan_reply(_CANCEL_NOTICE)
        self.writer.emit(
            EventType.GATE_CHECKED,
            {
                "profile": "reply",
                "pass": not violations,
                "violations": [
                    {"code": v.code, "rule_id": v.rule_id, "detail": v.detail} for v in violations
                ],
                "ruleset": "reply.v8.1",
                "text_hash": digest_of(_CANCEL_NOTICE),
            },
        )
        self.writer.emit(
            EventType.ADVICE_DELIVERED,
            {
                "advice": {"body": _CANCEL_NOTICE},
                "mode": "reply",
                "delivery": delivery_dict(
                    Delivery(
                        text=_CANCEL_NOTICE,
                        regime=Regime.VERIFIED,
                        fallback={"bool": True, "reason": "cancelled"},
                    )
                ),
            },
        )

    def _last_turn_text(self) -> str:
        """从事件日志取本轮原文（恢复时进程可能已经重启，内存上下文会丢）。"""
        for event in reversed(self.writer.store.read(session_id=self.session_id)):
            if event.type is EventType.TURN_RECEIVED:
                text = event.payload.get("text")
                return text if isinstance(text, str) else ""
        return ""

    # ---------------------------------------------------------------- helper --
    def _needs_input(self, question: Question | None, decision: IntentDecision) -> TurnOutcome:
        self.writer.emit(EventType.TURN_ENDED, {"outcome": "needs_input"})
        return TurnOutcome(status="needs_input", question=question, decision=decision)

    def _ask_path_choice(self) -> Question:
        question = Question(
            slot=PATH_SLOT,
            text="你是想要一个判断/建议，还是要我真的去执行？",
            options=("要建议（给判断框架）", "要执行（真的去跑）"),
        )
        self._ask([question], kind="path_choice")
        return question

    def _ask(self, questions: Sequence[Question], *, kind: str) -> Question | None:
        if not questions:
            return None
        self.writer.emit(
            EventType.CLARIFY_ASKED,
            {
                "round": self.state.clarify_round + 1,
                "kind": kind,
                "questions": [
                    {"slot": q.slot, "text": q.text, "options": list(q.options)} for q in questions
                ],
            },
        )
        self._pending = questions[0]
        return questions[0]


def _citation_dict(citation: Citation) -> dict[str, Any]:
    """引用落事件：**保留 seq**（grounding §2.1 / §4，C3-01）。"""
    return {
        "step_id": citation.step_id,
        "field": citation.field,
        "label": citation.label,
        "seq": citation.seq,
    }


def _judge_dict(record: JudgeRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"verdict": record.verdict, "reason": record.reason, "model": record.model}


def _unreviewed(record: JudgeRecord | None) -> bool:
    """语义评审是否没有真正发生：未配置（``none``）、调不通（``unavailable``）或没有记录。"""

    return record is None or record.verdict in ("none", "unavailable")


def _fallback_dict(fallback_record: Any) -> dict[str, Any]:
    """``fallback{bool, reason}``，与 mode 正交（grounding §3 T1 / §4）。"""
    if fallback_record is None:
        return {"bool": False, "reason": ""}
    return {"bool": True, "reason": fallback_record.reason}


def _mark_unsemantic(advice: Advice) -> Advice:
    """把"未经语义评审"写进**系统声明段**（不改模型正文）。

    ``Advice.disclaimer`` 非空时替代默认声明，所以这里先把默认声明补上，
    再追加标记；投递层（delivery.py）不需要知道 judge 的存在。
    """
    base = advice.disclaimer.strip() or DEFAULT_DISCLAIMER
    if _UNSEMANTIC_MARK in base:
        return advice
    return replace(advice, disclaimer=f"{base}\n{_UNSEMANTIC_MARK}")


def _append_system_line(advice: Advice, line: str) -> Advice:
    """把系统生成的一行追加到**系统声明段**（不是模型正文），再交给 render。

    #16 的"N 步未成功"由 Session 写：模型输出里没有这一行，投递文本里有。
    """
    base = advice.disclaimer.strip() or DEFAULT_DISCLAIMER
    return replace(advice, disclaimer=f"{base}\n{line}")


def _advice_dict(advice: Advice) -> dict[str, Any]:
    return {
        "info": list(advice.info),
        "risks": list(advice.risks),
        "framework": list(advice.framework),
        "refs": [_citation_dict(citation) for citation in advice.refs],
        "sources": list(advice.sources),
        "disclaimer": advice.disclaimer,
        "body": advice.body,
    }


def _advice_delivery(text: str, verdict: Any) -> Delivery:
    """gate 裁决 + 文本 → Delivery（R2 接地 / R3 未核验 / 兜底 verified）。

    - 过闸且有引用：``grounded``，checks 记 ``citations.grounded``，grounds 是引用 seq；
    - 过闸但无引用（framework）：``unverified``——没有可核对来源就不是接地文本；
    - 不过闸：投递的是兜底模板（代码文本），``verified`` + ``fallback{bool,reason}``。
    """

    citations = tuple(getattr(verdict, "citations", ()) or ())
    grounds = tuple(
        citation.seq for citation in citations if getattr(citation, "seq", None) is not None
    )
    if not getattr(verdict, "passed", False):
        codes = tuple(getattr(v, "code", "") for v in (getattr(verdict, "violations", ()) or ()))
        return Delivery(
            text=text,
            regime=Regime.VERIFIED,
            fallback={"bool": True, "reason": codes[0] if codes else "gate_blocked"},
        )
    checks = (CHECK_CITATIONS,) if citations else ()
    return Delivery(
        text=text,
        regime=Regime.GROUNDED if checks else Regime.UNVERIFIED,
        checks=checks,
        grounds=grounds,
        fallback={"bool": False, "reason": ""},
    )


def _is_outage(exc: BaseException) -> bool:
    """模型层不可用：``PlanDraftError.outage`` 优先，其次原始 ``ModelUnavailable``。"""
    return bool(getattr(exc, "outage", False)) or _is_model_outage(exc)


def _is_model_outage(exc: BaseException) -> bool:
    """模型层故障（没接模型 / 配额 / 网络 / 鉴权），不是计划草稿质量问题。"""
    from ..ports import ModelUnavailable

    if isinstance(exc, ModelUnavailable):
        return True
    try:
        from ..agent.model import ModelCallFailed

        return isinstance(exc, ModelCallFailed)
    except ImportError:  # 模型层没装
        return False


def _is_planning_failure(exc: BaseException) -> bool:
    """只把人可恢复的规划失败转成人话；真正的 bug 继续抛。"""
    from ..ports import ModelUnavailable

    if isinstance(exc, (ModelUnavailable, PlanningError)):
        return True
    try:
        from ..agent.llm_planner import PlanDraftError

        return isinstance(exc, PlanDraftError)
    except ImportError:  # 还没接 LLM planner
        return False
