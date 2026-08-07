"""Typed results for DAG step execution.

Replaces the opaque dict[str, Any] contract with explicit types.

v2.1 S1: Introduces orthogonal ExecState (tool execution state, system-managed)
and TaskState (task achievement state, LLM-managed) enums to separate concerns
that were conflated in the old StepStatus enum.

v2.1 S1.1 (Bug fix): should_not_rerun / is_done are pure ExecState functions and
MUST NOT read task_state — system enforcement must not depend on Agent output
(AGENTS.md constraint 4). task_state is an LLM annotation only.

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
      - task_state (LLM 写入):  业务目标达成状态 — v2.1 起为纯注解，仅供 LLM 参考，
        不进入 should_not_rerun / is_done 等任何受信组件判定
      - should_not_rerun 属性:  纯函数 — 该 step 的工具是否已执行过（不含 SOFT_ERROR）
      - is_done 属性:           纯函数 — 是否已收尾（含 SOFT_ERROR），与 should_not_rerun 解耦
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

        v2.1 受信边界修正（Bug S1.1）: 纯 ExecState 状态机，**不读取 task_state**
        （AGENTS.md 约束 4：系统强制不依赖 Agent 配合）。SOFT_ERROR 不在其中
        → 可重跑（自愈依赖此，重跑权由 LLM 在 revise 中表达，系统只提供"允许"）。
        COMPLETED 不可原地重跑 — 要重做须新建 step id。

        True for: COMPLETED, IDEMPOTENT, SKIPPED, CANCELLED
        False for: SOFT_ERROR, PENDING, RUNNING, FAILED
        """
        return self.exec_state in (
            ExecState.COMPLETED, ExecState.IDEMPOTENT,
            ExecState.SKIPPED, ExecState.CANCELLED,
        )

    @property
    def is_completed(self) -> bool:
        return self.exec_state == ExecState.COMPLETED

    @property
    def is_done(self) -> bool:
        """该 step 是否已"收尾"（可提供上游上下文 / 不构成层失败）。

        v2.1 修正: 与 should_not_rerun **解耦**。SOFT_ERROR 属于"已收尾"
        （工具跑过了，output 可给下游用），但可以重跑。
        """
        return self.exec_state in (
            ExecState.COMPLETED, ExecState.SOFT_ERROR, ExecState.IDEMPOTENT,
            ExecState.SKIPPED, ExecState.CANCELLED,
        )

    @property
    def is_failed(self) -> bool:
        return self.exec_state == ExecState.FAILED

    @property
    def needs_confirmation(self) -> bool:
        return self.confirmation_id is not None

    @property
    def has_soft_error(self) -> bool:
        return self.exec_state == ExecState.SOFT_ERROR
