"""T1 对账：advice.v8.2 的四条确定性问题（grounding §3 / §6 #1–#8）。"""

from __future__ import annotations

from jagent.contracts import Fallback
from jagent.gate.rules import AdviceGate

from .support import advice, bundle, cite, item, plan, req


def _rule_ids(verdict) -> list[str]:
    return [violation.rule_id for violation in verdict.violations]


# --------------------------------------------------------------------------- #
# #1 grounded 模式 refs 为空 -> no_citation
# --------------------------------------------------------------------------- #

def test_grounded_advice_without_refs_is_no_citation() -> None:
    verdict = AdviceGate().check(
        advice(body="只给结论，不列来源。"),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.no_citation"]
    assert verdict.mode == "grounded"
    assert verdict.fallback == Fallback(
        reason="ADVICE_NO_CITATION", detail=verdict.violations[0].detail
    )


def test_evidence_without_requirements_is_framework_not_grounded() -> None:
    """#18：空计划 / 无 evidence_requires => framework，跳过 T1，不触发 no_citation。"""

    verdict = AdviceGate().check(
        advice(body="只给通用框架，不给具体结论。"),
        bundle(item("s1", {"data.pe": "45"})),
        plan(),
    )

    assert verdict.passed is True
    assert verdict.violations == ()
    assert verdict.mode == "framework"


# --------------------------------------------------------------------------- #
# #2 引用 step_id 不存在 / #3 引用 field 为空值 -> citation_unresolved
# --------------------------------------------------------------------------- #

def test_citation_to_unknown_step_is_unresolved() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s9", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.citation_unresolved"]


def test_citation_to_empty_observation_is_unresolved() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(item("s1", {"stdout": "ok", "data.pe": ""})),
        plan(req("s1", "stdout")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.citation_unresolved"]
    assert verdict.mode == "grounded"


# --------------------------------------------------------------------------- #
# #4 数字不在被引用观测里 -> number_ungrounded
# --------------------------------------------------------------------------- #

def test_number_missing_from_the_cited_observation_is_ungrounded() -> None:
    verdict = AdviceGate().check(
        advice(body="当前 PE 为 52。", refs=(cite("s1", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.number_ungrounded"]
    assert "52" in verdict.violations[0].detail


def test_number_found_only_in_an_uncited_observation_is_ungrounded() -> None:
    verdict = AdviceGate().check(
        advice(body="当前 PE 为 52。", refs=(cite("s1", "data.pe"),)),
        bundle(
            item("s1", {"data.pe": "45"}),
            item("s2", {"stdout": "PE 52"}, seq=2),
        ),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.number_ungrounded"]


# --------------------------------------------------------------------------- #
# #5 数字在截断区外、artifact 中存在 -> 通过
# #6 数字在截断区外、无 artifact -> 同一条规则，reason=evidence_truncated
# --------------------------------------------------------------------------- #

def test_number_beyond_truncation_is_grounded_by_the_artifact() -> None:
    gate = AdviceGate(artifact_reader=lambda artifact_id: "完整观测：PE 52，成交额 68000")
    verdict = gate.check(
        advice(body="当前 PE 为 52。", refs=(cite("s1", "data.pe"),)),
        bundle(
            item(
                "s1",
                {"data.pe": "完整观测已截断……"},
                truncated=("data.pe",),
                artifact_ids=("a" * 64,),
            )
        ),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is True
    assert verdict.violations == ()
    assert verdict.mode == "grounded"


def test_number_beyond_truncation_without_artifact_is_evidence_truncated() -> None:
    verdict = AdviceGate().check(
        advice(body="当前 PE 为 52。", refs=(cite("s1", "data.pe"),)),
        bundle(
            item(
                "s1",
                {"data.pe": "完整观测已截断……"},
                truncated=("data.pe",),
                artifact_ids=(),
            )
        ),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.number_ungrounded"]
    assert "evidence_truncated" in verdict.violations[0].detail
    # evidence_truncated 是 reason，不是第五个 rule_id（D-005）
    assert "advice.evidence_truncated" not in _rule_ids(verdict)


# --------------------------------------------------------------------------- #
# #7 evidence_requires 少一项 -> evidence_incomplete + insufficient_evidence
# --------------------------------------------------------------------------- #

def test_missing_requirement_is_evidence_incomplete() -> None:
    verdict = AdviceGate().check(
        advice(body="PE 45。", refs=(cite("s1", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe"), req("s2", "stdout")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.evidence_incomplete"]
    assert verdict.mode == "insufficient_evidence"
    assert "s2.stdout" in verdict.violations[0].detail


# --------------------------------------------------------------------------- #
# #8 引用齐全、无未接地数字 -> 通过
# --------------------------------------------------------------------------- #

def test_clean_grounded_advice_passes() -> None:
    verdict = AdviceGate().check(
        advice(
            body="估值偏高，主要风险在需求端。",
            refs=(cite("s1", "data.pe"),),
            framework=("先核对口径，再看时间点。",),
        ),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is True
    assert verdict.violations == ()
    assert verdict.mode == "grounded"


# --------------------------------------------------------------------------- #
# 数字抽取/豁免写死：日期、序号、千分位/小数、单位换算豁免；自身结构计数不豁免
# --------------------------------------------------------------------------- #

def test_dates_ordinals_formatting_and_unit_conversion_are_exempt() -> None:
    verdict = AdviceGate().check(
        advice(
            body="2024-01-02 第 3 条：市值 1,234.56 元，约 6.8 万。",
            refs=(cite("s1", "stdout"),),
        ),
        bundle(item("s1", {"stdout": "市值 1234.56 元，约 68000 元"})),
        plan(req("s1", "stdout")),
    )

    assert verdict.passed is True, [violation.detail for violation in verdict.violations]


def test_self_referential_structure_count_is_not_exempt() -> None:
    verdict = AdviceGate().check(
        advice(body="以下 3 点建议：先看证据。", refs=(cite("s1", "data.pe"),)),
        bundle(item("s1", {"data.pe": "45"})),
        plan(req("s1", "data.pe")),
    )

    assert verdict.passed is False
    assert _rule_ids(verdict) == ["advice.number_ungrounded"]
    assert "3" in verdict.violations[0].detail
