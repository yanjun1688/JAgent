"""P2-f/h · runner 健壮性缺口（BUG2-09 半写 run / BUG2-12 墙钟记账）。

BUG2-09：只有 ``run.started``、没有 ``plan.frozen`` 的半写 run（崩溃现场），
``Runner.drive`` 会因"没有下一步"走 ``_finish``，报 ``completed(0/0)``——
崩溃被伪装成成功。修复：plan 为 None 时 fail-closed（``no_frozen_plan``）。

BUG2-12：墙钟预算从不记账，``Result.budget_used["wall_s"]`` 恒 0.0——
超时 run 和正常 run 都无法回答"这次花了多久"。修复：墙钟超时与正常收尾
两个出口都记 ``budget.consumed{kind:"wall_s"}``（report 直接复制投影）。
"""

from __future__ import annotations

import time


from jagent.contracts import Budget, EventType, Plan, Step, ToolResult, Verify

from .conftest import Rig
from .test_runner_cancel import _probe_spec


def _sleepy_probe(rig: Rig, seconds: float) -> list[str]:
    calls: list[str] = []

    def fn(args, workspace, timeout):
        time.sleep(seconds)
        calls.append("slept")
        return ToolResult(ok=True)

    rig.registry.add(_probe_spec("sleeper"), fn)
    return calls


def _sleep_plan() -> Plan:
    return Plan(steps=(Step(id="sleeper", tool="sleeper", verify=Verify("True", needs=())),))


# --------------------------------------------------------------------------- #
# BUG2-09：半写 run fail-closed
# --------------------------------------------------------------------------- #

def test_half_written_run_fails_loudly_not_completed(rig: Rig) -> None:
    """Given 只落了 run.started（崩溃在 frozen 之前）的 run，When drive，
    Then status=failed、fail_reason=no_frozen_plan，绝不伪装 completed(0/0)。"""
    run_id = "run-half-written"
    rig.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=run_id,
    )

    runner = rig.runner()
    outcome = runner.drive(run_id)

    assert outcome.status == "failed"
    assert rig.world.runs[run_id].fail_reason == "no_frozen_plan"
    assert rig.of_type(EventType.RUN_COMPLETED) == []
    assert rig.of_type(EventType.RUN_FAILED)[-1].payload["reason"] == "no_frozen_plan"


# --------------------------------------------------------------------------- #
# BUG2-12：墙钟记账
# --------------------------------------------------------------------------- #

def test_wall_clock_exceed_records_budget(rig: Rig) -> None:
    """Given max_wall_s=0.01 且工具睡 50ms，When 第二个循环边界超时，
    Then run 失败于 wall_clock_exceeded，且 budget.consumed{wall_s} 已落盘。"""
    _sleepy_probe(rig, 0.05)
    runner = rig.runner(budget=Budget(max_wall_s=0.01))

    outcome = runner.start_and_run(_sleep_plan())

    assert outcome.status == "failed"
    assert rig.world.runs[outcome.run_id].fail_reason == "wall_clock_exceeded"
    wall_budgets = [
        e.payload
        for e in rig.events()
        if e.type is EventType.BUDGET_CONSUMED and e.payload.get("kind") == "wall_s"
    ]
    assert wall_budgets and wall_budgets[-1]["total"] > 0, "墙钟超时必须记账"


def test_normal_finish_records_wall_budget(rig: Rig) -> None:
    """Given 正常完成的 run，When _finish，
    Then budget_used.wall_s > 0——"这次花了多久"有据可查。"""
    runner = rig.runner(budget=Budget(max_wall_s=30.0))
    _sleepy_probe(rig, 0.01)

    outcome = runner.start_and_run(_sleep_plan())

    assert outcome.status == "completed"
    assert outcome.result is not None
    wall = outcome.result.budget_used.get("wall_s")
    assert wall is not None and wall > 0, f"budget_used.wall_s={wall!r}"
