"""I2 · 判不了 ≠ 通过：断言求值抛异常时，run 不能变成 completed。

规格依据：
- §4.8「断言由代码判定 ... 求值不了按 fail 处理，理由写明 evidence_missing」；
- contracts.py 顶部「判定只有 pass / fail，没有第三态」；
- §8 明确删除了 ``undetermined`` 第三态，替代方案就是「求值不了按 fail」。
"""

from __future__ import annotations

from jagent.contracts import EventType, FailureClass, Plan, Step, StepOutcome, Verify
from jagent.harness.verify import Verifier


def _outcome(expr: str, observations: dict, needs=("content",)):
    step = Step(id="s", tool="probe", verify=Verify(expr, needs=needs))
    return Verifier().verify(step, StepOutcome(ok=True, observations=observations))


def test_type_error_during_evaluation_is_fail() -> None:
    outcome = _outcome("content + 1 > 0", {"content": "abc"})

    assert outcome.passed is False
    assert "eval_error" in outcome.reason
    assert outcome.checks[0]["passed"] is False


def test_value_error_inside_allowlisted_function_is_fail() -> None:
    # int("abc") 在白名单里，但会抛；抛了就是判不了，不是通过。
    outcome = _outcome("int(content) > 3", {"content": "abc"})

    assert outcome.passed is False
    assert "eval_error" in outcome.reason


def test_zero_division_is_fail() -> None:
    outcome = _outcome("1 / 0 > 0", {}, needs=())

    assert outcome.passed is False
    assert "eval_error" in outcome.reason


def test_syntax_error_is_fail() -> None:
    outcome = _outcome("len(content", {"content": "x"})

    assert outcome.passed is False
    assert "eval_error" in outcome.reason


def test_exception_never_leaks_out_of_verifier() -> None:
    # 异常必须被收敛成 fail，不能把 run 循环炸掉。
    outcome = _outcome("content + 1 > 0", {"content": "abc"})

    assert isinstance(outcome.passed, bool)
    assert outcome.passed is False


def test_runner_does_not_complete_an_undecidable_step(harness, make_runner) -> None:
    """端到端：工具成功 + 断言求值抛异常 → run failed，且分类为 assertion。"""

    from tests.v8_redteam.conftest import probe_tool

    spec, fn, _calls = probe_tool(name="probe", data={"content": "not-a-number"}, observations=("ok", "content"))
    runner = make_runner(harness, tools=[(spec, fn)])
    plan = Plan(
        steps=(
            Step(
                id="parse",
                tool="probe",
                verify=Verify("int(content) > 3", needs=("content",)),
            ),
        )
    )

    outcome = runner.start_and_run(plan)

    assert outcome.status == "failed"
    assert harness.world.runs[outcome.run_id].status == "failed"
    assert not harness.of_type(EventType.RUN_COMPLETED)
    verified = harness.of_type(EventType.STEP_VERIFIED)[-1]
    assert verified.payload["result"] == "fail"
    classified = harness.of_type(EventType.FAILURE_CLASSIFIED)[-1]
    assert classified.payload["class"] == FailureClass.ASSERTION.value
    # 求值异常不能把异常本身抛给调用方（进程级崩溃）。
    assert outcome.result is not None
    assert outcome.result.verdict.value == "failed"
