"""S1 · run.cancelled 事件契约与投影（#17 / D-020，实现口径见 D-028）。

规格依据：
- 架构 §2：``cancelled`` 是 run 终态（用户取消 / 人工确认中心）；
  planning / running / waiting_human → cancelled 均为合法转移，终态无出边；
- 架构 §3.2 事件表：``run.cancelled`` 载荷 ``{reason, at_step?}``，``reason`` 必填；
- 宪法 I1：终态不可改写——cancelled 与 completed / failed 同受终态守卫；
- D-028：取消语义为边界取消（在跑步骤跑完、后续步不开）+ U2 收尾
  （reply 通知，不落 fallback.advice；D-002 预留的 ``run_aborted`` 由此删除）。

每个场景 docstring 按 Given / When / Then 写明行为契约。
"""

from __future__ import annotations

import copy

import pytest

from jagent.contracts import EventDraft, EventType, Plan, Step, Verify, plan_to_dict
from jagent.harness.fold import fold
from jagent.harness.store import EventStore

from .conftest import RUN_ID, SESSION_ID, STEP_ID, Harness

#: 与 I1 测试共用的事件前缀形状：started → frozen → (waiting_human 可选)。


def _seed_running(harness: Harness, plan: Plan | None = None) -> None:
    """Given 一个已冻结计划、处于 running 的 run。"""
    plan = plan or Plan(steps=(Step(id=STEP_ID, tool="probe", verify=Verify("True", needs=())),))
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=RUN_ID,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=RUN_ID,
    )


def _seed_waiting_human(harness: Harness) -> str:
    """Given 一个挂起等人的 run（审批请求已发），返回 token。"""
    _seed_running(harness)
    token = "tok_cancel_me"
    harness.writer.emit(
        EventType.APPROVAL_REQUESTED,
        {
            "step_id": STEP_ID,
            "token": token,
            "payload_hash": "h" * 64,
            "expires_at": 9_999_999_999.0,
        },
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    harness.writer.emit(EventType.RUN_SUSPENDED, {"at_step": STEP_ID}, run_id=RUN_ID, step_id=STEP_ID)
    return token


def _snapshot(harness: Harness) -> dict:
    run = harness.world.runs[RUN_ID]
    return copy.deepcopy(
        {
            "fingerprint": harness.world.fingerprint(),
            "run_status": run.status,
            "cancel_reason": getattr(run, "cancel_reason", None),
            "result": run.result,
        }
    )


def _rejected(harness: Harness) -> list:
    return harness.of_type(EventType.RUN_TRANSITION_REJECTED)


# --------------------------------------------------------------------------- #
# 场景 1：running 的 run 被取消——事件落盘、投影到终态、cancel_reason 可查
# --------------------------------------------------------------------------- #

def test_running_run_transitions_to_cancelled(harness) -> None:
    """Given 一个 running 的 run，When 取消入口写 run.cancelled{reason}，
    Then run 投影到 cancelled 终态，cancel_reason 随事件可查，事件只落一条。"""
    _seed_running(harness)
    assert harness.world.runs[RUN_ID].status == "running"

    recorded = harness.writer.emit(
        EventType.RUN_CANCELLED,
        {"reason": "user cancelled", "at_step": STEP_ID},
        run_id=RUN_ID,
    )

    assert recorded.type is EventType.RUN_CANCELLED
    assert recorded.run_id == RUN_ID
    run = harness.world.runs[RUN_ID]
    assert run.status == "cancelled"
    assert run.cancel_reason == "user cancelled"
    assert len(harness.of_type(EventType.RUN_CANCELLED)) == 1


# --------------------------------------------------------------------------- #
# 场景 2：waiting_human 的 run 可以直接取消（人工确认中心是取消来源之一）
# --------------------------------------------------------------------------- #

def test_waiting_human_run_can_be_cancelled(harness) -> None:
    """Given 一个挂起等人的 run，When 写 run.cancelled，
    Then waiting_human → cancelled 是合法转移，run 落终态。"""
    _seed_waiting_human(harness)
    assert harness.world.runs[RUN_ID].status == "waiting_human"

    recorded = harness.writer.emit(
        EventType.RUN_CANCELLED,
        {"reason": "user gave up while waiting"},
        run_id=RUN_ID,
    )

    assert recorded.type is EventType.RUN_CANCELLED
    assert harness.world.runs[RUN_ID].status == "cancelled"


# --------------------------------------------------------------------------- #
# 场景 3：cancelled 是吸收态——后续任何带 run_id 的事件都被拒且留痕
# --------------------------------------------------------------------------- #

def test_cancelled_run_is_absorbing(harness) -> None:
    """Given 一个已取消的 run，When 迟到的 step.finished / 二次取消 / 伪造
    run.completed 到达，Then 每条都被替换为 run.transition_rejected，
    投影逐字段不变。"""
    _seed_running(harness)
    harness.writer.emit(EventType.RUN_CANCELLED, {"reason": "user cancelled"}, run_id=RUN_ID)
    before = _snapshot(harness)
    assert before["run_status"] == "cancelled"

    late_events = [
        (
            EventType.STEP_FINISHED,
            {"step_id": STEP_ID, "status": "ok", "artifact_refs": []},
            STEP_ID,
        ),
        (EventType.RUN_CANCELLED, {"reason": "double cancel"}, None),
        (EventType.RUN_COMPLETED, {"result": {"verdict": "completed"}}, None),
    ]
    rejected_types = []
    for type_, payload, step_id in late_events:
        recorded = harness.writer.emit(type_, payload, run_id=RUN_ID, step_id=step_id)
        assert recorded.type is EventType.RUN_TRANSITION_REJECTED
        assert recorded.payload["target_status"] == "cancelled"
        rejected_types.append(recorded.payload["rejected_type"])

    assert rejected_types == ["step.finished", "run.cancelled", "run.completed"]
    assert len(_rejected(harness)) == len(late_events)
    assert _snapshot(harness) == before


# --------------------------------------------------------------------------- #
# 场景 4：含取消的流可重放——指纹一致、无未知事件诊断、换句柄仍一致
# --------------------------------------------------------------------------- #

def test_cancelled_stream_replays_identically(harness) -> None:
    """Given 一条含 run.cancelled 与迟到拒绝事件的流，When 折叠两次并换
    store 句柄重放，Then 指纹一致且 run.cancelled 不是未知事件。"""
    _seed_running(harness)
    harness.writer.emit(EventType.RUN_CANCELLED, {"reason": "user cancelled"}, run_id=RUN_ID)
    harness.writer.emit(
        EventType.STEP_FINISHED,
        {"step_id": STEP_ID, "status": "ok", "artifact_refs": []},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    events = harness.store.read()

    first = fold(events)
    second = fold(events)

    assert first.fingerprint() == second.fingerprint()
    assert not [d for d in first.diagnostics if "unknown event type" in d], (
        "run.cancelled 必须可重放：未知事件类型会污染 diagnostics"
    )
    assert first.runs[RUN_ID].status == "cancelled"
    assert first.runs[RUN_ID].cancel_reason == "user cancelled"
    assert len([e for e in events if e.type is EventType.RUN_TRANSITION_REJECTED]) == 1

    other = EventStore(harness.db_path)
    try:
        replayed = fold(other.read())
        assert replayed.fingerprint() == first.fingerprint()
        assert replayed.runs[RUN_ID].status == "cancelled"
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# 场景 5：reason 必填——validate_draft 是唯一必填字段闸门（conformance 契约）
# --------------------------------------------------------------------------- #

def test_cancelled_without_reason_is_rejected_by_validate_draft(harness) -> None:
    """Given 取消事件缺 reason，When 走 store.append，
    Then validate_draft 拒绝——没有理由的取消不允许落账本。"""
    from jagent.contracts import validate_draft

    _seed_running(harness)
    with pytest.raises(Exception) as excinfo:
        validate_draft(
            EventDraft(
                EventType.RUN_CANCELLED,
                {"at_step": STEP_ID},
                SESSION_ID,
                RUN_ID,
            )
        )
    assert "reason" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 场景 7（S5）：预算账本在取消时闭合——记账制，没有"回收"动作（D-028）
# --------------------------------------------------------------------------- #

def test_budget_ledger_closes_at_cancel(harness) -> None:
    """Given 取消前已有一笔 steps 计账，When 取消后迟到的一笔记账到达，
    Then 终态守卫拒绝它，投影累计不变；live 投影与重放投影的账本逐 kind 相等。
    预算是记账制（budget.consumed 逐笔落盘），取消无回收动作——
    真正要防的是"取消路径漏记/多记"。"""
    _seed_running(harness)
    harness.writer.budget(RUN_ID, "steps")
    before = dict(harness.world.runs[RUN_ID].budget)
    assert before["steps"] == 1.0

    harness.writer.emit(EventType.RUN_CANCELLED, {"reason": "user cancelled"}, run_id=RUN_ID)

    recorded = harness.writer.emit(
        EventType.BUDGET_CONSUMED,
        {"kind": "steps", "delta": 1.0, "total": 2.0},
        run_id=RUN_ID,
    )
    assert recorded.type is EventType.RUN_TRANSITION_REJECTED, "迟到的记账被终态守卫拒绝"
    assert harness.world.runs[RUN_ID].budget == before, "账本在取消点闭合"

    replayed = fold(harness.store.read())
    assert replayed.runs[RUN_ID].budget == harness.world.runs[RUN_ID].budget


# --------------------------------------------------------------------------- #
# 场景 6：取消前的步骤事实保留——已 passed 的步骤不因取消回退
# --------------------------------------------------------------------------- #

def test_completed_step_facts_survive_cancellation(harness) -> None:
    """Given 一个 step 已 passed 的 running run，When 取消，
    Then step 的 passed 状态与 budget 累计保留——事件是真相，取消不改历史。"""
    _seed_running(harness)
    harness.writer.emit(
        EventType.STEP_STARTED,
        {"step_id": STEP_ID, "attempt": 1, "idempotency_key": "k1"},
        run_id=RUN_ID,
        step_id=STEP_ID,
    )
    harness.writer.step_to(RUN_ID, STEP_ID, "passed", "assertions_passed")
    harness.writer.budget(RUN_ID, "steps")
    before_steps = {sid: s.status for sid, s in harness.world.runs[RUN_ID].steps.items()}
    before_budget = dict(harness.world.runs[RUN_ID].budget)

    harness.writer.emit(EventType.RUN_CANCELLED, {"reason": "user cancelled"}, run_id=RUN_ID)

    run = harness.world.runs[RUN_ID]
    assert run.status == "cancelled"
    assert {sid: s.status for sid, s in run.steps.items()} == before_steps
    assert run.budget == before_budget
