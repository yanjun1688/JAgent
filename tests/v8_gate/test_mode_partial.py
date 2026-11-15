"""mode 三态、证据缺失 fail-closed 与 partial 正交（grounding §3 / §4 / §6 #16–#18）。"""

from __future__ import annotations

from jagent.contracts import Fallback
from jagent.gate.rules import AdviceGate

from .support import RejectJudge, advice, bundle, cite, item, plan, req


# --------------------------------------------------------------------------- #
# mode 三态
# --------------------------------------------------------------------------- #

def test_framework_mode_when_evidence_is_none_and_t2_still_runs() -> None:
    judge = RejectJudge()
    verdict = AdviceGate(judge=judge).check(advice(body="只给通用框架。"))

    assert verdict.mode == "framework"
    assert verdict.passed is False, "T2 在 framework 模式照常运行"
    assert [violation.code for violation in verdict.violations] == ["policy_reject"]
    assert len(judge.calls) == 1


def test_insufficient_evidence_mode() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe"), req("s2", "stdout")),
    )

    assert verdict.mode == "insufficient_evidence"
    assert not verdict.passed


def test_grounded_mode_survives_a_t1_failure() -> None:
    verdict = AdviceGate().check(
        advice(body="只给结论。"),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.mode == "grounded", "证据齐但引用不接地仍是 grounded"
    assert not verdict.passed


# --------------------------------------------------------------------------- #
# B-01："没有证据包" != "没有证据要求"（I-G3 / fail-closed）
# --------------------------------------------------------------------------- #

def test_requires_without_an_evidence_bundle_is_insufficient_evidence() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        None,
        plan(req("s1", "data.pe")),
    )

    assert verdict.mode == "insufficient_evidence"
    assert verdict.passed is False
    assert [violation.rule_id for violation in verdict.violations] == [
        "advice.evidence_incomplete"
    ]
    assert verdict.fallback == Fallback(
        reason="evidence_incomplete", detail=verdict.violations[0].detail
    )
    assert "s1.data.pe" in verdict.violations[0].detail


def test_no_requirements_without_an_evidence_bundle_is_framework() -> None:
    verdict = AdviceGate().check(advice(body="只给通用框架。"), None, plan())

    assert verdict.mode == "framework"
    assert verdict.passed is True
    assert verdict.violations == ()


def test_no_requirements_with_evidence_present_is_still_framework() -> None:
    """#18：空计划不触发 no_citation（T1 在 framework 模式整体跳过）。"""

    verdict = AdviceGate().check(
        advice(body="只给通用框架。"),
        bundle(item("s1", {"data.pe": "45"})),
        plan(),
    )

    assert verdict.mode == "framework"
    assert verdict.passed is True


# --------------------------------------------------------------------------- #
# B-02：partial 只由 Step.required 决定，evidence_requires 不参与
# --------------------------------------------------------------------------- #

def test_partial_true_when_a_non_required_step_failed() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(
            item("s1", {"data.pe": "45"}),
            item("s2", {"stdout": ""}, seq=2, status="failed"),
        ),
        plan(req("s1", "data.pe"), non_required=("s2",)),
    )

    assert verdict.mode == "grounded", "partial 不改变 mode"
    assert verdict.partial is True
    assert verdict.passed is True


def test_required_step_failure_is_not_partial() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(
            item("s1", {"data.pe": "45"}),
            item("s2", {"stdout": ""}, seq=2, status="failed"),
        ),
        plan(req("s1", "data.pe")),  # s2 用默认 required=True
    )

    assert verdict.mode == "grounded"
    assert verdict.partial is False, "必需步失败不能被降格成 partial"


def test_partial_false_when_all_steps_are_ok() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(
            item("s1", {"data.pe": "45"}),
            item("s2", {"stdout": "ok"}, seq=2),
        ),
        plan(req("s1", "data.pe")),
    )

    assert verdict.partial is False
    assert verdict.passed is True


def test_partial_is_orthogonal_to_framework_mode() -> None:
    """framework 只说明"没有证据要求"，不说明"所有步都成功"。"""

    verdict = AdviceGate().check(
        advice(body="只给通用框架。"),
        bundle(
            item("s1", {"data.pe": "45"}),
            item("s2", {"stdout": ""}, seq=2, status="failed"),
        ),
        plan(non_required=("s2",)),
    )

    assert verdict.mode == "framework"
    assert verdict.partial is True


def test_partial_false_without_evidence() -> None:
    verdict = AdviceGate().check(advice(body="只给通用框架。"))

    assert verdict.mode == "framework"
    assert verdict.partial is False
