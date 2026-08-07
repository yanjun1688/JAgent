"""Regression tests for Bug S1.1 — should_not_rerun must NOT read LLM task_state.

Constraint 4 (AGENTS.md v2.1): system enforcement must NOT depend on Agent
cooperation. Scheduling decisions (rerun or not) are driven ONLY by the trusted
ExecState state machine; TaskState is an LLM annotation for its own reference.

Locks in the v2.1 semantics:
  * should_not_rerun excludes SOFT_ERROR → soft-error steps are re-runnable
    WITHOUT the LLM having to mark them not_achieved
  * COMPLETED steps never re-run in place even when the LLM marks them
    not_achieved → redo requires a NEW step id
  * is_done (includes SOFT_ERROR) is decoupled from should_not_rerun
  * SOFT_ERROR results are NOT written to the idempotency cache → same-input
    re-runs actually re-execute the tool
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.storage.event_store import EventStore
from harness.tools.executor import ExecutionStatus, ToolExecutor
from harness.tools.file_op import FILE_OP_DEF, file_op_fn, reset_sandbox_root, set_sandbox_root
from harness.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _sandbox_isolation():
    """Isolate the module-global sandbox root (mirrors test_dag_self_heal)."""
    yield
    reset_sandbox_root()


# ── 1. should_not_rerun is a pure ExecState function (constraint 4) ─────────

_ALL_TASK_STATES = [s for s in TaskState]


@pytest.mark.parametrize("task_state", _ALL_TASK_STATES)
def test_soft_error_should_not_rerun_false_for_every_task_state(task_state):
    """SOFT_ERROR must be re-runnable regardless of the LLM's task_state.

    Pre-fix: task_state=NOT_ACHIEVED made it False but any other value made it
    True → rerun depended on LLM cooperation (constraint 4 violation).
    """
    sr = StepResult(step_id="s1", exec_state=ExecState.SOFT_ERROR, task_state=task_state)
    assert sr.should_not_rerun is False, f"task_state={task_state} must not gate SOFT_ERROR rerun"


@pytest.mark.parametrize("task_state", _ALL_TASK_STATES)
def test_completed_should_not_rerun_true_for_every_task_state(task_state):
    """COMPLETED must never re-run in place, even when LLM says not_achieved.

    Pre-fix: task_state=NOT_ACHIEVED flipped should_not_rerun to False → the
    completed step was re-executed (duplicate side effects).
    """
    sr = StepResult(step_id="s1", exec_state=ExecState.COMPLETED, task_state=task_state)
    assert sr.should_not_rerun is True, f"task_state={task_state} must not force a COMPLETED rerun"


@pytest.mark.parametrize("exec_state,expected", [
    (ExecState.COMPLETED, True),
    (ExecState.SOFT_ERROR, False),   # v2.1: re-runnable
    (ExecState.IDEMPOTENT, True),
    (ExecState.SKIPPED, True),
    (ExecState.CANCELLED, True),
    (ExecState.PENDING, False),
    (ExecState.RUNNING, False),
    (ExecState.FAILED, False),
])
def test_should_not_rerun_pure_exec_state_mapping(exec_state, expected):
    sr = StepResult(step_id="s1", exec_state=exec_state, task_state=TaskState.NOT_ACHIEVED)
    assert sr.should_not_rerun == expected


# ── 2. is_done is decoupled from should_not_rerun (includes SOFT_ERROR) ─────

def test_is_done_includes_soft_error():
    sr = StepResult(step_id="s1", exec_state=ExecState.SOFT_ERROR)
    assert sr.is_done is True


def test_is_done_independent_of_task_state():
    sr = StepResult(step_id="s1", exec_state=ExecState.SOFT_ERROR, task_state=TaskState.NOT_ACHIEVED)
    assert sr.is_done is True
    sr2 = StepResult(step_id="s1", exec_state=ExecState.COMPLETED, task_state=TaskState.NOT_ACHIEVED)
    assert sr2.is_done is True


@pytest.mark.parametrize("task_state", _ALL_TASK_STATES)
def test_is_done_soft_error_holds_across_task_states(task_state):
    sr = StepResult(step_id="s1", exec_state=ExecState.SOFT_ERROR, task_state=task_state)
    assert sr.is_done is True


def test_is_done_and_should_not_rerun_orthogonal():
    """SOFT_ERROR: done (usable upstream) but re-runnable. COMPLETED: both True."""
    soft = StepResult(step_id="s1", exec_state=ExecState.SOFT_ERROR)
    assert soft.is_done is True and soft.should_not_rerun is False

    done = StepResult(step_id="s2", exec_state=ExecState.COMPLETED)
    assert done.is_done is True and done.should_not_rerun is True

    failed = StepResult(step_id="s3", exec_state=ExecState.FAILED)
    assert failed.is_done is False and failed.should_not_rerun is False


# ── 3. SOFT_ERROR not cached in idempotency (self-heal must really re-run) ──

@pytest.mark.asyncio
async def test_soft_error_result_is_not_idempotency_cached():
    """Same-input SOFT_ERROR re-run must re-execute the tool, not hit cache.

    Pre-fix: the SOFT_ERROR TOOL_COMPLETED event was written with the
    idempotency key → the second identical call returned IDEMPOTENCY_HIT and
    the tool never actually re-ran → self-heal was impossible for same input.
    """
    store = EventStore(":memory:")
    await store.initialize()
    try:
        set_sandbox_root(str(ROOT))
        ex = ToolExecutor(store)
        step_input = {"operation": "read", "path": "nonexistent_file.xyz"}

        r1 = await ex.execute("run1", "file_op", step_input, FILE_OP_DEF, file_op_fn)
        assert r1.status == ExecutionStatus.COMPLETED
        assert r1.has_semantic_error is True

        r2 = await ex.execute("run1", "file_op", step_input, FILE_OP_DEF, file_op_fn)
        assert r2.status == ExecutionStatus.COMPLETED, f"expected re-execution, got {r2.status.value}"
        assert r2.has_semantic_error is True
        assert r2.cached is False, "SOFT_ERROR must not be served from the idempotency cache"
    finally:
        await store.close()


# ── 4. e2e: SOFT_ERROR self-heal works WITHOUT LLM cooperation ─────────────

async def _count_step_starts(store: EventStore, run_id: str) -> Counter:
    events = await store.get_events(run_id)
    counts: Counter = Counter()
    for e in events:
        if e.event_type.value == "DagStepStarted":
            counts[e.payload["step_id"]] += 1
    return counts


async def _build_engine():
    store = EventStore(":memory:")
    await store.initialize()
    set_sandbox_root(str(ROOT))
    ex = ToolExecutor(store)
    reg = ToolRegistry()
    reg.register(FILE_OP_DEF, file_op_fn)
    defs, fns = reg.list_tool_defs(), reg.list_tool_fns()
    return store, ex, reg, defs, fns


@pytest.mark.asyncio
async def test_soft_error_reruns_without_llm_not_achieved_marker():
    """Constraint 4 e2e: LLM omits step_tasks entirely, soft-error step still reruns.

    Pre-fix: s3 was NOT re-run (should_not_rerun stayed True because task_state
    stayed UNKNOWN) → self-heal silently failed. The run only 'completed' by
    accident via the exhausted-mock fall-through.
    """
    store, ex, reg, defs, fns = await _build_engine()
    try:
        plan1 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}},
                {"id": "s2", "tool": "file_op", "input": {"operation": "read", "path": "pyproject.toml"}, "depends_on": ["s1"]},
                {"id": "s3", "tool": "file_op", "input": {"operation": "read", "path": "nonexistent_file.xyz"}, "depends_on": ["s2"]},
                {"id": "s4", "tool": "file_op", "input": {"operation": "read", "path": "AGENTS.md"}, "depends_on": ["s3"]},
                {"id": "s5", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}, "depends_on": ["s4"]},
            ],
        })
        plan2 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s3", "tool": "file_op", "input": {"operation": "read", "path": "AGENTS.md"}},
                {"id": "s4", "tool": "file_op", "input": {"operation": "read", "path": "AGENTS.md"}, "depends_on": ["s3"]},
                {"id": "s5", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}, "depends_on": ["s4"]},
            ],
        })
        planner = Planner(
            MockLLMClient(responses=["yes", plan1, plan2, "answer"]),
            reg, store, max_plan_retries=2,
        )
        dag = DagExecutor(ex, store, reg)
        sched = PlanningExecutorScheduler(
            store, ex, planner, dag, defs, fns,
            config=SchedulerConfig(max_iterations=10),
        )
        state = await sched.run("no_coop_heal", "复现")
        counts = await _count_step_starts(store, "no_coop_heal")
        assert state.status.value == "completed"
        assert counts["s3"] == 2, f"soft-error step must rerun WITHOUT LLM marker: {dict(counts)}"
        assert counts["s1"] == 1, f"s1 re-executed: {dict(counts)}"
        assert counts["s2"] == 1, f"s2 re-executed: {dict(counts)}"
        assert counts["s4"] == 1, f"s4 re-executed: {dict(counts)}"
        assert counts["s5"] == 1, f"s5 re-executed: {dict(counts)}"
    finally:
        await store.close()


# ── 5. e2e: COMPLETED step marked not_achieved does NOT re-run in place ─────

@pytest.mark.asyncio
async def test_completed_step_not_achieved_does_not_rerun_in_place():
    """New-id contract e2e: LLM reuses completed s1 id AND marks it not_achieved.

    Pre-fix: s1 was re-executed in place (task_state flipped should_not_rerun).
    Post-fix: system skips completed s1 → only s3 runs. Redo requires a new id.
    """
    store, ex, reg, defs, fns = await _build_engine()
    try:
        plan1 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}},
                {"id": "s2", "tool": "file_op", "input": {"operation": "read", "path": "nonexistent_file.xyz"}},
            ],
        })
        plan2 = json.dumps({
            "intent": "t",
            "steps": [
                {"id": "s1", "tool": "file_op", "input": {"operation": "read", "path": "README.md"}},
                {"id": "s3", "tool": "file_op", "input": {"operation": "read", "path": "pyproject.toml"}},
            ],
            "step_tasks": {"s1": "not_achieved", "s2": "waived"},
        })
        planner = Planner(
            MockLLMClient(responses=["yes", plan1, plan2, "answer"]),
            reg, store, max_plan_retries=2,
        )
        dag = DagExecutor(ex, store, reg)
        sched = PlanningExecutorScheduler(
            store, ex, planner, dag, defs, fns,
            config=SchedulerConfig(max_iterations=10),
        )
        state = await sched.run("completed_not_achieved", "复现")
        counts = await _count_step_starts(store, "completed_not_achieved")
        assert state.status.value == "completed"
        assert counts["s1"] == 1, f"completed s1 must NOT re-run in place: {dict(counts)}"
        assert counts["s2"] == 1, f"s2 re-executed: {dict(counts)}"
        assert counts["s3"] == 1, f"s3 should have run: {dict(counts)}"
    finally:
        await store.close()
