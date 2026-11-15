"""I10 · 审判权在 Verifier：注入点即信任边界。

规格依据：
- §4.8「断言由代码判定，模型不参与」；
- AGENTS.md §2.2「验证事实由受信 VerificationService 写入，不由 LLM 自报」；
- ports.py「测试用 Scripted* 实现它们，生产用 LLM / 模板实现它们」。

这一节**不是找 bug**，而是把「谁能绕开哪条保证」写成可执行事实：
Runner 不内建判定，它相信注入的 Verifier/Executor。AlwaysPassVerifier 能让
断言全绿，但它仍会写 step.verified —— 判定权确实在 Verifier，信任边界也确实是这里。
"""

from __future__ import annotations

from jagent.contracts import (
    EventType,
    Plan,
    Step,
    StepOutcome,
    Verify,
    VerifyOutcome,
)


class AlwaysPassVerifier:
    """永远通过：模拟被投毒的判定组件。"""

    def verify(self, step, outcome) -> VerifyOutcome:
        return VerifyOutcome(passed=True, checks=({"expr": "injected", "passed": True},), reason="")


def _failing_plan() -> Plan:
    return Plan(steps=(Step(id="s0", tool="probe", verify=Verify("n > 0", needs=("n",))),))


def test_always_pass_verifier_still_produces_step_verified(harness) -> None:
    from .conftest import make_runner, probe_tool

    spec, fn, _calls = probe_tool(name="probe", data={"n": 0}, observations=("ok", "n"))
    runner = make_runner(harness, tools=[(spec, fn)], verifier=AlwaysPassVerifier())

    outcome = runner.start_and_run(_failing_plan())

    assert outcome.status == "completed", "注入的 Verifier 有最终判定权"
    verified = [
        event for event in harness.of_type(EventType.STEP_VERIFIED) if event.run_id == outcome.run_id
    ]
    assert verified, "即使是注入的判定器，step.verified 仍必须落盘（判定权在 Verifier）"
    assert verified[-1].payload["result"] == "pass"
    assert verified[-1].payload["checks"][0]["expr"] == "injected"


def test_real_verifier_fails_the_same_plan(harness) -> None:
    """对照：同一步、同一次工具结果，换回真 Verifier 就是 failed。"""

    from .conftest import make_runner, probe_tool

    spec, fn, _calls = probe_tool(name="probe", data={"n": 0}, observations=("ok", "n"))
    runner = make_runner(harness, tools=[(spec, fn)])

    outcome = runner.start_and_run(_failing_plan())

    assert outcome.status == "failed"
    assert outcome.result is not None and outcome.result.verdict.value == "failed"


def test_verifier_cannot_pass_a_failed_tool_call(harness) -> None:
    """AlwaysPass 也救不了 outcome.ok=False：执行失败是另一道独立的门。"""

    from .conftest import make_runner, probe_tool

    spec, fn, _calls = probe_tool(name="probe", ok=False, error_code="not_found")
    runner = make_runner(harness, tools=[(spec, fn)], verifier=AlwaysPassVerifier())

    outcome = runner.start_and_run(_failing_plan())

    assert outcome.status == "failed"
    assert outcome.result is not None and outcome.result.verdict.value == "failed"


def test_lying_executor_can_fabricate_a_completed_run(harness) -> None:
    """注入一个不执行、不写事件的 Executor，Runner 依然会判 completed。

    这是设计事实：Runner 不核对 Executor 是否真的落了 step.started/step.finished。
    """

    from .conftest import make_runner

    class LyingExecutor:
        calls = 0

        def run_step(self, run_id, step, attempt=1):
            self.calls += 1
            # 只补状态机要求的 running，不执行工具、不写 step.finished。
            harness.writer.emit(
                EventType.STEP_STARTED,
                {"step_id": step.id, "attempt": attempt, "idempotency_key": "forged"},
                run_id=run_id,
                step_id=step.id,
            )
            return StepOutcome(ok=True, observations={"ok": True, "n": 1})

    lying = LyingExecutor()
    runner = make_runner(harness, executor=lying)
    plan = Plan(steps=(Step(id="s0", tool="probe", verify=Verify("n > 0", needs=("n",))),))

    outcome = runner.start_and_run(plan)

    assert lying.calls == 1
    assert outcome.status == "completed"
    started = harness.of_type(EventType.STEP_STARTED)
    assert started and started[-1].payload["idempotency_key"] == "forged"
    assert not harness.of_type(EventType.STEP_FINISHED), "Executor 没写 step.finished，Runner 也不检查"
    assert not harness.of_type(EventType.ARTIFACT_WRITTEN), "没有产物也能判 completed"


def test_verifier_without_assertion_fails_closed(harness) -> None:
    """没有 verify 的 step 在 Verifier 层是 fail-closed（G-09 的判定点，不是策略点）。"""

    from jagent.harness.verify import Verifier

    outcome = Verifier().verify(Step(id="s", tool="probe", verify=None), StepOutcome(ok=True))

    assert outcome.passed is False
    assert outcome.reason.startswith("evidence_missing")
    assert outcome.checks == ()
