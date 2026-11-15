"""I4 · 状态机：唯一改状态的地方，非法转移必须拒绝且留痕。

规格依据：
- §2「所有转移显式化。State Machine 是唯一允许改状态的地方，非法转移直接拒绝
  （拒绝本身也落事件）」；
- §2 表格：step ``passed`` 只能去 ``stale``；run 的 ``completed/failed/cancelled``
  没有出边；``waiting_human`` 只属于 run。

Task 4 要求的攻击：直接调用 ``writer.step_to`` 做 ``passed → running``。
"""

from __future__ import annotations

import pytest

from jagent.contracts import EventType, Plan, Step, Verify, plan_to_dict
from jagent.harness.statemachine import (
    IllegalTransitionError,
    RUN_TERMINAL,
    run_can,
    step_can,
)


def _run_with_step(harness) -> str:
    run_id = "run_fixed"
    plan = Plan(
        steps=(
            Step(id="s1", tool="probe", verify=Verify("ok", needs=("ok",))),
        )
    )
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "test", "config_hash": "test"}},
        run_id=run_id,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=run_id,
    )
    return run_id


def _reach_passed(harness) -> str:
    run_id = _run_with_step(harness)
    harness.writer.step_to(run_id, "s1", "running", "started")
    harness.writer.step_to(run_id, "s1", "passed", "assertions_passed")
    assert harness.world.runs[run_id].steps["s1"].status == "passed"
    return run_id


def test_transition_table_matches_spec() -> None:
    # §2 的表格逐条核对（这是契约，不是实现细节）。
    assert step_can("pending", "running")
    assert step_can("pending", "skipped")
    assert step_can("running", "passed")
    assert step_can("running", "failed")
    assert step_can("failed", "pending")
    assert step_can("passed", "stale")
    assert step_can("stale", "pending")
    assert not step_can("passed", "running")
    assert not step_can("skipped", "pending")
    # waiting_human 只属于 run，不属于 step。
    assert not step_can("running", "waiting_human")

    assert run_can("planning", "running")
    assert run_can("running", "waiting_human")
    assert run_can("waiting_human", "running")
    for terminal in RUN_TERMINAL:
        assert not run_can(terminal, "running")
        assert not run_can(terminal, "failed")
    assert not run_can("running", "stale")


def test_illegal_step_transition_raises(harness) -> None:
    run_id = _reach_passed(harness)

    with pytest.raises(IllegalTransitionError):
        harness.writer.step_to(run_id, "s1", "running", "again")


def test_illegal_step_transition_is_recorded(harness) -> None:
    run_id = _reach_passed(harness)

    with pytest.raises(IllegalTransitionError):
        harness.writer.step_to(run_id, "s1", "running", "again")

    illegal = [
        event
        for event in harness.of_type(EventType.STEP_STATE_CHANGED)
        if event.payload.get("illegal") is True
    ]
    assert len(illegal) == 1
    assert illegal[0].payload["from"] == "passed"
    assert illegal[0].payload["to"] == "running"
    assert illegal[0].step_id == "s1"


def test_illegal_step_transition_does_not_mutate_state(harness) -> None:
    """已修复（本轮）：fold 遇到 illegal:true 只记诊断，不再应用 to 状态。"""
    run_id = _reach_passed(harness)

    with pytest.raises(IllegalTransitionError):
        harness.writer.step_to(run_id, "s1", "running", "again")

    # 拒绝就是没发生：世界投影必须仍是 passed。
    assert harness.world.runs[run_id].steps["s1"].status == "passed"


def test_run_terminal_has_no_outgoing_transition(harness) -> None:
    run_id = _run_with_step(harness)
    harness.writer.emit(EventType.RUN_COMPLETED, {"result": {"verdict": "completed"}}, run_id=run_id)

    with pytest.raises(IllegalTransitionError):
        harness.writer.run_to(run_id, "failed")


def test_rejected_run_transition_is_recorded(harness) -> None:
    run_id = _run_with_step(harness)
    harness.writer.emit(EventType.RUN_COMPLETED, {"result": {"verdict": "completed"}}, run_id=run_id)

    with pytest.raises(IllegalTransitionError):
        harness.writer.run_to(run_id, "failed")

    illegal = [
        event
        for event in harness.events()
        if event.payload.get("illegal") is True and event.run_id == run_id
    ]
    assert illegal, "run 的非法转移也必须留痕（§2）"


def test_terminal_run_cannot_be_rewritten_by_a_later_event(harness) -> None:
    run_id = _run_with_step(harness)
    harness.writer.emit(EventType.RUN_COMPLETED, {"result": {"verdict": "completed"}}, run_id=run_id)
    assert harness.world.runs[run_id].status == "completed"

    # 绕过状态机直接写一条 run.failed：投影不能被改回 failed。
    harness.writer.emit(EventType.RUN_FAILED, {"reason": "forged"}, run_id=run_id)

    assert harness.world.runs[run_id].status == "completed"
