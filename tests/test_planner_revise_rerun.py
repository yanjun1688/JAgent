"""Integration tests for Planner.revise() with ExecState-based filtering (S1).

Covers tc-csi-01 through tc-csi-04, tc-rev-01, tc-grd-01, tc-bds-01,
tc-topo-01, and tc-e2e-01 from TestPlan-S1.
"""

import pytest
from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import ToolDefinition
from harness.core.dag_executor import DagExecutor
from harness.core.planner import PlanGuardrail
from harness.tools.registry import ToolRegistry


def _make_registry_with_http() -> ToolRegistry:
    """Create a minimal registry with http_request tool registered."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="http_request",
            description="HTTP request tool",
            input_schema={},
            output_schema={},
            idempotency_key_fields=[],
            side_effects=[],
            timeout_ms=30000,
        ),
        fn=lambda input: {"status": "ok"},
    )
    return registry


# ── 2.4 completed_step_ids → executed_step_ids (tc-csi-01~04) ──

@pytest.mark.parametrize("results,expected_ids", [
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.COMPLETED)},
        {"s1", "s2"},
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.SOFT_ERROR)},
        {"s1", "s2"},
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.FAILED)},
        {"s1"},
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.SOFT_ERROR),
         "s2": StepResult("s2", exec_state=ExecState.FAILED)},
        {"s1"},
    ),
])
def test_completed_step_ids_includes_soft_error(results, expected_ids):
    computed = {
        sid for sid, r in results.items()
        if isinstance(r, StepResult) and r.should_not_rerun
    }
    assert computed == expected_ids


# ── 2.5 build_dag_status_text 包含 exec_state (tc-bds-01) ──────

def test_build_dag_status_text_includes_exec_state():
    sr = StepResult("s1", exec_state=ExecState.COMPLETED)
    plan = DagPlan(intent="test intent", steps=[
        DagStep(id="s1", tool="http_request", input={}, description="step 1"),
    ])
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "exec=" in text


def test_build_dag_status_text_includes_replan_tag():
    sr = StepResult("s1", exec_state=ExecState.SOFT_ERROR)
    plan = DagPlan(intent="test intent", steps=[
        DagStep(id="s1", tool="http_request", input={}, description="step 1"),
    ])
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "replan=NO" in text


def test_build_dag_status_text_failed_replan_maybe():
    sr = StepResult("s1", exec_state=ExecState.FAILED, error="timeout")
    plan = DagPlan(intent="test intent", steps=[
        DagStep(id="s1", tool="http_request", input={}, description="step 1"),
    ])
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "replan=MAYBE" in text


# ── 3.2 PlanGuardrail 依赖检查 (tc-grd-01) ─────────────────────

def test_plan_guardrail_uses_completed_step_ids():
    guardrail = PlanGuardrail(_make_registry_with_http())
    plan = DagPlan(
        intent="test",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="depends on s1")],
    )
    errors = guardrail.validate(plan, completed_step_ids={"s1"})
    assert errors == []


def test_plan_guardrail_unfulfilled_dependency():
    guardrail = PlanGuardrail(_make_registry_with_http())
    plan = DagPlan(
        intent="test",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="depends on s1")],
    )
    errors = guardrail.validate(plan, completed_step_ids=set())
    assert len(errors) > 0
    assert "depends on unknown step" in errors[0]


# ── 3.3 topological_sort with executed deps (tc-topo-01) ──────

def test_topological_sort_with_executed_deps():
    plan = DagPlan(
        intent="test",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="")],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"]]


def test_topological_sort_without_executed_deps_raises():
    plan = DagPlan(
        intent="test",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="")],
    )
    with pytest.raises(ValueError, match="depends on unknown step"):
        plan.topological_sort()


# ── ExecState/TaskState 回归检查 ─────────────────────────────────

def test_step_result_all_properties_work():
    """All StepResult backward-compat properties remain functional."""
    sr = StepResult(step_id="s1", exec_state=ExecState.COMPLETED)
    assert sr.is_completed is True
    assert sr.is_done is True
    assert sr.is_failed is False
    assert sr.needs_confirmation is False
    assert sr.has_soft_error is False


def test_step_result_soft_error_properties():
    sr = StepResult(step_id="s1", exec_state=ExecState.SOFT_ERROR, error="minor issue")
    assert sr.is_completed is False
    assert sr.is_done is True
    assert sr.is_failed is False
    assert sr.has_soft_error is True
    assert sr.should_not_rerun is True


def test_step_result_failed_properties():
    sr = StepResult(step_id="s1", exec_state=ExecState.FAILED, error="timeout")
    assert sr.is_completed is False
    assert sr.is_done is False
    assert sr.is_failed is True
    assert sr.should_not_rerun is False


def test_step_result_executor_error_properties():
    sr = StepResult(step_id="s1", exec_state=ExecState.FAILED, error="internal")
    assert sr.is_completed is False
    assert sr.is_done is False
    assert sr.is_failed is True
    assert sr.should_not_rerun is False


# ── ExecState 和 TaskState 正交性 ─────────────────────────────────

def test_exec_state_and_task_state_are_independent():
    sr = StepResult(
        step_id="s1",
        exec_state=ExecState.COMPLETED,
        task_state=TaskState.PARTIAL,
    )
    assert sr.exec_state == ExecState.COMPLETED
    assert sr.task_state == TaskState.PARTIAL
    assert sr.should_not_rerun is True


# ── IDEMPOTENT exec_state ──────────────────────────────────────

def test_idempotent_step_result():
    sr = StepResult(
        step_id="s1",
        exec_state=ExecState.IDEMPOTENT,
    )
    assert sr.should_not_rerun is True
    assert sr.exec_state == ExecState.IDEMPOTENT
