"""Wave 2 · S0 契约冻结的验收测试。

只测类型与纯函数，不测 gate / agent / session 的实现。
规格：grounding v3 §2、架构 v8.2 §4.3 / §4.4 / §4.6。
"""

from __future__ import annotations

import inspect

from jagent.contracts import (
    Advice,
    Citation,
    EvidenceBundle,
    EvidenceItem,
    EvidenceReq,
    Fallback,
    GateVerdict,
    IntentType,
    JudgeRecord,
    Plan,
    Step,
    Verify,
    Violation,
    plan_from_dict,
    plan_to_dict,
)
from jagent.evidence import missing
from jagent.ports import AdviceGatePort, ProposePort


def _item(
    step_id: str,
    observations: dict[str, str],
    *,
    seq: int = 1,
    status: str = "completed",
) -> EvidenceItem:
    return EvidenceItem(
        step_id=step_id,
        seq=seq,
        status=status,
        observations=observations,
        truncated=(),
        artifact_ids=(),
    )


def _plan(*requires: EvidenceReq) -> Plan:
    return Plan(
        steps=(Step(id="s1", tool="fs.read", verify=Verify("ok == true")),),
        then="advice",
        evidence_requires=requires,
    )


# --------------------------------------------------------------------------- #
# 1. plan_hash 纳入 evidence_requires（grounding §2.4）
# --------------------------------------------------------------------------- #

def test_plan_hash_changes_when_only_evidence_requires_differs() -> None:
    left = _plan(EvidenceReq(step_id="s1", field="data.pe"))
    right = _plan(EvidenceReq(step_id="s1", field="data.volume"))

    assert left.steps == right.steps
    assert left.then == right.then
    assert left.hash() != right.hash()


def test_plan_hash_is_stable_for_same_evidence_requires() -> None:
    first = _plan(EvidenceReq(step_id="s1", field="data.pe"))
    second = _plan(EvidenceReq(step_id="s1", field="data.pe"))

    assert first.hash() == second.hash()


def test_plan_serialization_keeps_evidence_requires() -> None:
    plan = _plan(EvidenceReq(step_id="s1", field="data.pe"))

    restored = plan_from_dict(plan_to_dict(plan))

    assert restored == plan
    assert restored.evidence_requires == (EvidenceReq(step_id="s1", field="data.pe"),)


# --------------------------------------------------------------------------- #
# 2. missing() 的四种组合（grounding §2.4）
# --------------------------------------------------------------------------- #

def test_missing_returns_empty_when_all_requirements_are_present() -> None:
    plan = _plan(
        EvidenceReq(step_id="s1", field="data.pe"),
        EvidenceReq(step_id="s2", field="stdout"),
    )
    evidence = EvidenceBundle(
        run_id="r1",
        items=(
            _item("s1", {"data.pe": "45"}),
            _item("s2", {"stdout": "ok"}),
        ),
    )

    assert missing(plan, evidence) == ()


def test_missing_returns_only_the_unmet_requirement() -> None:
    plan = _plan(
        EvidenceReq(step_id="s1", field="data.pe"),
        EvidenceReq(step_id="s2", field="stdout"),
    )
    evidence = EvidenceBundle(run_id="r1", items=(_item("s1", {"data.pe": "45"}),))

    assert missing(plan, evidence) == (EvidenceReq(step_id="s2", field="stdout"),)


def test_missing_without_evidence_returns_all_requirements() -> None:
    plan = _plan(
        EvidenceReq(step_id="s1", field="data.pe"),
        EvidenceReq(step_id="s2", field="stdout"),
    )

    assert missing(plan, None) == plan.evidence_requires


def test_missing_returns_empty_when_plan_has_no_requirements() -> None:
    plan = _plan()
    evidence = EvidenceBundle(run_id="r1", items=(_item("s1", {"data.pe": "45"}),))

    assert missing(plan, evidence) == ()


# --------------------------------------------------------------------------- #
# 3. Citation 形状；GateVerdict 向后兼容（grounding §2.1 / §4）
# --------------------------------------------------------------------------- #

def test_citation_seq_defaults_to_none() -> None:
    citation = Citation(step_id="s1", field="data.pe")

    assert citation.seq is None
    assert citation.label == ""


def test_gate_verdict_three_positional_arguments_still_work() -> None:
    violation = Violation(code="ADVICE_NO_RISK", rule_id="advice.no_risk", detail="x")
    verdict = GateVerdict("advice", False, (violation,))

    assert verdict.profile == "advice"
    assert verdict.passed is False
    assert verdict.violations == (violation,)
    # 新增字段全部有默认值，旧构造点不需要改
    assert verdict.ruleset == "advice.v8.1"
    assert verdict.mode == "framework"
    assert verdict.partial is False
    assert verdict.fallback is None
    assert verdict.citations == ()
    assert verdict.judge is None


def test_gate_verdict_extended_fields_hold_their_values() -> None:
    citation = Citation(step_id="s1", field="data.pe", label="行情", seq=7)
    fallback = Fallback(reason="advice.no_citation", detail="refs 为空")
    judge = JudgeRecord(verdict="UNDECIDABLE", reason="证据不足", model="judge-1")

    verdict = GateVerdict(
        "advice",
        False,
        (),
        mode="insufficient_evidence",
        partial=True,
        fallback=fallback,
        citations=(citation,),
        judge=judge,
    )

    assert verdict.mode == "insufficient_evidence"
    assert verdict.partial is True
    assert verdict.fallback == fallback
    assert verdict.citations == (citation,)
    assert verdict.judge == judge


def test_advice_refs_default_and_round_trip() -> None:
    citation = Citation(step_id="s1", field="data.pe")
    advice = Advice(body="框架", refs=(citation,))

    assert advice.refs == (citation,)
    assert advice.sources == ()
    assert Advice().refs == ()


# --------------------------------------------------------------------------- #
# 4. IntentType 成员名集合（D-011）
# --------------------------------------------------------------------------- #

def test_intent_type_has_exactly_the_frozen_members() -> None:
    assert {member.name for member in IntentType} == {"ADVISE", "EXECUTE", "GATHER"}
    assert IntentType.GATHER.value == "gather"
    assert len(IntentType) == 3


# --------------------------------------------------------------------------- #
# 5. 端口形状（架构 §4.3 / §4.4）
# --------------------------------------------------------------------------- #

def test_ports_freeze_the_wave2_signatures() -> None:
    check = inspect.signature(AdviceGatePort.check)
    assert list(check.parameters) == ["self", "advice", "evidence", "plan"]
    assert check.parameters["evidence"].default is None
    assert check.parameters["plan"].default is None

    compose = inspect.signature(ProposePort.compose)
    assert list(compose.parameters) == ["self", "slots", "text", "evidence", "missing", "then"]
    for name in ("slots", "text", "evidence", "missing", "then"):
        assert compose.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
