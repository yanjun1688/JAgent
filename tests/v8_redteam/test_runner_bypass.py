"""D-001(d)：Session 被绕过时，坏计划仍必须在 Runner 里自然失败。

`Runner.start_and_run` 不调用 Planner 的草稿自检，也不应该再有一道 Session
计划门。这条测试直接喂一个没有断言（`verify=None`）的计划，证明它死在
Verifier 的 fail-closed 上，而不是靠前置检查兜住。
"""

from __future__ import annotations

from jagent.contracts import EventType, Plan, Step

from .conftest import probe_tool


def test_runner_bypass_without_assertion_still_fails(harness, make_runner) -> None:
    spec, fn, _calls = probe_tool(name="probe", data={"n": 1}, observations=("ok", "n"))
    runner = make_runner(harness, tools=[(spec, fn)])
    plan = Plan(steps=(Step(id="s0", tool="probe"),))

    outcome = runner.start_and_run(plan)

    assert outcome.status == "failed"
    assert "assertion" in outcome.reason
    assert harness.of_type(EventType.RUN_FAILED)
    assert not harness.of_type(EventType.RUN_COMPLETED)
    verified = harness.of_type(EventType.STEP_VERIFIED)
    assert verified and verified[-1].payload["result"] == "fail"
    assert verified[-1].payload["checks"] == [], "无断言不应伪造一条检查记录"
