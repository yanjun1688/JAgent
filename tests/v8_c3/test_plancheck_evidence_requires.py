"""Wave 2 · C3 · plancheck 校验 evidence_requires（BUG3-06 / grounding §2.4 / D-016）。

这是 Planner 的**草稿校验**，不是计划门：不合法只让 Planner 重试，不产生
"计划被打回"的语义。这里只钉住确定性检查本身：``step_id`` 必须存在，
``field`` 必须是该步工具声明过的观测名。
"""

from __future__ import annotations

from jagent.contracts import EvidenceReq, Plan, Step, ToolSpec, Verify
from jagent.harness.plancheck import check_plan


def _spec() -> ToolSpec:
    return ToolSpec(
        name="fs.list",
        version="1",
        args_schema={},
        observations=("ok", "entries"),
    )


def _plan(*requires: EvidenceReq) -> Plan:
    return Plan(
        steps=(
            Step(
                id="s1",
                tool="fs.list",
                args={"path": "."},
                verify=Verify("ok == true", needs=("ok",)),
            ),
        ),
        evidence_requires=tuple(requires),
    )


def _codes(plan: Plan) -> list[str]:
    return [issue.code for issue in check_plan(plan, {"fs.list": _spec()})]


# --------------------------------------------------------------------------- #
# 1. 合法的 evidence_requires 不产生 issue
# --------------------------------------------------------------------------- #

def test_declared_step_and_field_are_accepted() -> None:
    plan = _plan(EvidenceReq(step_id="s1", field="ok"))

    assert _codes(plan) == []


# --------------------------------------------------------------------------- #
# 2. step_id 不存在：一条 issue
# --------------------------------------------------------------------------- #

def test_unknown_step_id_is_one_issue() -> None:
    plan = _plan(EvidenceReq(step_id="s9", field="ok"))

    issues = check_plan(plan, {"fs.list": _spec()})

    assert [issue.code for issue in issues] == ["EVIDENCE_STEP_UNKNOWN"]
    assert issues[0].step_id == "s9"
    assert "s9" in issues[0].detail


# --------------------------------------------------------------------------- #
# 3. field 未被该步工具声明：一条 issue
# --------------------------------------------------------------------------- #

def test_undeclared_field_is_one_issue() -> None:
    plan = _plan(EvidenceReq(step_id="s1", field="data.pe"))

    issues = check_plan(plan, {"fs.list": _spec()})

    assert [issue.code for issue in issues] == ["EVIDENCE_FIELD_UNDECLARED"]
    assert issues[0].step_id == "s1"
    assert "data.pe" in issues[0].detail
    assert "entries" in issues[0].detail  # 详情里列出真正声明的观测名


# --------------------------------------------------------------------------- #
# 4. 多处不合规：每项各报一条，不去重也不合并
# --------------------------------------------------------------------------- #

def test_each_bad_requirement_gets_its_own_issue() -> None:
    plan = _plan(
        EvidenceReq(step_id="s9", field="ok"),
        EvidenceReq(step_id="s1", field="data.pe"),
    )

    codes = _codes(plan)

    assert codes == ["EVIDENCE_STEP_UNKNOWN", "EVIDENCE_FIELD_UNDECLARED"]
