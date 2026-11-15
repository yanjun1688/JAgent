"""P2-g · 非终态非法 run 转移的拒绝留痕（BUG2-02）。

QA 复现：对已 ``running`` 的 run 再发一条 ``run.started``，旧 writer 只拦终态，
第二条被接受；fold 的 ``_run_started`` 把投影清空重置——步骤、预算全部蒸发，
且没有任何拒绝记录。修复：非终态的 run 状态事件同样过 ``ensure_run`` 合法性
校验，非法即替换成 ``run.transition_rejected``（与终态守卫同一入口、同一形状）。
"""

from __future__ import annotations

import copy

import pytest

from jagent.contracts import EventType, Plan, Step, Verify, plan_to_dict

from .conftest import RUN_ID, STEP_ID, Harness


@pytest.fixture()
def running(harness: Harness):
    plan = Plan(steps=(Step(id=STEP_ID, tool="probe", verify=Verify("True", needs=())),))
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=RUN_ID,
    )
    harness.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=RUN_ID,
    )
    return plan


def _snapshot(harness: Harness) -> dict:
    run = harness.world.runs[RUN_ID]
    return copy.deepcopy(
        {
            "status": run.status,
            "steps": {sid: s.status for sid, s in run.steps.items()},
            "budget": dict(run.budget),
        }
    )


def test_duplicate_run_started_on_running_run_is_rejected(harness: Harness, running) -> None:
    """Given 一个 running 的 run，When 再发一条 run.started，
    Then 写下 run.transition_rejected 而不是第二条 started；
    投影原样保留（不清空、不重置）。"""
    before = _snapshot(harness)
    assert before["status"] == "running"

    recorded = harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=RUN_ID,
    )

    assert recorded.type is EventType.RUN_TRANSITION_REJECTED
    payload = recorded.payload
    assert payload["rejected_type"] == "run.started"
    assert payload["from"] == "running"
    assert payload["to"] == "planning"
    assert len(harness.of_type(EventType.RUN_STARTED)) == 1
    assert _snapshot(harness) == before


def test_run_started_on_planning_run_is_rejected_as_duplicate(harness: Harness) -> None:
    """Given 只有 run.started 的 planning run，When 再发一条 run.started，
    Then planning→planning 不在合法转移表里，拒绝留痕。"""
    harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=RUN_ID,
    )
    before = _snapshot(harness)

    recorded = harness.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": RUN_ID, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=RUN_ID,
    )

    assert recorded.type is EventType.RUN_TRANSITION_REJECTED
    assert recorded.payload["from"] == "planning"
    assert _snapshot(harness) == before


def test_legal_transitions_still_pass_the_guard(harness: Harness, running) -> None:
    """Given running 的 run，When 发合法的 run 状态事件（run.cancelled），
    Then 照常接受——守卫只拦非法，不误伤合法路径。"""
    recorded = harness.writer.emit(
        EventType.RUN_CANCELLED, {"reason": "legal"}, run_id=RUN_ID
    )
    assert recorded.type is EventType.RUN_CANCELLED
    assert harness.world.runs[RUN_ID].status == "cancelled"
    assert harness.of_type(EventType.RUN_TRANSITION_REJECTED) == []
