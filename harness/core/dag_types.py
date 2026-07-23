"""Typed results for DAG step execution.

Replaces the opaque dict[str, Any] contract with explicit types.

v2.1 S1: Introduces orthogonal ExecState (tool execution state, system-managed)
and TaskState (task achievement state, LLM-managed) enums to separate concerns
that were conflated in the old StepStatus enum.

See ADR-007 / PRD_S1 for full design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecState(str, Enum):
    """工具执行态 — 由 DagExecutor / Tool Layer 写入，LLM 只读。

    回答: "这个 step 的工具调用现在处于什么执行阶段？"
    """
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    SOFT_ERROR    = "soft_error"
    FAILED        = "failed"
    SKIPPED       = "skipped"
    IDEMPOTENT    = "idempotent"
    CANCELLED     = "cancelled"


class TaskState(str, Enum):
    """任务达成态 — 由 LLM 在 revise() 中判定。

    回答: "这个 step 的业务目标达成了吗？"
    """
    UNKNOWN       = "unknown"
    ACHIEVED      = "achieved"
    PARTIAL       = "partial"
    NOT_ACHIEVED  = "not_achieved"
    WAIVED        = "waived"


@dataclass
class StepResult:
    """Result of executing a single DAG step.

    New in V0.7.1 (S1):
      - exec_state (系统写入): 工具调用执行状态 — required, no default
      - task_state (LLM 写入):  业务目标达成状态
      - should_not_rerun 属性:  纯函数 — 该 step 的工具是否已执行过
    """

    step_id: str
    exec_state: ExecState = field(default=ExecState.PENDING)
    output: Any = None
    summary: str = ""
    error: str | None = None
    retryable: bool = False
    confirmation_id: str | None = None
    task_state: TaskState = field(default=TaskState.UNKNOWN)

    @property
    def should_not_rerun(self) -> bool:
        """该 step 的工具是否已执行过（不应再次调度）。

        注意: 这不等于"任务目标已达成"，只是"工具已经跑过了"。

        True for: COMPLETED, SOFT_ERROR, IDEMPOTENT, SKIPPED, CANCELLED
        False for: PENDING, RUNNING, FAILED
        """
        return self.exec_state in (
            ExecState.COMPLETED, ExecState.SOFT_ERROR, ExecState.IDEMPOTENT,
            ExecState.SKIPPED, ExecState.CANCELLED,
        ) and self.task_state != TaskState.NOT_ACHIEVED

    @property
    def is_completed(self) -> bool:
        return self.exec_state == ExecState.COMPLETED

    @property
    def is_done(self) -> bool:
        return self.exec_state in (
            ExecState.COMPLETED, ExecState.SOFT_ERROR, ExecState.IDEMPOTENT,
            ExecState.SKIPPED, ExecState.CANCELLED,
        ) and self.task_state != TaskState.NOT_ACHIEVED

    @property
    def is_failed(self) -> bool:
        return self.exec_state == ExecState.FAILED

    @property
    def needs_confirmation(self) -> bool:
        return self.confirmation_id is not None

    @property
    def has_soft_error(self) -> bool:
        return self.exec_state == ExecState.SOFT_ERROR
