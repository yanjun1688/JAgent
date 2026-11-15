"""I1 · fail-closed：判不了必须判 fail，不能默认通过。

规格依据：
- docs/jagent-v8-architecture.md §4.8「求值不了（缺字段）按 fail 处理，理由写明
  evidence_missing，交失败分类器，不静默放过」；
- §8「step 状态 undetermined ... 求值不了按 fail 处理」；
- contracts.py 模块注释「判定只有 pass / fail，没有第三态」。
"""

from __future__ import annotations

from jagent.contracts import EventType, Plan, Step, StepOutcome, Verify
from jagent.harness.verify import Verifier


def _verify(expr: str, needs: tuple[str, ...], observations: dict):
    step = Step(id="s", tool="probe", verify=Verify(expr, needs=needs))
    return Verifier().verify(step, StepOutcome(ok=True, observations=observations))


def test_needs_declared_but_not_present_is_fail_not_pass() -> None:
    outcome = _verify("len(content) > 0", ("content",), {})

    assert outcome.passed is False
    assert outcome.reason.startswith("evidence_missing")
    assert "content" in outcome.reason
    assert outcome.checks and outcome.checks[0]["passed"] is False


def test_undeclared_name_in_expr_is_fail_even_without_needs() -> None:
    # needs 为空不能成为"表达式里随便引用一个不存在的名字也放过"的理由。
    outcome = _verify("len(entries) > 0", (), {})

    assert outcome.passed is False
    assert outcome.reason.startswith("evidence_missing")
    assert "entries" in outcome.reason


def test_partial_evidence_is_fail() -> None:
    # 三个观测只给了两个：剩下的那个必须被点名。
    outcome = _verify(
        "exit_code == 0 and 'ok' in stdout and done is True",
        ("exit_code", "stdout", "done"),
        {"exit_code": 0, "stdout": "ok"},
    )

    assert outcome.passed is False
    assert "done" in outcome.reason


def test_extra_observation_does_not_satisfy_declared_need() -> None:
    # 工具给了别的字段，不等于给了断言要的那一个。
    outcome = _verify("len(content) > 0", ("content",), {"other": "x", "ok": True})

    assert outcome.passed is False
    assert "content" in outcome.reason


def test_false_assertion_is_fail() -> None:
    outcome = _verify("len(entries) > 0", ("entries",), {"entries": []})

    assert outcome.passed is False
    assert outcome.reason.startswith("assertion_failed")
    assert outcome.checks[0]["passed"] is False


def test_missing_evidence_cannot_reach_completed_end_to_end(harness, make_runner) -> None:
    """端到端：工具成功、但断言要的观测没有 → run 必须是 failed。"""

    from tests.v8_redteam.conftest import probe_tool

    spec, fn, _calls = probe_tool(name="probe", data={"other": 1}, observations=("ok", "content"))
    runner = make_runner(harness, tools=[(spec, fn)])
    plan = Plan(
        steps=(
            Step(
                id="read",
                tool="probe",
                verify=Verify("len(content) > 0", needs=("content",)),
            ),
        )
    )

    outcome = runner.start_and_run(plan)

    assert outcome.status == "failed"
    assert harness.world.runs[outcome.run_id].status == "failed"
    verified = harness.of_type(EventType.STEP_VERIFIED)
    assert verified, "step.verified 必须存在，否则无从解释为什么失败"
    assert verified[-1].payload["result"] == "fail"
    assert "evidence_missing" in verified[-1].payload["reason"]
    assert not harness.of_type(EventType.RUN_COMPLETED)


def test_assertion_uses_only_declared_observation_names() -> None:
    """观测里存在同名但需要靠 needs 声明——这是规格里的硬约束。

    表达式可以引用的名字必须来自 observations；这里验证 Verifier 不把 Python
    内建名字当观测（``sorted`` 不在白名单里，应判 fail 而不是"调不到就跳过"）。
    """

    outcome = _verify("sorted(entries) == entries", ("entries",), {"entries": [1]})

    assert outcome.passed is False
    assert "eval_error" in outcome.reason
