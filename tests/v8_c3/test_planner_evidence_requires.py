"""Wave 2 · C3-fix · Planner 产出 evidence_requires（C3-02 / D-017）。

`I-G3` 要有生产方，`evidence_requires` 才不会在生产里永不可达。这里钉住：

* 模型输出里的 `evidence_requires` 由 `_build` 原样组装进 `Plan`；
* `plan_hash` 只在 `evidence_requires` 不同时也不同；
* 缺字段 / 空值 / 未知 step 不在 `_build` 里报新错，而是流到 plancheck 的草稿自检，
  以 `EVIDENCE_STEP_UNKNOWN` / `EVIDENCE_FIELD_UNDECLARED` 的 `PlanIssue` 出现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jagent.agent.llm_planner import LLMPlanner, PlanDraftError
from jagent.contracts import (
    EvidenceReq,
    IntentDecision,
    IntentType,
    Plan,
    ToolResult,
    ToolSpec,
)
from jagent.harness.registry import ToolRegistry
from jagent.ports import ModelReply

_MISSING = object()


class FakeModel:
    def __init__(self, text: str) -> None:
        self._text = text
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0) -> ModelReply:
        self.prompts.append(prompt)
        self.calls += 1
        return ModelReply(text=self._text)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    def fn(args: dict, workspace: Path, timeout: float) -> ToolResult:
        return ToolResult(ok=True)

    registry.add(
        ToolSpec(
            name="fs.list",
            version="1",
            args_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            observations=("ok", "entries"),
        ),
        fn,
    )
    return registry


def _plan_json(evidence_requires: object = _MISSING) -> str:
    payload: dict[str, object] = {
        "mode": "steps",
        "steps": [
            {
                "id": "s1",
                "tool": "fs.list",
                "args": {"path": "."},
                "depends_on": [],
                "verify": {"expr": "len(entries) > 0", "needs": ["entries"]},
            }
        ],
    }
    if evidence_requires is not _MISSING:
        payload["evidence_requires"] = evidence_requires
    return json.dumps(payload, ensure_ascii=False)


def _plan(model: FakeModel, registry: ToolRegistry) -> Plan:
    draft = LLMPlanner(model, registry).plan(
        decision=IntentDecision(type=IntentType.EXECUTE, confidence=0.9),
        slots={},
        tools=registry.descriptors(),
        text="列出目录并给出判断",
    )
    assert isinstance(draft, Plan)
    return draft


# --------------------------------------------------------------------------- #
# 1. 模型给出 evidence_requires -> Plan.evidence_requires
# --------------------------------------------------------------------------- #

def test_model_supplied_evidence_requires_is_built_into_the_plan() -> None:
    model = FakeModel(_plan_json([{"step_id": "s1", "field": "entries"}]))

    plan = _plan(model, _registry())

    assert plan.evidence_requires == (EvidenceReq(step_id="s1", field="entries"),)
    # prompt 的输出 schema 里要能看见这个字段，否则模型没有生产入口。
    prompt = model.prompts[0]
    assert "evidence_requires" in prompt
    assert "结论必须引用的观测，可空" in prompt


# --------------------------------------------------------------------------- #
# 2. plan_hash 纳入 evidence_requires
# --------------------------------------------------------------------------- #

def test_plan_hash_changes_when_only_evidence_requires_differs() -> None:
    registry = _registry()
    without = _plan(FakeModel(_plan_json()), registry)
    with_req = _plan(FakeModel(_plan_json([{"step_id": "s1", "field": "entries"}])), registry)

    assert without.steps == with_req.steps
    assert without.hash() != with_req.hash()


# --------------------------------------------------------------------------- #
# 3. 未知 step / 未声明 field -> plancheck issue（不是 _build 报错）
# --------------------------------------------------------------------------- #

def test_unknown_step_flows_to_plancheck() -> None:
    model = FakeModel(_plan_json([{"step_id": "s9", "field": "entries"}]))

    with pytest.raises(PlanDraftError) as caught:
        _plan(model, _registry())

    codes = [issue.code for issue in caught.value.issues]
    assert codes == ["EVIDENCE_STEP_UNKNOWN"]
    assert caught.value.issues[0].step_id == "s9"


def test_undeclared_field_flows_to_plancheck() -> None:
    model = FakeModel(_plan_json([{"step_id": "s1", "field": "data.pe"}]))

    with pytest.raises(PlanDraftError) as caught:
        _plan(model, _registry())

    codes = [issue.code for issue in caught.value.issues]
    assert codes == ["EVIDENCE_FIELD_UNDECLARED"]
    assert caught.value.issues[0].step_id == "s1"


# --------------------------------------------------------------------------- #
# 4. 形状坏掉也不在 _build 里抛新错：一律交给 plancheck
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        ([{"step_id": "s1"}], "EVIDENCE_FIELD_UNDECLARED"),  # 缺 field
        ([{"field": "entries"}], "EVIDENCE_STEP_UNKNOWN"),  # 缺 step_id
        (["not-an-object"], "EVIDENCE_STEP_UNKNOWN"),  # 条目不是对象
        ([{"step_id": "", "field": ""}], "EVIDENCE_STEP_UNKNOWN"),  # 空值
    ],
)
def test_malformed_entries_flow_to_plancheck_instead_of_a_build_error(
    raw: object, expected_code: str
) -> None:
    model = FakeModel(_plan_json(raw))

    with pytest.raises(PlanDraftError) as caught:
        _plan(model, _registry())

    codes = [issue.code for issue in caught.value.issues]
    assert codes == [expected_code]
    # 没有走 _parse_mode/_inspect_steps 的 INVALID_DRAFT 兜底：坏引用只由 plancheck 报。
    assert "INVALID_DRAFT" not in codes
