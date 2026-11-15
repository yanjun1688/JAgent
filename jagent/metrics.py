"""JAgent v8 · 可观测性指标投影（D-031 S2）。

**纯派生，零采集**：路线图 14 的三原语在事件日志里已经齐备——Trace = run、
Thread = session、Run = model.called / step.*——这里只做聚合投影，不新增
任何事实来源。与 ``evidence.missing`` 同款的中立模块。

指标口径（路线图 14 §2.4，CNCF 实践）：
- 成本：tokens_in / tokens_out / 模型调用次数（S1 的 model.called）；
- 效率：轮次数、run 数、每 run 步数与修复次数；
- 质量：完成率、人工介入率（approval.requested / run）；
- 异常：invalidated 次数、修复预算消耗。

基数纪律（CNCF 警告）：session_id / run_id 永远不进指标标签——单会话细节
走 Trace（事件流），聚合只留计数。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .contracts import Event, EventType


def session_metrics(events: Iterable[Event]) -> dict[str, Any]:
    """会话级聚合：成本 / 效率 / 质量 / 异常四个面，全部由事件派生。"""
    events = list(events)
    runs_started = 0
    verdicts: dict[str, int] = {"completed": 0, "failed": 0, "cancelled": 0}
    approvals = 0
    model_calls = 0
    tokens_in = 0
    tokens_out = 0
    model_tokens_by_purpose: dict[str, dict[str, int]] = {}
    repairs = 0.0
    turns = 0
    memory_proposed = 0
    memory_confirmed = 0
    invalidated = 0

    for event in events:
        kind = event.type
        if kind is EventType.TURN_RECEIVED:
            turns += 1
        elif kind is EventType.RUN_STARTED:
            runs_started += 1
        elif kind is EventType.RUN_COMPLETED:
            verdicts["completed"] += 1
        elif kind is EventType.RUN_FAILED:
            verdicts["failed"] += 1
        elif kind is EventType.RUN_CANCELLED:
            verdicts["cancelled"] += 1
        elif kind is EventType.APPROVAL_REQUESTED:
            approvals += 1
        elif kind is EventType.MODEL_CALLED:
            model_calls += 1
            tokens_in += int(event.payload.get("tokens_in") or 0)
            tokens_out += int(event.payload.get("tokens_out") or 0)
            purpose = str(event.payload.get("purpose") or "unknown")
            bucket = model_tokens_by_purpose.setdefault(
                purpose, {"calls": 0, "tokens_in": 0, "tokens_out": 0}
            )
            bucket["calls"] += 1
            bucket["tokens_in"] += int(event.payload.get("tokens_in") or 0)
            bucket["tokens_out"] += int(event.payload.get("tokens_out") or 0)
        elif kind is EventType.BUDGET_CONSUMED:
            if event.payload.get("kind") == "repairs":
                repairs += float(event.payload.get("delta") or 0.0)
        elif kind is EventType.MEMORY_PROPOSED:
            memory_proposed += 1
        elif kind is EventType.MEMORY_RESOLVED:
            if event.payload.get("applied"):
                memory_confirmed += 1
        elif kind is EventType.STEP_INVALIDATED:
            invalidated += 1

    finished = verdicts["completed"] + verdicts["failed"]
    return {
        "turns": turns,
        "runs": runs_started,
        "verdicts": verdicts,
        "completion_rate": (verdicts["completed"] / finished) if finished else None,
        "human_interventions": approvals,
        "human_intervention_rate": (approvals / runs_started) if runs_started else None,
        "model_calls": model_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model_tokens_by_purpose": model_tokens_by_purpose,
        "repairs": repairs,
        "invalidations": invalidated,
        "memory_proposed": memory_proposed,
        "memory_confirmed": memory_confirmed,
    }


def session_summaries(events: Iterable[Event]) -> list[dict[str, Any]]:
    """对话列表投影（D-034）：每个 session_id 一行——轮数 / run 数 / 首末时间 /
    标题（首轮 user 文本截断）。创建后未发消息的会话不出现在这里（fold 惰性）。"""
    per: dict[str, dict[str, Any]] = {}
    for event in events:
        sid = event.session_id
        if sid is None:
            continue
        entry = per.setdefault(
            sid,
            {
                "session_id": sid,
                "turns": 0,
                "runs": 0,
                "first_at": event.at,
                "last_at": event.at,
                "title": "",
            },
        )
        entry["last_at"] = event.at
        if event.type is EventType.TURN_RECEIVED:
            entry["turns"] += 1
            text = str(event.payload.get("text") or "").strip()
            if text and not entry["title"]:
                entry["title"] = text[:40]
        elif event.type is EventType.RUN_STARTED:
            entry["runs"] += 1
    return sorted(per.values(), key=lambda item: item["last_at"], reverse=True)


def run_metrics(events: Iterable[Event], run_id: str) -> dict[str, Any]:
    """单 run 聚合：终态、步骤面、预算面、时长。模型 token 不归属 run
    （调用多发生在 run 边界之外）——会话级成本看 ``session_metrics``。"""
    in_run = [e for e in events if e.run_id == run_id]
    if not in_run:
        return {}

    verdict = "none"
    fail_reason = ""
    steps: dict[str, int] = {}
    budget: dict[str, float] = {}
    invalidated = 0
    for event in in_run:
        kind = event.type
        if kind is EventType.RUN_COMPLETED:
            verdict = "completed"
        elif kind is EventType.RUN_FAILED:
            verdict = "failed"
            fail_reason = str(event.payload.get("reason") or "")
        elif kind is EventType.RUN_CANCELLED:
            verdict = "cancelled"
        elif kind is EventType.STEP_STATE_CHANGED:
            to = str(event.payload.get("to") or "")
            steps[to] = steps.get(to, 0) + 1
        elif kind is EventType.STEP_INVALIDATED:
            invalidated += 1
        elif kind is EventType.BUDGET_CONSUMED:
            kind_name = str(event.payload.get("kind") or "")
            budget[kind_name] = budget.get(kind_name, 0.0) + float(event.payload.get("delta") or 0.0)

    started_at = in_run[0].at
    ended_at = in_run[-1].at
    return {
        "run_id": run_id,
        "verdict": verdict,
        "fail_reason": fail_reason,
        "steps": steps,
        "budget": budget,
        "invalidations": invalidated,
        "wall_s": round(max(ended_at - started_at, 0.0), 3),
        "events": len(in_run),
    }
