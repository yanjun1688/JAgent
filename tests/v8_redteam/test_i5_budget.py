"""I5 · 预算：有上限、超限必须停下、不能无限循环。

规格依据：
- §4.7「工具调用有预算（步数 / 时间）」；
- §4.10「修复次数有上限，超了 → run.failed + 报告」；
- §4.9「瞬时：退避重试（≤ 3 次）」；
- M1 出口「断言失败时 run 是 failed，不是 completed」。

Task 4 要求的攻击：``max_steps=1`` 喂一个 3 步计划。
"""

from __future__ import annotations

import time


from jagent.contracts import Budget, EventType, Plan, Step, Verify


def _plan(n: int, *, verify: str = "ok", needs: tuple[str, ...] = ("ok",)) -> Plan:
    return Plan(
        steps=tuple(
            Step(id=f"s{i}", tool="probe", verify=Verify(verify, needs=needs)) for i in range(n)
        )
    )


def test_runner_max_steps_is_enforced(harness, make_runner) -> None:
    """已修复（本轮）：drive 每轮比较 run.budget["steps"] >= max_steps，超了直接 fail。"""
    from tests.v8_redteam.conftest import probe_tool

    spec, fn, calls = probe_tool(name="probe")
    runner = make_runner(harness, tools=[(spec, fn)], budget=Budget(max_steps=1))

    outcome = runner.start_and_run(_plan(3))

    # 先保证没有死循环（工具上限由 probe_tool 兜底）。
    assert len(calls) <= 10, f"scheduler did not stop: {len(calls)} tool calls"
    assert outcome.status != "completed", (
        f"max_steps=1 却执行了 {len(calls)} 步并判 completed；预算只在 Session 的 "
        "check_plan 里检查，Runner 只记账不拦截"
    )
    assert len(calls) <= 1


def test_wall_clock_budget_fails_the_run(harness, make_runner) -> None:
    from jagent.contracts import ToolResult
    from tests.v8_redteam.conftest import probe_spec

    spec = probe_spec(name="probe")
    calls: list[int] = []

    def slow(_args, _workspace, _timeout):
        time.sleep(0.05)
        calls.append(1)
        return ToolResult(ok=True)

    runner = make_runner(harness, tools=[(spec, slow)], budget=Budget(max_wall_s=0.01))

    outcome = runner.start_and_run(_plan(1))

    assert outcome.status == "failed"
    assert "wall_clock" in outcome.reason
    assert len(calls) <= 1
    assert harness.of_type(EventType.RUN_FAILED)


def test_transient_retry_is_bounded(harness, make_runner) -> None:
    from tests.v8_redteam.conftest import probe_tool

    spec, fn, calls = probe_tool(name="probe", ok=False, error_code="timeout")
    runner = make_runner(harness, tools=[(spec, fn)], budget=Budget(transient_per_step=3))

    started = time.monotonic()
    outcome = runner.start_and_run(_plan(1))
    elapsed = time.monotonic() - started

    assert outcome.status == "failed"
    assert len(calls) == 3, f"瞬时重试应当 ≤3 次，实际 {len(calls)}"
    assert elapsed < 3.0, "退避不应把测试拖长（规格要求 5s 内）"


def test_repair_loop_is_bounded(harness, make_runner) -> None:
    from tests.v8_redteam.conftest import probe_tool

    spec, fn, calls = probe_tool(name="probe", data={"n": 1}, observations=("ok", "n"))

    class AlwaysRepair:
        def repair(self, *, plan, step_id, failure, attempt):
            return plan

    runner = make_runner(
        harness,
        tools=[(spec, fn)],
        repairer=AlwaysRepair(),
        budget=Budget(max_repairs=2),
    )

    outcome = runner.start_and_run(
        Plan(steps=(Step(id="s0", tool="probe", verify=Verify("n == 2", needs=("n",))),))
    )

    assert outcome.status == "failed"
    assert len(calls) <= 5, f"修复次数没有上限：{len(calls)} 次调用"
    assert harness.world.runs[outcome.run_id].budget["repairs"] <= 2
