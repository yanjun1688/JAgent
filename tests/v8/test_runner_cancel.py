"""S2 · runner 协作取消（#17 / D-028：边界取消）。

规格：
- 取消是**边界**语义：在跑的步让它跑完（工具无法安全中断），下一个步边界落
  ``run.cancelled``，未开始的步不再开始。依据：Agent Roadmap 03 §7 的最小循环
  用"循环边界检查条件"终止（max_steps / answer），12 篇明确"取消到达时工作
  可能已经做完"——协作检查点而不是强制中断；
- ``run.cancelled`` 先于任何结论类事件：没有 report.rendered，RunOutcome 无 result
  （取消的 run 没有结论可给，与 report 层"没有结果就是没有结果"同口径）；
- ``at_step`` 记录取消发生时下一个将要执行的步骤：审计能回答"卡在哪一步被取消"；
- 取消优先于自动终止路径（墙钟 / 步预算）——这是用户意图，不是系统故障；
- 二次取消是 no-op：cancelled 是吸收态，事件只落一条。
"""

from __future__ import annotations

from typing import Any, Callable

from jagent.app.runner import Runner
from jagent.contracts import (
    EventType,
    Idempotency,
    Plan,
    SideEffect,
    Step,
    ToolResult,
    ToolSpec,
    Verify,
    plan_to_dict,
)

from .conftest import Rig


# --------------------------------------------------------------------------- #
# 工具小助手：NATURAL 只读探针，可注册成计数器或带副作用的触发器
# --------------------------------------------------------------------------- #

def _probe_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1",
        args_schema={"type": "object", "properties": {}},
        side_effect=SideEffect.NONE,
        idempotency=Idempotency.NATURAL,
        readonly=True,
        observations=("ok",),
        timeout_s=5.0,
    )


def _register(rig: Rig, name: str, fn: Callable[[dict, Any, float], ToolResult]) -> list[str]:
    calls: list[str] = []

    def wrapper(args, workspace, timeout):
        calls.append(name)
        return fn(args, workspace, timeout)

    rig.registry.add(_probe_spec(name), wrapper)
    return calls


def _three_step_plan() -> Plan:
    verify = Verify("True", needs=())
    return Plan(
        steps=(
            Step(id="probe_a", tool="probe_a", verify=verify),
            Step(id="probe_b", tool="probe_b", verify=verify),
            Step(id="probe_c", tool="probe_c", verify=verify),
        ),
    )


# --------------------------------------------------------------------------- #
# 场景 1：步中请求取消 → 停在下一个步边界，在跑步的事实完整保留
# --------------------------------------------------------------------------- #

def test_cancel_requested_during_a_step_stops_at_the_next_boundary(rig) -> None:
    """Given 三步计划、第 1 步的工具在执行期间请求取消，
    When start_and_run 跑完，
    Then 第 1 步 finished/passed/budget 全部先于 run.cancelled 落盘，
    第 2 步未开始，run.cancelled{reason, at_step=probe_b}，
    没有 completed/failed，也没有 report.rendered。"""
    holder: dict[str, Runner] = {}

    def cancelling_fn(args, workspace, timeout):
        runner = holder.get("runner")
        assert runner is not None
        # 工具只知道"当前这个 run"：run.started 已落盘，投影里有且只有它。
        run_id = next(iter(runner.writer.world.runs))
        runner.cancel(run_id, "user cancelled")
        return ToolResult(ok=True)

    a_calls = _register(rig, "probe_a", cancelling_fn)
    b_calls = _register(rig, "probe_b", lambda args, ws, t: ToolResult(ok=True))
    c_calls = _register(rig, "probe_c", lambda args, ws, t: ToolResult(ok=True))

    runner = rig.runner()
    holder["runner"] = runner
    outcome = runner.start_and_run(_three_step_plan())

    # RunOutcome：cancelled、无结论、带原因。
    assert outcome.status == "cancelled"
    assert outcome.reason == "user cancelled"
    assert outcome.result is None

    # 工具调用：第 1 步跑过，第 2/3 步没跑。
    assert a_calls == ["probe_a"]
    assert b_calls == [] and c_calls == []

    events = rig.events()
    types = [e.type for e in events]
    assert EventType.RUN_CANCELLED in types
    assert EventType.RUN_COMPLETED not in types
    assert EventType.RUN_FAILED not in types
    assert not rig.of_type(EventType.REPORT_RENDERED), "取消的 run 没有结论，不渲染报告"

    cancelled = rig.of_type(EventType.RUN_CANCELLED)[-1]
    assert cancelled.payload["reason"] == "user cancelled"
    assert cancelled.payload["at_step"] == "probe_b"

    # 事件顺序：第 1 步的全部事实先于 run.cancelled；取消不吞掉已完成的工作。
    cancel_idx = types.index(EventType.RUN_CANCELLED)
    assert types.index(EventType.STEP_STARTED) < cancel_idx
    assert types.index(EventType.STEP_FINISHED) < cancel_idx
    assert types.index(EventType.STEP_STATE_CHANGED) < cancel_idx
    budget_idx = [i for i, t in enumerate(types) if t is EventType.BUDGET_CONSUMED]
    assert budget_idx and max(budget_idx) < cancel_idx


# --------------------------------------------------------------------------- #
# 场景 2：一步都没跑就取消 → run.cancelled 指向第一步，无任何 step.started
# --------------------------------------------------------------------------- #

def test_cancel_before_any_step_starts(rig) -> None:
    """Given 手工冻结的 run 尚未执行任何步骤，When 请求取消后 drive，
    Then run.cancelled{at_step=probe_a}，无任何 step.started。"""
    plan = _three_step_plan()
    run_id = "run-cancel-early"
    rig.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=run_id,
    )
    rig.writer.emit(
        EventType.PLAN_FROZEN,
        {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
        run_id=run_id,
    )

    runner = rig.runner()
    runner.cancel(run_id, "changed my mind")
    outcome = runner.drive(run_id)

    assert outcome.status == "cancelled"
    assert outcome.reason == "changed my mind"
    cancelled = rig.of_type(EventType.RUN_CANCELLED)[-1]
    assert cancelled.payload["at_step"] == "probe_a"
    assert rig.of_type(EventType.STEP_STARTED) == []


# --------------------------------------------------------------------------- #
# 场景 3：二次取消是 no-op——cancelled 是吸收态，事件只落一条
# --------------------------------------------------------------------------- #

def test_second_cancel_on_a_cancelled_run_is_a_noop(rig) -> None:
    """Given 一个已取消的 run，When 再次请求取消并 drive，
    Then 不产生第二条 run.cancelled，drive 安全返回。"""
    run_id = "run-cancel-twice"
    rig.writer.emit(
        EventType.RUN_STARTED,
        {"run_id": run_id, "kind": "execute", "env": {"git_commit": "t", "config_hash": "t"}},
        run_id=run_id,
    )
    runner = rig.runner()
    runner.cancel(run_id, "first")
    runner.drive(run_id)
    assert len(rig.of_type(EventType.RUN_CANCELLED)) == 1

    runner.cancel(run_id, "second")
    outcome = runner.drive(run_id)

    assert outcome.status == "cancelled"
    assert len(rig.of_type(EventType.RUN_CANCELLED)) == 1, "吸收态不重复落取消事件"
