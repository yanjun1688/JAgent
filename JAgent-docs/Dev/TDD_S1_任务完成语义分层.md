# TDD-S1: 任务完成语义与执行态正交分层 — 技术开发文档

| 属性 | 值 |
|---|---|
| **文档类型** | 技术开发文档 (TDD) |
| **版本** | 2.0 |
| **日期** | 2026-07-23 |
| **相关 ADR** | ADR-007 |
| **相关 PRD** | PRD_S1 |
| **目标版本** | V0.7.1 |

---

## 1. 前置说明

项目处于开发阶段，无生产环境、无用户。本方案的实现采用 **一次性替换** 策略：
直接删除旧 `StepStatus` 枚举和所有 backward-compat shim，以 `ExecState` + `TaskState` + `should_not_rerun` 替代。
不需要回滚开关、不需要保留旧字段、不需要分步迁移。

---

## 2. 架构分层约束

| 层级 | 本任务涉及的组件 | 前置依赖 |
|------|-----------------|----------|
| L4 — Agent Kernel | Planner / System Prompt | L1 Event Store, L2 Tool Layer |
| L5 — 工具注册与实现 | PlanGuardrail / DagExecutor / DagPlan | L2, L4 |

**受信边界**：`ExecState` 写入权归 `DagExecutor`（受信），`TaskState` 写入权归 `Planner`（非受信，LLM 决定）。`should_not_rerun` 是系统强制属性，纯函数 `ExecState → bool`，不依赖 Agent 任何输出。

---

## 3. 文件影响范围

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `harness/core/dag_types.py` | **重写** | 删除 `StepStatus`，新增 `ExecState`/`TaskState`；`StepResult` 改用 `exec_state` (取代 `status`)；新增 `should_not_rerun` 属性；删除 `get()` / `is_completed` / `is_done` / `is_failed` / `needs_confirmation` / `has_soft_error` |
| `harness/core/planner.py` | **修改** | `revise()` 改用 `should_not_rerun`；删除 `isinstance(r, dict)` 守卫和 `"idempotency_hit"` 死代码 |
| `harness/core/dag_executor.py` | **修改** | `_run_step` 返回 `StepResult` 时使用 `exec_state`；`build_dag_status_text` 输出 `exec_state`；全局 `is_done`/`is_completed` 引用替换为 `should_not_rerun` |
| `harness/core/system_prompt.py` | **修改** | revise prompt 增加 `ExecState`/`TaskState` 枚举说明 + few-shot 示例 |
| `harness/core/scheduler/plan.py` | **修改** | `_execute_plan` 中 `is_completed`/`has_soft_error` 替换为 `should_not_rerun` + `exec_state` 判断 |
| `harness/analysis/service.py` | **修改** | 分析 API 中 `step.status` → `step.exec_state` |
| 测试文件 | **修改+新增** | 现有测试中的 `StepStatus` 引用全部更新；新增 `tests/test_exec_state_mapping.py` |

---

## 4. 实现详情

### 4.1 dag_types.py — 完全重写

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecState(str, Enum):
    """工具执行态——由 DagExecutor 写入，LLM 只读。

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
    """任务达成态——由 LLM 在 revise 中写入。

    回答: "这个 step 的业务目标达成了吗？"
    """
    UNKNOWN       = "unknown"
    ACHIEVED      = "achieved"
    PARTIAL       = "partial"
    NOT_ACHIEVED  = "not_achieved"
    WAIVED        = "waived"


@dataclass
class StepResult:
    step_id: str
    exec_state: ExecState = ExecState.PENDING
    output: Any = None
    summary: str = ""
    error: str | None = None
    retryable: bool = False
    confirmation_id: str | None = None
    task_state: TaskState = TaskState.UNKNOWN

    @property
    def should_not_rerun(self) -> bool:
        """系统判定：该 step 的工具已执行过且不应再次调度。

        注意: 这不等于"任务目标已达成"——那由 LLM 通过 TaskState 判定。
        这里只回答"工具是否已经跑过了"。
        """
        return self.exec_state in (
            ExecState.COMPLETED,
            ExecState.SOFT_ERROR,
            ExecState.IDEMPOTENT,
            ExecState.SKIPPED,
            ExecState.CANCELLED,
        )
```

**删除物**：`StepStatus` 枚举、`StepResult.status` 字段、`StepResult.get()`。
**保留物**：`is_completed`、`is_done`、`is_failed`、`needs_confirmation`、`has_soft_error` 便捷属性 — 保持语义一致性，现由 `exec_state` 推导。

---

### 4.2 dag_executor.py — 直接写入 ExecState

`_run_step` 中（约 line 240-270），所有构造 `StepResult` 的地方：

| 旧代码 | 新代码 |
|--------|--------|
| `StepResult(step_id=sid, status=StepStatus.COMPLETED, output=..., summary=...)` | `StepResult(step_id=sid, exec_state=ExecState.COMPLETED, output=..., summary=...)` |
| `StepResult(step_id=sid, status=StepStatus.SOFT_ERROR, output=..., summary=..., error=...)` | `StepResult(step_id=sid, exec_state=ExecState.SOFT_ERROR, output=..., summary=..., error=...)` |
| `StepResult(step_id=sid, status=StepStatus.FAILED, error=...)` | `StepResult(step_id=sid, exec_state=ExecState.FAILED, error=...)` |
| `StepResult(step_id=sid, status=StepStatus.CONFIRMATION_NEEDED, confirmation_id=...)` | `StepResult(step_id=sid, exec_state=ExecState.PENDING, confirmation_id=...)` |
| `StepResult(step_id=sid, status=StepStatus.EXECUTOR_ERROR, error=...)` | `StepResult(step_id=sid, exec_state=ExecState.FAILED, error=...)` |

`dag_executor.py` 中其他引用替换：

| 旧 | 新 |
|----|-----|
| `r.is_done` | `r.should_not_rerun` |
| `r.is_completed` | `r.exec_state == ExecState.COMPLETED` |
| `r.has_soft_error` | `r.exec_state == ExecState.SOFT_ERROR` |
| `r.status` | `r.exec_state` |
| `StepStatus.` | `ExecState.` |

---

### 4.3 scheduler/plan.py — 引用替换

| 旧 | 新 |
|----|-----|
| `r.is_completed` | `r.exec_state == ExecState.COMPLETED` |
| `r.has_soft_error` | `r.exec_state == ExecState.SOFT_ERROR` |
| `results[sid].is_completed` | `results[sid].exec_state == ExecState.COMPLETED` |
| `isinstance(r, StepResult) and r.is_completed` | `isinstance(r, StepResult) and r.should_not_rerun` |

---

### 4.4 planner.py — 核心修复

#### 4.4.1 修正 `completed_step_ids` 计算（planner.py:274-277）

```python
# 旧代码（有 bug — isinstance(r, dict) 对 StepResult 返回 False）:
# completed_step_ids = {
#     sid for sid, r in results.items()
#     if isinstance(r, dict) and r.get("status") in ("completed", "idempotency_hit")
# }

# 新代码:
executed_step_ids: set[str] = {
    sid for sid, r in results.items()
    if isinstance(r, StepResult) and r.should_not_rerun
}
```

传播到 `PlanGuardrail.validate(revised, completed_step_ids=executed_step_ids)` 和 `plan.topological_sort(completed_step_ids=executed_step_ids)`。

#### 4.4.2 工具过滤逻辑（planner.py:427-439）

如果 `_filter_tools_by_intent` 中使用了 `StepResult.status` 或 `StepStatus`，替换为 `exec_state` 和 `ExecState`。

#### 4.4.3 删除死代码

- 删除 `"idempotency_hit"` 字面量（全局搜索确认无其他引用后删除）
- 删除 `isinstance(r, dict)` 形式的 StepResult 类型守卫

---

### 4.5 system_prompt.py — revise prompt 增强

在 `_REVISE_PROMPT` 中新增：

```
## Step Execution State (exec_state) — SYSTEM-GENERATED, READ-ONLY

The system reports each step's execution state. This tells you whether the tool ran,
NOT whether the goal was met. You must NOT modify these values.

| exec_state    | Meaning                                | should_not_rerun |
|---------------|----------------------------------------|------------------|
| "completed"   | Tool ran successfully                  | Yes              |
| "soft_error"  | Tool ran but returned a minor issue    | Yes              |
| "idempotent"  | Tool was skipped (duplicate detected)  | Yes              |
| "failed"      | Tool failed (timeout / exception)      | No               |
| "pending"     | Not yet executed                       | No               |

## Step Task State (task_state) — YOU MUST OUTPUT

For each step that has executed, output a task_state judgment in the plan JSON:

| task_state      | Meaning                                                       |
|-----------------|---------------------------------------------------------------|
| "achieved"      | Step's business goal was fully achieved                       |
| "partial"       | Step partially achieved its goal (may need supplementary)     |
| "not_achieved"  | Step did NOT achieve its goal (needs retry or new approach)   |
| "waived"        | Step's goal can be abandoned (unrecoverable or irrelevant)    |
| "unknown"       | Cannot determine (insufficient information)                   |

Always check the step's output before judging task_state.
A COMPLETED exec_state does NOT automatically mean task_state=achieved.
A SOFT_ERROR exec_state does NOT automatically mean task_state=not_achieved.
```

---

### 4.6 scheduler/plan.py + planner.py — revise context 构建

在 `planner.py` 的 `revise()` 中，`_build_revise_context` 增加 per-step 状态：

```python
def _build_revise_context(self, plan, results, system_state):
    lines = [system_state, ""]
    lines.append("## Per-Step Status\n")
    for step in plan.steps:
        r = results.get(step.id)
        if r is None:
            lines.append(f"- {step.id}: NOT EXECUTED (exec_state=pending)")
        else:
            lines.append(
                f"- {step.id}: exec_state={r.exec_state.value} "
                f"should_not_rerun={r.should_not_rerun} "
            )
            if isinstance(r.output, dict):
                lines.append(f"  output_keys: {list(r.output.keys())}")
            if r.error:
                lines.append(f"  error: {r.error[:200]}")
    return "\n".join(lines)
```

---

## 5. 全局搜索替换清单

实现前运行以下搜索，确保无遗漏引用：

```bash
rg "StepStatus"                  # 全部替换为 ExecState
rg "step\.status|\.status\s*="   # 全部替换为 .exec_state
rg "is_done|is_completed|has_soft_error|is_failed|needs_confirmation"  # 按 4.2-4.3 表替换
rg "\.get\(\"status\"\)"         # 替换为 .exec_state.value
rg "idempotency_hit"             # 确认无引用后删除
rg "isinstance\(r.*dict"         # 替换为 isinstance(r, StepResult)
```

---

## 6. 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 一次性替换而非分步迁移 | 直接删除旧枚举，全量替换 | 项目未上线，无用户，无兼容负担 |
| `SKIPPED` / `CANCELLED` 不新建事件类型 | 枚举值存在，暂不持久化 | DAG 执行中无对应事件类型；枚举存在为 `should_not_rerun` 提供完整规则。新建事件是后续工作 |
| `CONFIRMATION_NEEDED` 映射为 `PENDING` | `PENDING` + `confirmation_id` 非空 | 语义正确：等待确认 == 尚未执行 |
| `ExecState.RUNNING` 不持久化 | 事件流仅 `DagStepStarted → DagStepCompleted/Failed` | 无中间"running"事件；枚举值存在但不写入 Event Store |
| 不保留 backward-compat shim | `get()` / 旧属性全部删除 | 未上线项目没必要保留 |

---

## 7. 受信边界检查清单

- [x] `ExecState` 的写入路径全部在 `DagExecutor`（受信）内，无外部写入点
- [x] `TaskState` 只有 `Planner.revise()` 能写入，值仅来源于 LLM 输出
- [x] `should_not_rerun` 判定为纯函数 `ExecState → bool`，不依赖 LLM 输出
- [x] 不存在 Agent Kernel 直接驱动 DagExecutor 而绕过 `should_not_rerun` 的路径
- [x] 全局无 `StepStatus` / `.get("status")` / `"idempotency_hit"` 残留

---

## 8. 验收标准

- [x] `ExecState` 与 `TaskState` 完全正交，互不可推导
- [x] `StepResult.should_not_rerun` 对 COMPLETED / SOFT_ERROR / IDEMPOTENT / SKIPPED / CANCELLED → True
- [x] `StepResult.should_not_rerun` 对 PENDING / RUNNING / FAILED → False
- [x] `planner.revise()` 中 SOFT_ERROR 的 step 出现于 `executed_step_ids`（旧行为不含）
- [x] `planner.py` 中无 `isinstance(r, dict)` / `"idempotency_hit"` 残留
- [x] revise prompt 包含 `ExecState`/`TaskState` 定义表
- [x] `build_dag_status_text()` 输出 `exec_state` 信息
- [x] 全局 `StepStatus` grep 零匹配
- [x] `mypy harness/core/` 零错误
- [x] `ruff check harness/` 零警告
- [x] 全量测试通过

---

## 9. 相关文件

- 架构文档: `D:\Project\JAgent\JAgent-docs\Dev\ARCHITECTURE_v2.1.md` §3.7
- ADR: `D:\Project\JAgent\JAgent-docs\Prd\ADR-007_任务完成语义与执行态正交分层设计.md`
- PRD: `D:\Project\JAgent\JAgent-docs\Prd\PRD_S1_任务完成语义分层.md`
- 原始评审: `D:\Project\JAgent\JAgent-docs\Reviews\planner_protocol_gaps_review_20260722.md`
- 测试计划: `D:\Project\JAgent\JAgent-docs\Test_Plan\TestPlan_S1_任务完成语义分层.md`
