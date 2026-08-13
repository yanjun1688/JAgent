"""Typed results for DAG step execution.

Replaces the opaque dict[str, Any] contract with explicit types.

v2.1 S1: Introduces orthogonal ExecState (tool execution state, system-managed)
and TaskState (task achievement state, LLM-managed) enums to separate concerns
that were conflated in the old StepStatus enum.

v2.1 S1.1 (Bug fix): should_not_rerun / is_done are pure ExecState functions and
MUST NOT read task_state — system enforcement must not depend on Agent output
(AGENTS.md constraint 4). task_state is an LLM annotation only.

v2.2 S1.2 (Phase A): the former "soft error" state was renamed to UNSUCCESSFUL.
Later phases (B/E) add step_normal, output_available (is_done narrowed) and
probe — see ADR-007 §0.2 / TDD_S1 §0.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecState(str, Enum):
    """工具执行态 — 由 DagExecutor / Tool Layer 写入，LLM 只读。

    回答: "这个 step 的工具调用现在处于什么执行阶段？"

    v2.2: UNSUCCESSFUL (former "soft error") — "跑了但没拿到东西"（不是"错误的一种"）。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    UNSUCCESSFUL = "unsuccessful"
    FAILED = "failed"
    SKIPPED = "skipped"
    IDEMPOTENT = "idempotent"
    CANCELLED = "cancelled"


class TaskState(str, Enum):
    """任务达成态 — 由 LLM 在 revise() 中判定。

    回答: "这个 step 的业务目标达成了吗？"

    v2.1 起: 纯审计便签，不参与任何受信判定（AGENTS.md 约束 4）。
    [FUTURE] 计划作为 "LLM 自评 vs 系统机械判定" 差异对比功能的素材，
    与 StepResult.step_normal 并排展示；当前只落事件供审计，不实现对照逻辑。
    """

    UNKNOWN = "unknown"
    ACHIEVED = "achieved"
    PARTIAL = "partial"
    NOT_ACHIEVED = "not_achieved"
    WAIVED = "waived"


@dataclass
class StepResult:
    """Result of executing a single DAG step.

    New in V0.7.1 (S1):
      - exec_state (系统写入): 工具调用执行状态 — required, no default
      - task_state (LLM 写入):  业务目标达成状态 — v2.1 起为纯审计便签，仅供审计，
        不进入 step_normal / output_available / 完成门等任何受信组件判定
      - should_not_rerun 属性:  纯函数 — 该 step 的工具是否已执行过（不含 UNSUCCESSFUL）
      - step_normal 属性 (v2.2 B): 纯函数 (exec_state, probe) → bool — 完成门原子判据
      - output_available 属性 (v2.2 B): 由 is_done 改名 — 仅 planner available_step_ids 用
      - probe 字段 (v2.2 B): 探测型步骤声明（E 阶段由 LLM 提供并经 PlanGuardrail 校验）
    """

    step_id: str
    exec_state: ExecState = field(default=ExecState.PENDING)
    output: Any = None
    summary: str = ""
    error: str | None = None
    retryable: bool = False
    confirmation_id: str | None = None
    task_state: TaskState = field(default=TaskState.UNKNOWN)
    probe: bool = field(default=False)
    tool_call_id: str | None = field(default=None)  # v2.2 (C, D6): step↔tool 挂钩

    @property
    def should_not_rerun(self) -> bool:
        """该 step 的工具是否已执行过（不应再次调度）。

        注意: 这不等于"任务目标已达成"，只是"工具已经跑过了"。

        v2.1 受信边界修正（Bug S1.1）: 纯 ExecState 状态机，**不读取 task_state**
        （AGENTS.md 约束 4：系统强制不依赖 Agent 配合）。UNSUCCESSFUL 不在其中
        → 可重跑（自愈依赖此，重跑权由 LLM 在 revise 中表达，系统只提供"允许"）。
        COMPLETED 不可原地重跑 — 要重做须新建 step id。

        v2.2 (D9 修订): SKIPPED 从其中移除 — SKIPPED 是因依赖非 normal 被门控跳过，
        **工具从未执行过**，revise 修复上游后应能重跑。CANCELLED 保留（被外部取消，
        不再执行）。

        True for: COMPLETED, IDEMPOTENT, CANCELLED
        False for: UNSUCCESSFUL, SKIPPED, PENDING, RUNNING, FAILED
        """
        return self.exec_state in (
            ExecState.COMPLETED,
            ExecState.IDEMPOTENT,
            ExecState.CANCELLED,
        )

    @property
    def is_completed(self) -> bool:
        return self.exec_state == ExecState.COMPLETED

    @property
    def step_normal(self) -> bool:
        """v2.2 (D3): 步骤是否"正常" — 完成门（任务完成聚合）的原子判据。

        纯函数 (exec_state, probe) → bool，系统计算，**不读取 task_state**（约束 4）。
        UNSUCCESSFUL 且 step.probe=True 时算正常（探测型步骤"没有/不存在"就是正确答案）。

        True for: COMPLETED, IDEMPOTENT, (UNSUCCESSFUL and probe)
        False for: UNSUCCESSFUL(非 probe), FAILED, SKIPPED, PENDING, RUNNING, CANCELLED
        """
        return self.exec_state in (
            ExecState.COMPLETED,
            ExecState.IDEMPOTENT,
        ) or (self.exec_state == ExecState.UNSUCCESSFUL and self.probe)

    @property
    def output_available(self) -> bool:
        """v2.2 (D8): 该 step 的输出是否可用（可被下游 `$var.field` 引用）。

        由 v2.1 的 is_done 改名而来。职责仅为"输出可用"，只用于 planner 的
        available_step_ids；完成计数 / 上游注入 / layer 失败检查一律改用 step_normal。

        v2.2 (P2): SKIPPED / CANCELLED 不在此列 — 两者没有实际产出，
        "输出可用"不应夸大（若下游引用其 `$var.field` 会在执行时解析为缺值）。
        """
        return self.exec_state in (
            ExecState.COMPLETED,
            ExecState.UNSUCCESSFUL,
            ExecState.IDEMPOTENT,
        )

    @property
    def is_failed(self) -> bool:
        return self.exec_state == ExecState.FAILED

    @property
    def needs_confirmation(self) -> bool:
        return self.confirmation_id is not None

    @property
    def is_unsuccessful(self) -> bool:
        return self.exec_state == ExecState.UNSUCCESSFUL
