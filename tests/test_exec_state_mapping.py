"""Unit tests for ExecState/TaskState enums and should_not_rerun mapping (S1).

Covers tc-srr-01 through tc-srr-10, tc-ctr-01, tc-ctr-02, tc-inj-02
from TestPlan-S1.
"""

import pytest
from harness.core.dag_types import (
    ExecState,
    TaskState,
    StepResult,
)


# ── 2.1 should_not_rerun mapping (tc-srr-01 ~ tc-srr-08) ──────────


@pytest.mark.parametrize(
    "exec_state,expected",
    [
        (ExecState.COMPLETED, True),  # tc-srr-01
        (ExecState.UNSUCCESSFUL, False),  # tc-srr-02 (v2.1: re-runnable)
        (ExecState.IDEMPOTENT, True),  # tc-srr-03
        (ExecState.SKIPPED, False),  # tc-srr-04 (v2.2 D9: 门控跳过, 工具未执行 → 可重跑)
        (ExecState.CANCELLED, True),  # tc-srr-05
        (ExecState.PENDING, False),  # tc-srr-06
        (ExecState.RUNNING, False),  # tc-srr-07
        (ExecState.FAILED, False),  # tc-srr-08
    ],
)
def test_should_not_rerun_mapping_direct(exec_state, expected):
    sr = StepResult(step_id="s1", exec_state=exec_state)
    assert sr.should_not_rerun == expected


# ── 2.2 ExecState default (tc-srr-09) ────────────────────────────


def test_step_result_default_exec_state_is_pending():
    sr = StepResult(step_id="s1")
    assert sr.exec_state == ExecState.PENDING
    assert sr.should_not_rerun is False


# ── 2.3 TaskState default (tc-srr-10) ─────────────────────────────


def test_step_result_default_task_state_is_unknown():
    sr = StepResult(step_id="s1")
    assert sr.task_state == TaskState.UNKNOWN


# ── 6.1 Enum value stability (tc-ctr-01) ──────────────────────────


def test_exec_state_values_are_stable():
    expected = {"pending", "running", "completed", "unsuccessful", "failed", "skipped", "idempotent", "cancelled"}
    actual = {e.value for e in ExecState}
    assert actual == expected


def test_task_state_values_are_stable():
    expected = {"unknown", "achieved", "partial", "not_achieved", "waived"}
    actual = {e.value for e in TaskState}
    assert actual == expected


# ── 6.2 Properties (tc-ctr-02) ────────────────────────────────────


def test_step_result_properties_completed():
    sr = StepResult(step_id="s1", exec_state=ExecState.COMPLETED)
    assert sr.is_completed is True
    assert sr.output_available is True
    assert sr.is_failed is False
    assert sr.needs_confirmation is False
    assert sr.is_unsuccessful is False
    assert sr.should_not_rerun is True


def test_step_result_properties_unsuccessful():
    sr = StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL)
    assert sr.is_completed is False
    assert sr.output_available is True
    assert sr.is_failed is False
    assert sr.is_unsuccessful is True
    assert sr.should_not_rerun is False  # v2.1: UNSUCCESSFUL is re-runnable


def test_step_result_properties_failed():
    sr = StepResult(step_id="s1", exec_state=ExecState.FAILED)
    assert sr.is_completed is False
    assert sr.output_available is False
    assert sr.is_failed is True
    assert sr.should_not_rerun is False


def test_step_result_properties_pending():
    sr = StepResult(step_id="s1", exec_state=ExecState.PENDING)
    assert sr.is_completed is False
    assert sr.output_available is False
    assert sr.is_failed is False
    assert sr.should_not_rerun is False


def test_step_result_needs_confirmation_by_id():
    sr = StepResult(step_id="s1", exec_state=ExecState.PENDING, confirmation_id="cid-1")
    assert sr.needs_confirmation is True


# ── 7.2 should_not_rerun is pure no I/O (tc-inj-02) ──────────────


def test_should_not_rerun_is_pure_no_io():
    sr = StepResult(step_id="s1", exec_state=ExecState.COMPLETED)
    result = sr.should_not_rerun
    assert result is True


# ── Additional: should_not_rerun with explicit exec_state ────────


def test_should_not_rerun_failed_exec_state():
    sr = StepResult(step_id="s1", exec_state=ExecState.FAILED)
    assert sr.exec_state == ExecState.FAILED
    assert sr.should_not_rerun is False


# ── Additional: TaskState explicit set ────────────────────────────


def test_task_state_explicit():
    sr = StepResult(step_id="s1", exec_state=ExecState.COMPLETED, task_state=TaskState.ACHIEVED)
    assert sr.task_state == TaskState.ACHIEVED


# ── Additional: all ExecState enum values ────────────────────────


def test_exec_state_all_values():
    values = [e.value for e in ExecState]
    assert len(values) == 8
    assert "pending" in values
    assert "completed" in values
    assert "idempotent" in values
    assert "skipped" in values
    assert "cancelled" in values


# ── Additional: all TaskState enum values ─────────────────────────


def test_task_state_all_values():
    values = [e.value for e in TaskState]
    assert len(values) == 5
    assert "unknown" in values
    assert "achieved" in values
    assert "partial" in values
    assert "not_achieved" in values
    assert "waived" in values
