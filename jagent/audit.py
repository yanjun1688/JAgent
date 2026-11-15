"""JAgent v8 · 运行验证视图（D-032：LLM 的结论必须可对账）。

用户需求：会话里模型说"完成了"不算数——事后要能翻出那次任务的事件链，
自己核对「声称 vs 证据」。本模块从事件流构建核对视图：每一步的工具/参数/
观测/断言/最终状态/失效记录，加上全部模型调用与一致性结论。

**纯派生，零新存储**：链路数据本来就在 events.db（append-only），这里只做
投影——与 metrics.py 同款的中立模块口径。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .contracts import Event, EventType, plan_from_dict

_REQUIRED_STATUSES = ("passed", "skipped")


def run_verification(events: Iterable[Event], run_id: str) -> dict[str, Any]:
    """把一次 run 的「声称」与「证据」并列成可核对视图。

    - ``claimed``：run 级事件的结论（completed / failed / cancelled + 原因）；
    - ``steps``：每步的工具、参数、观测、断言逐条结果、最终状态、尝试次数、
      是否被失效过——「完成了」的每一条证据都在这里；
    - ``model_calls``：本次会话的全部模型调用（purpose / token / 耗时）；
    - ``consistent`` / ``problems``：声称与证据的对账结论——completed 必须所有
      必需步骤 passed 且断言通过；failed 必须有可追溯的失败原因。
    """
    events = list(events)
    in_run = [e for e in events if e.run_id == run_id]
    if not in_run:
        return {}

    claimed_verdict = "none"
    fail_reason = ""
    for event in in_run:
        if event.type is EventType.RUN_COMPLETED:
            claimed_verdict = "completed"
        elif event.type is EventType.RUN_FAILED:
            claimed_verdict = "failed"
            fail_reason = str(event.payload.get("reason") or "")
        elif event.type is EventType.RUN_CANCELLED:
            claimed_verdict = "cancelled"

    plan = None
    for event in in_run:
        # 证据视图跟随**最新**计划：plan.amended（D-019 载全量计划）覆盖冻结版，
        # 否则修复改了参数后，审计显示的是"没跑过的旧参数"（D-032 review 修复）。
        if event.type in (EventType.PLAN_FROZEN, EventType.PLAN_AMENDED):
            plan = plan_from_dict(event.payload["plan"])

    attempts: dict[str, int] = {}
    observations: dict[str, dict[str, Any]] = {}
    error_codes: dict[str, str | None] = {}
    checks: dict[str, list[dict[str, Any]]] = {}
    verified_result: dict[str, str] = {}
    classes: dict[str, dict[str, Any]] = {}
    invalidated: list[dict[str, Any]] = []
    model_calls: list[dict[str, Any]] = []
    for event in events:
        kind = event.type
        payload = event.payload
        if kind is EventType.STEP_STARTED and event.run_id == run_id:
            step_id = str(payload.get("step_id") or "")
            attempts[step_id] = attempts.get(step_id, 0) + 1
        elif kind is EventType.STEP_FINISHED and event.run_id == run_id:
            step_id = str(payload.get("step_id") or "")
            observations[step_id] = dict(payload.get("observations") or {})
            error_codes[step_id] = payload.get("error_code")
        elif kind is EventType.STEP_VERIFIED and event.run_id == run_id:
            step_id = str(payload.get("step_id") or "")
            checks[step_id] = list(payload.get("checks") or [])
            verified_result[step_id] = str(payload.get("result") or "")
        elif kind is EventType.FAILURE_CLASSIFIED and event.run_id == run_id:
            step_id = str(payload.get("step_id") or "")
            classes[step_id] = {
                "class": payload.get("class"),
                "error_code": payload.get("error_code"),
                "reason": payload.get("reason"),
            }
        elif kind is EventType.STEP_INVALIDATED and event.run_id == run_id:
            invalidated.append(
                {"step_id": payload.get("step_id"), "reason": payload.get("reason")}
            )
        elif kind is EventType.MODEL_CALLED:
            model_calls.append(dict(payload))

    plan_steps = {
        step.id: {"tool": step.tool, "args": dict(step.args or {})}
        for step in (plan.steps if plan is not None else ())
    }

    steps: list[dict[str, Any]] = []
    problems: list[str] = []
    for step_id, meta in plan_steps.items():
        status = "pending"
        for event in in_run:
            if (
                event.type is EventType.STEP_STATE_CHANGED
                and event.payload.get("step_id") == step_id
            ):
                status = str(event.payload.get("to") or status)
        entry = {
            "step_id": step_id,
            "tool": meta["tool"],
            "args": meta["args"],
            "status": status,
            "attempts": attempts.get(step_id, 0),
            "verified": verified_result.get(step_id, ""),
            "checks": checks.get(step_id, []),
            "observations": observations.get(step_id, {}),
            "error_code": error_codes.get(step_id),
            "failure": classes.get(step_id),
            "invalidated": any(i.get("step_id") == step_id for i in invalidated),
        }
        steps.append(entry)
        if (
            claimed_verdict == "completed"
            and status not in _REQUIRED_STATUSES
        ):
            problems.append(
                f"声称 completed 但步骤 {step_id} 终态是 {status!r}"
            )
        if claimed_verdict == "completed" and entry["verified"] != "pass":
            problems.append(f"声称 completed 但步骤 {step_id} 断言结果缺失或未通过")

    steps.sort(key=lambda item: item["step_id"])

    if claimed_verdict == "failed" and not fail_reason:
        problems.append("声称 failed 但没有可追溯的失败原因")
    if claimed_verdict == "none":
        problems.append("没有终态事件：run 未到 completed/failed/cancelled")

    return {
        "run_id": run_id,
        "claimed": {"verdict": claimed_verdict, "fail_reason": fail_reason},
        "steps": steps,
        "model_calls": model_calls,
        "invalidations": invalidated,
        "consistent": not problems,
        "problems": problems,
    }
