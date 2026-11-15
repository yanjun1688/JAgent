"""T2 评审接线：judge 只能否决、不能放行（grounding §3 T2 / §6 #9–#12 / #15）。"""

from __future__ import annotations

import pytest

from jagent.gate.rules import AdviceGate

from .support import (
    NoOpinionJudge,
    RejectJudge,
    UndecidableJudge,
    UnavailableJudge,
    advice,
    bundle,
    cite,
    item,
    plan,
    req,
)


def _grounded_check(gate: AdviceGate, *, body: str = "估值偏高，主要风险在需求端。", refs=None):
    return gate.check(
        advice(body=body, refs=refs if refs is not None else (cite("s1", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )


# --------------------------------------------------------------------------- #
# #9 judge 判 REJECT -> 兜底 policy_reject，理由只进 detail，不回传
# --------------------------------------------------------------------------- #

def test_judge_reject_falls_back_with_policy_reject() -> None:
    judge = RejectJudge()
    verdict = _grounded_check(AdviceGate(judge=judge))

    assert verdict.passed is False
    assert [violation.code for violation in verdict.violations] == ["policy_reject"]
    violation = verdict.violations[0]
    # policy_reject 是 code；rule_id 用 judge.*，不占用 advice.* 规则集
    assert violation.rule_id == "judge.policy"
    assert not violation.rule_id.startswith("advice.")
    assert verdict.fallback is not None
    assert verdict.fallback.reason == "policy_reject"
    assert verdict.judge is not None and verdict.judge.verdict == "reject"
    assert len(judge.calls) == 1


def test_judge_receives_claim_evidence_and_policy_context() -> None:
    judge = NoOpinionJudge()
    _grounded_check(AdviceGate(judge=judge), body="估值偏高。")

    request = judge.calls[0]
    assert request.claim == "估值偏高。"
    assert request.evidence["citations"][0]["step_id"] == "s1"
    assert request.evidence["citations"][0]["field"] == "data.pe"
    assert request.evidence["observations"] == {"s1.data.pe": "45"}
    assert request.context == {"policy_version": "1", "mode": "grounded"}
    assert request.reason == "judge.policy"


# --------------------------------------------------------------------------- #
# #10 UNDECIDABLE -> 放行 + judge record
# #11 调不通 -> 放行 + judge_unavailable
# --------------------------------------------------------------------------- #

def test_undecidable_judge_passes_and_is_recorded() -> None:
    verdict = _grounded_check(AdviceGate(judge=UndecidableJudge()))

    assert verdict.passed is True
    assert verdict.judge is not None
    assert verdict.judge.verdict == "undecidable"
    assert verdict.judge.model == "fake-judge"


def test_unavailable_judge_passes_and_is_marked() -> None:
    judge = UnavailableJudge()
    verdict = _grounded_check(AdviceGate(judge=judge))

    assert verdict.passed is True
    assert verdict.judge is not None
    assert verdict.judge.verdict == "unavailable"
    assert verdict.judge.reason == "judge_unavailable"
    assert judge.calls == 1


def test_judge_returning_none_passes_with_a_record() -> None:
    verdict = _grounded_check(AdviceGate(judge=NoOpinionJudge()))

    assert verdict.passed is True
    assert verdict.judge is not None
    assert verdict.judge.verdict == "undecidable"
    assert verdict.judge.reason == "no_opinion"


def test_gate_without_judge_records_none() -> None:
    verdict = _grounded_check(AdviceGate())

    assert verdict.passed is True
    assert verdict.judge is not None
    assert verdict.judge.verdict == "none"
    assert verdict.judge.reason == "judge_not_configured"


# --------------------------------------------------------------------------- #
# #12 judge 说 OK 但引用不存在 -> 仍拒绝；T1 失败时不调用 judge
# --------------------------------------------------------------------------- #

def test_judge_cannot_override_a_t1_failure() -> None:
    judge = NoOpinionJudge()
    verdict = AdviceGate(judge=judge).check(
        advice(body="PE 45。", refs=(cite("s9", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert [violation.rule_id for violation in verdict.violations] == [
        "advice.citation_unresolved"
    ]
    assert judge.calls == [], "T1 已失败时不应调用 judge"
    assert verdict.judge is None


def test_judge_is_not_called_when_evidence_is_incomplete() -> None:
    judge = NoOpinionJudge()
    verdict = AdviceGate(judge=judge).check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe"), req("s2", "stdout")),
    )

    assert verdict.passed is False
    assert verdict.mode == "insufficient_evidence"
    assert judge.calls == []


@pytest.mark.parametrize("state", ["none", "unavailable"])
def test_none_and_unavailable_never_change_a_t1_rejection(state: str) -> None:
    """judge 的缺席/故障都不能把 T1 的失败改成通过；T1 失败时 judge 不被问到。"""

    judge = None if state == "none" else UnavailableJudge()
    verdict = AdviceGate(judge=judge).check(
        advice(body="PE 45。", refs=(cite("s9", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert [violation.rule_id for violation in verdict.violations] == [
        "advice.citation_unresolved"
    ]
    # judge 未被调用：judge 记录缺席，而不是伪造一个"没有配置/调不通"的结论。
    assert verdict.judge is None
