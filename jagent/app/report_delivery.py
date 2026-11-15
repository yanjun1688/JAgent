"""run 收尾投递（R2 · grounded）：把终态 Result 复述成一段人话，并做确定性接地检查。

**为什么不在 Runner 里**（宪法 v2 I8）：模型调用不能出现在 run 的终态事务里。否则

- ``wall_s`` / ``max_wall_s`` 失真（模型耗时记不进账，也不受预算管）；
- 终态事件被慢模型拖住（qwen 实测一次 10–14s，超时上限 180s）；
- ``report.rendered.result.summary`` 变成不可重算的文本（纯函数渲染与事件各说各话）。

分工：Runner 只写模板事实（verified）；Session 在终态之后调本模块拿草稿，过确定性检查，
再以 ``advice.delivered{mode:"report", delivery:{...}}`` 投递。检查不过/模型不可用 →
降级为模板文本（仍是 verified），**不阻断事实**。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..contracts import Delivery, Regime, Result
from ..grounding import CHECK_DIRECTIVES, CHECK_NUMBERS, check_numbers
from ..logsupport import event, warn
from .runner import _bounded_observations, _clip_text

#: 事实包里最多带几步（只取前 N 步 + 省略计数）：总结是复述，不是审计报告。
SUMMARY_MAX_STEPS = 12
#: 事实包里单条文字（失败原因 / note / 模板摘要）的上限。
SUMMARY_TEXT_CHARS = 300


def summary_facts(run: Any, result: Result) -> dict[str, Any]:
    """收尾复述的事实包：只装受信组件已有的事实，不装推断。

    - ``verdict`` / ``remaining_plan`` / ``budget_used`` 来自 ``Result``（代码判定）；
    - 每步的 status / attempts / failure_class / error_code / verified_reason 来自事件投影；
    - ``observations`` 走 ``_bounded_observations``（与修复事实同一量级）；
    - ``template_summary`` 是 Reporter 的模板摘要：失败时那里的"转人工"说明不能被吞掉。
    """

    plan_steps = run.plan.by_id() if run.plan is not None else {}
    states = run.step_list()
    steps: list[dict[str, Any]] = []
    for state in states:
        step = plan_steps.get(state.id)
        steps.append(
            {
                "id": state.id,
                "tool": getattr(step, "tool", "") or "",
                "status": state.status,
                "attempts": state.attempts,
                "failure_class": state.failure_class or "",
                "error_code": state.error_code or "",
                "reason": _clip_text(state.verified_reason or "", SUMMARY_TEXT_CHARS),
                "observations": _bounded_observations(state.observations),
            }
        )
    passed = sum(1 for state in states if state.status == "passed")
    return {
        "run_id": run.run_id,
        "verdict": result.verdict.value,
        "kind": run.kind,
        "steps_total": len(states),
        "steps_passed": passed,
        "steps": steps[:SUMMARY_MAX_STEPS],
        "steps_omitted": max(0, len(steps) - SUMMARY_MAX_STEPS),
        "fail_reason": _clip_text(run.fail_reason or "", SUMMARY_TEXT_CHARS),
        "template_summary": _clip_text(result.summary, SUMMARY_TEXT_CHARS * 2),
        "remaining_plan": list(result.remaining_plan),
        "budget_used": dict(result.budget_used),
    }


def _directive_codes(text: str) -> tuple[str, ...]:
    """言语行为检查（I4）：只取"指令性表述"，不取数字规则——数字走接地对账。

    惰性 import：``gate/`` 依赖 ``contracts``，不能反向依赖 ``app/``。
    """

    from ..gate.rules import scan_reply

    return tuple(
        getattr(violation, "code", "")
        for violation in scan_reply(text)
        if getattr(violation, "code", "") == "REPLY_DIRECTIVE"
    )


def _grounds(run: Any) -> tuple[int, ...]:
    """这段复述能指回的事件 seq：本次 run 已通过步骤的证据序号。"""

    return tuple(
        seq for state in run.step_list() for seq in (state.evidence_seqs or ())
    )


def build_run_delivery(
    run: Any, result: Result, *, summarizer: Any = None
) -> Delivery:
    """终态 Result → 对外文本。没有总结器/草稿不合格时返回模板文本（verified）。"""

    template = Delivery(
        text=result.summary,
        regime=Regime.VERIFIED,
        fallback={"bool": False, "reason": ""},
    )
    if summarizer is None:
        return template

    facts = summary_facts(run, result)
    try:
        draft = summarizer.summarize(facts)
    except Exception as exc:  # noqa: BLE001 - 复述失败不能拖垮终态
        warn(
            "delivery downgraded", run=run.run_id, reason="model_unavailable",
            error=f"{type(exc).__name__}: {exc}",
        )
        return dataclasses.replace(template, fallback={"bool": True, "reason": "model_unavailable"})

    text = draft.strip() if isinstance(draft, str) else ""
    if not text:
        warn("delivery downgraded", run=run.run_id, reason="draft_empty")
        return dataclasses.replace(template, fallback={"bool": True, "reason": "draft_empty"})

    codes = _directive_codes(text)
    if codes:
        warn("delivery downgraded", run=run.run_id, reason="directive", codes=list(codes))
        return dataclasses.replace(template, fallback={"bool": True, "reason": "directive"})

    grounded, missing = check_numbers(text, facts)
    if not grounded:
        warn(
            "delivery downgraded", run=run.run_id,
            reason="number_ungrounded", numbers=list(missing[:8]),
        )
        return dataclasses.replace(
            template, fallback={"bool": True, "reason": "number_ungrounded"}
        )

    event("delivery grounded", run=run.run_id, chars=len(text),
          grounds=len(_grounds(run)))
    return Delivery(
        text=text,
        regime=Regime.GROUNDED,
        checks=(CHECK_DIRECTIVES, CHECK_NUMBERS),
        grounds=_grounds(run),
        fallback={"bool": False, "reason": ""},
    )


def delivery_dict(delivery: Delivery | None) -> dict[str, Any] | None:
    """`Delivery` → 事件/API 载荷（regime 必填，checks/grounds 有序）。"""

    if delivery is None:
        return None
    return {
        "regime": delivery.regime.value,
        "checks": list(delivery.checks),
        "grounds": list(delivery.grounds),
        "fallback": dict(delivery.fallback),
        "artifact_id": delivery.artifact_id,
    }
