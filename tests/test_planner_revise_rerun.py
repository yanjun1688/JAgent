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
        {"s1"},  # v2.1: SOFT_ERROR excluded from executed ids (re-runnable)
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.FAILED)},
        {"s1"},
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.SOFT_ERROR),
         "s2": StepResult("s2", exec_state=ExecState.FAILED)},
        set(),  # v2.1: SOFT_ERROR + FAILED both re-runnable → nothing executed
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
    assert "replan=MAYBE" in text  # v2.1: SOFT_ERROR may be re-run


def test_build_dag_status_text_failed_replan_maybe():
    sr = StepResult("s1", exec_state=ExecState.FAILED, error="timeout")
    plan = DagPlan(intent="test intent", steps=[
        DagStep(id="s1", tool="http_request", input={}, description="step 1"),
    ])
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "replan=MAYBE" in text


def test_build_dag_status_text_includes_step_description():
    """Revise state must surface the step's business goal so the revise LLM
    can judge whether a soft-error step's task was actually met (tc-bds-02)."""
    sr = StepResult("s1", exec_state=ExecState.SOFT_ERROR, error="File not found")
    plan = DagPlan(intent="test intent", steps=[
        DagStep(id="s1", tool="file_op", input={},
                description="Read dataset.csv and return its real content"),
    ])
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "Task: Read dataset.csv and return its real content" in text
    assert "replan=MAYBE" in text


def test_build_dag_status_text_omits_empty_description():
    """Steps without a description must not render a stray Task line."""
    sr = StepResult("s1", exec_state=ExecState.SOFT_ERROR, error="File not found")
    plan = DagPlan(intent="test intent", steps=[
        DagStep(id="s1", tool="file_op", input={}, description=""),
    ])
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "Task:" not in text


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


# ── SOFT_ERROR 步骤输出可被 revise 计划引用（Bug 修复回归）────────

def test_plan_guardrail_allows_dep_on_soft_error_step():
    """revise 计划依赖 soft-error 步骤是合法的：其输出已记录（is_done），
    执行时 upstream 可解析，拓扑上作为 external 依赖（不产生调度边）。"""
    guardrail = PlanGuardrail(_make_registry_with_http())
    plan = DagPlan(
        intent="revise",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="")],
    )
    errors = guardrail.validate(
        plan,
        completed_step_ids=set(),
        available_step_ids={"s1"},
    )
    assert errors == []


def test_plan_guardrail_still_rejects_unknown_step():
    """没有 available_step_ids 时，依赖未知步骤仍被拒绝。"""
    guardrail = PlanGuardrail(_make_registry_with_http())
    plan = DagPlan(
        intent="revise",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="")],
    )
    errors = guardrail.validate(plan, completed_step_ids=set())
    assert len(errors) > 0
    assert "depends on unknown step" in errors[0]


def test_plan_guardrail_soft_error_dep_via_is_done_results():
    """从 StepResult 聚合的 available ids（is_done）应使 soft-error 依赖通过。"""
    results = {
        "s1": StepResult("s1", exec_state=ExecState.SOFT_ERROR,
                         output={"success": False, "error": "File not found"}),
    }
    completed = {
        sid for sid, r in results.items()
        if isinstance(r, StepResult) and r.should_not_rerun
    }
    available = {
        sid for sid, r in results.items()
        if isinstance(r, StepResult) and r.is_done
    }
    assert completed == set()
    assert available == {"s1"}

    guardrail = PlanGuardrail(_make_registry_with_http())
    plan = DagPlan(
        intent="revise",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="")],
    )
    errors = guardrail.validate(
        plan, completed_step_ids=completed, available_step_ids=available,
    )
    assert errors == []


def test_topological_sort_accepts_external_soft_error_dep():
    """soft-error 步骤作为 external 依赖：合法且不产生调度边。"""
    plan = DagPlan(
        intent="revise",
        steps=[DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"], description="")],
    )
    layers = plan.topological_sort(completed_step_ids=set(), external_deps={"s1"})
    assert layers == [["s2"]]


def test_topological_sort_external_dep_in_plan_still_scheduled():
    """external_deps 中的 id 若同时是计划内步骤，仍正常调度（保留重跑）。"""
    plan = DagPlan(
        intent="revise",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids=set(), external_deps={"s1"})
    assert layers == [["s1"], ["s2"]]


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
    assert sr.should_not_rerun is False  # v2.1: SOFT_ERROR is re-runnable


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


# ── Self-heal 拓扑跳过已完成的 in-plan 步骤（Bug 修复回归）──────────────

def test_topological_sort_skips_completed_in_plan_step():
    """Revise 复用相同 step id 时，已完成的步骤必须从拓扑层中移除。

    修复前：completed_step_ids={'s1'} 但 s1 仍在 plan.steps 中时，
    Kahn 队列仍包含 s1 → self-heal 后 s1 被重复执行。
    """
    plan = DagPlan(
        intent="self-heal",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
            DagStep(id="s3", tool="file_op", input={}, depends_on=["s2"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"], ["s3"]]


def test_topological_sort_completed_in_first_layer():
    """已完成的步骤与未完成步骤同层时，只调度未完成部分。"""
    plan = DagPlan(
        intent="parallel-self-heal",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}),
            DagStep(id="s3", tool="file_op", input={}, depends_on=["s1", "s2"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"], ["s3"]]


def test_topological_sort_dep_on_completed_skips_dependency_count():
    """步骤依赖已完成步骤时，in_degree 不应计该依赖。"""
    plan = DagPlan(
        intent="dep-completed",
        steps=[
            DagStep(id="s1", tool="file_op", input={}),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"]]


def test_topological_sort_completed_in_plan_cycle_detection_still_works():
    """completed 步骤从环中移除后，剩余步骤应正常调度（无误报环）。

    环 s1↔s2 中 s1 已完成 → 依赖关系解除，s2 可执行。
    """
    plan = DagPlan(
        intent="cycle",
        steps=[
            DagStep(id="s1", tool="file_op", input={}, depends_on=["s2"]),
            DagStep(id="s2", tool="file_op", input={}, depends_on=["s1"]),
        ],
    )
    layers = plan.topological_sort(completed_step_ids={"s1"})
    assert layers == [["s2"]]
