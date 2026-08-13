"""S03 — PlanGuardrail DAG 结构校验（validate_dag_structure 纯函数）测试。

覆盖问题四（非法 DAG 未在 Executor 前拦截）：step_id 唯一、依赖存在、
自依赖、环检测（含路径）、层级一致性、外部依赖不误报。纯函数，无 I/O。
"""

from __future__ import annotations

import pytest

from harness.core.planner import PlanGuardrail
from harness.models.plan import DagPlan, DagStep, validate_dag_structure
from harness.tools.registry import ToolRegistry


def _plan(*steps: DagStep) -> DagPlan:
    return DagPlan(intent="t", steps=list(steps))


# ── 唯一性 / 缺失 ─────────────────────────────────────────────────


def test_empty_plan_is_valid():
    assert validate_dag_structure(_plan()) == []


def test_single_step_is_valid():
    plan = _plan(DagStep(id="s1", tool="echo", input={}))
    assert validate_dag_structure(plan) == []


def test_duplicate_step_id_rejected():
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}),
        DagStep(id="s1", tool="echo", input={}),
    )
    errors = validate_dag_structure(plan)
    assert any("Duplicate step id 's1'" in e for e in errors)


def test_missing_step_id_rejected():
    plan = _plan(DagStep(id="", tool="echo", input={}))
    assert any("missing 'id'" in e for e in validate_dag_structure(plan))


# ── 依赖存在性 / 自依赖 ──────────────────────────────────────────


def test_depends_on_unknown_step_rejected():
    plan = _plan(DagStep(id="s1", tool="echo", input={}, depends_on=["ghost"]))
    assert any("depends on unknown step 'ghost'" in e for e in validate_dag_structure(plan))


def test_self_dependency_rejected():
    plan = _plan(DagStep(id="s1", tool="echo", input={}, depends_on=["s1"]))
    assert any("depends on itself" in e for e in validate_dag_structure(plan))


# ── 环检测 ───────────────────────────────────────────────────────


def test_simple_cycle_rejected():
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}, depends_on=["s2"]),
        DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
    )
    errors = validate_dag_structure(plan)
    assert any("Cycle detected" in e for e in errors)


def test_long_cycle_contains_path():
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}, depends_on=["s3"]),
        DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
        DagStep(id="s3", tool="echo", input={}, depends_on=["s2"]),
    )
    errors = validate_dag_structure(plan)
    cycle = [e for e in errors if "Cycle detected" in e]
    assert cycle, errors
    assert "s1" in cycle[0] and "s3" in cycle[0]


# ── 合法分层 / 层级一致性 ────────────────────────────────────────


def test_valid_hierarchy_no_errors():
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}),
        DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
        DagStep(id="s3", tool="echo", input={}, depends_on=["s2"]),
    )
    assert validate_dag_structure(plan) == []


def test_multi_dep_hierarchy_no_errors():
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}),
        DagStep(id="s2", tool="echo", input={}),
        DagStep(id="s3", tool="echo", input={}, depends_on=["s1", "s2"]),
    )
    assert validate_dag_structure(plan) == []


# ── 外部依赖（completed / available）不误报 ───────────────────────


def test_completed_step_external_dep_no_false_positive():
    plan = _plan(DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]))
    assert validate_dag_structure(plan, completed_step_ids={"s1"}) == []


def test_available_step_external_dep_no_false_positive():
    plan = _plan(DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]))
    assert validate_dag_structure(plan, available_step_ids={"s1"}) == []


def test_cycle_within_inplan_deps_still_detected_with_external():
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}, depends_on=["s2"]),
        DagStep(id="s2", tool="echo", input={}, depends_on=["s1", "ext"]),
    )
    errors = validate_dag_structure(plan, completed_step_ids={"ext"})
    assert any("Cycle detected" in e for e in errors)


# ── input 结构 ───────────────────────────────────────────────────


def test_non_dict_input_rejected():
    plan = _plan(DagStep.model_construct(id="s1", tool="echo", input="not-a-dict"))
    assert any("'input' must be an object" in e for e in validate_dag_structure(plan))


# ── 纯函数不抛 ValueError ────────────────────────────────────────


def test_never_raises_valueerror():
    bad = [
        _plan(
            DagStep(id="s1", tool="echo", input={}, depends_on=["s2"]),
            DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
        ),
        _plan(DagStep(id="s1", tool="echo", input={}, depends_on=["s1"])),
    ]
    for plan in bad:
        with pytest.raises(ValueError):
            plan.topological_sort()
        assert validate_dag_structure(plan)  # pure function returns errors instead


# ── PlanGuardrail 集成 ───────────────────────────────────────────


@pytest.fixture
def registry():
    from harness.models.tools import SideEffect, ToolDefinition

    r = ToolRegistry()
    r._register(ToolDefinition(name="echo", description="e", input_schema={}, side_effects=[]), lambda x: {"ok": True})
    r._register(
        ToolDefinition(
            name="file_op",
            description="f",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["read", "write"]},
                    "path": {"type": "string"},
                },
                "required": ["operation", "path"],
            },
            side_effects=[SideEffect.WRITE],
        ),
        lambda x: {"ok": True},
    )
    return r


def test_guardrail_rejects_cycle(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}, depends_on=["s2"]),
        DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
    )
    errors = PlanGuardrail(registry).validate(plan)
    assert any("Cycle detected" in e for e in errors)


def test_guardrail_rejects_duplicate(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={}),
        DagStep(id="s1", tool="echo", input={}),
    )
    errors = PlanGuardrail(registry).validate(plan)
    assert any("Duplicate step id" in e for e in errors)


def test_guardrail_external_dep_not_rejected(registry):
    plan = _plan(DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]))
    errors = PlanGuardrail(registry).validate(plan, completed_step_ids={"s1"})
    assert errors == []
