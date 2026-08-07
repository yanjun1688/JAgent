# TDD-S1: 任务完成语义与执行态正交分层 — 技术开发文档

| 属性 | 值 |
|---|---|
| **文档类型** | 技术开发文档 (TDD) |
| **版本** | 2.1 |
| **日期** | 2026-08-04 |
| **相关 ADR** | ADR-007 |
| **相关 PRD** | PRD_S1 |
| **目标版本** | V0.7.1 |

---

## 0. v2.1 修订记录（受信边界修正）

> **修订原因（Bug S1.1）**：v2.0 声称 `should_not_rerun` 是"纯函数 `ExecState → bool`，不依赖 Agent 输出"，
> 但实现中 `should_not_rerun` 还额外读取了 LLM 写入的 `task_state`：
>
> ```python
> # v2.0 实现（有 Bug）— 读 LLM 的 task_state，受信边界被打破
> should_not_rerun = exec_state in {COMPLETED, SOFT_ERROR, IDEMPOTENT, SKIPPED, CANCELLED} \
>                    and task_state != NOT_ACHIEVED
> ```
>
> **后果**：约束 4（系统强制不依赖 Agent 配合）被违反。若 LLM 忘记对 SOFT_ERROR 步骤标
> `not_achieved`，该步骤永远不会重跑 → 自愈静默失效；反之 COMPLETED 步骤被标 `not_achieved`
> 会被原地重跑 → 工具副作用重复执行。**调度决策权落在了 LLM 手里。**

### 0.1 修正决策

| 维度 | v2.0（错误） | v2.1（修正） |
|---|---|---|
| `should_not_rerun` | `ExecState` + 读 `task_state` | **纯 `ExecState` 状态机**，删除 `and task_state != NOT_ACHIEVED` |
| `should_not_rerun` 映射 | 含 SOFT_ERROR | **不含 SOFT_ERROR**（SOFT_ERROR 可重跑，重跑权归 LLM 在 revise 中表达，系统只提供"允许"） |
| `is_done` | 与 `should_not_rerun` 同义 | **解耦**：含 SOFT_ERROR（用于"层失败判定/上游上下文注入/完成计数"） |
| `task_state` | 参与调度决策 | **纯注解**：只供 `build_dag_status_text` 展示给 LLM 参考，不进入任何受信组件判定 |
| SOFT_ERROR 幂等缓存 | 以幂等键写入缓存 | **不入缓存**：同输入重跑必须真实再次执行工具，否则自愈形同虚设 |
| COMPLETED 步骤重做 | 标 `not_achieved` 即可原地重跑 | **必须新建 step id**：系统静默跳过已完成 id，LLM 复用旧 id 不会触发重跑 |

### 0.2 修正后的受信边界

- **调度判定（是否重跑）只由系统可信字段决定**：`exec_state`（系统写入）→ `should_not_rerun`。
- **`task_state` 是 LLM 写给 LLM 看的便签**：系统在 revise 面板（`build_dag_status_text`）把它展示给
  LLM 作为上下文，但**不读取它做任何强制决策**。
- 系统给 LLM 的边界（permission）：SOFT_ERROR/FAILED 步骤可被 revise 保留重跑，COMPLETED 步骤不可原地重跑（须新 id）。
- LLM 在边界内的决策权不变：revised plan 保留/移除哪些步骤。

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

**受信边界**：`ExecState` 写入权归 `DagExecutor`（受信），`TaskState` 写入权归 `Planner`（非受信，LLM 决定）。`should_not_rerun` 是系统强制属性，纯函数 `ExecState → bool`，不依赖 Agent 任何输出（v2.1 起删除 `task_state` 读入，见 §0）。`is_done` 同样为纯 `ExecState` 函数，且**与 `should_not_rerun` 解耦**（含 SOFT_ERROR）。

---

## 3. 文件影响范围

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `harness/core/dag_types.py` | **重写** | 删除 `StepStatus`，新增 `ExecState`/`TaskState`；`StepResult` 改用 `exec_state` (取代 `status`)；新增 `should_not_rerun` 属性；删除 `get()` / `is_completed` / `is_done` / `is_failed` / `needs_confirmation` / `has_soft_error` |
| `harness/core/planner.py` | **修改** | `revise()` 改用 `should_not_rerun`；删除 `isinstance(r, dict)` 守卫和 `"idempotency_hit"` 死代码 |
| `harness/core/dag_executor.py` | **修改** | `_run_step` 返回 `StepResult` 时使用 `exec_state`；`build_dag_status_text` 输出 `exec_state`；全局 `is_done`/`is_completed` 引用替换为 `should_not_rerun`（**v2.1 注**：上游上下文注入与层失败计数改用保留的 `is_done`——含 SOFT_ERROR，见 §4.1） |
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

        v2.1 修正: 纯 ExecState 状态机，不读取 task_state（受信边界约束 4）。
        SOFT_ERROR 不在其中 → 可重跑（自愈依赖此）。COMPLETED 不可原地重跑。
        """
        return self.exec_state in (
            ExecState.COMPLETED,
            ExecState.IDEMPOTENT,
            ExecState.SKIPPED,
            ExecState.CANCELLED,
        )

    @property
    def is_done(self) -> bool:
        """系统判定：该 step 是否"已收尾"（可提供上游上下文 / 不构成层失败）。

        v2.1 修正: 与 should_not_rerun 解耦。SOFT_ERROR 属于"已收尾"
        （工具跑过了，可给下游用 output），但可以重跑。
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
| `r.is_done` | `r.should_not_rerun`（**v2.0 语义**；v2.1 起二者解耦，`is_done` 保留含 SOFT_ERROR 语义，见 §4.1） |
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

**v2.1 追加**：两处 `revised.step_tasks → results[sid].task_state` 合并逻辑（layer_failure 分支与
soft_error 分支）提取为公共辅助函数 `_merge_step_tasks(results, revised)`。该合并**不再影响任何调度判定**
（`should_not_rerun` 不读 task_state），仅保留观测语义。

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

**v2.1 语义变化**：因 `should_not_rerun` 不再含 SOFT_ERROR，`executed_step_ids` **不再包含 SOFT_ERROR 步骤**。
后果：
- SOFT_ERROR 步骤在 parse 时其 `step_tasks` 会被过滤丢弃（`if sid not in exec_ids: continue`）——即 LLM 对
  SOFT_ERROR 步骤的 task_state 便签不被记录（纯注解损失，无调度影响，见 §0.2）。
- SOFT_ERROR 步骤在下轮拓扑排序中**仍参与调度** → 系统保证它可被重跑，**无需 LLM 标记 not_achieved**（约束 4）。
- 旧 Bug 3 的"补丁式 merge（写 task_state 翻转重跑）"整个不再需要。

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

| exec_state    | Meaning                                | can rerun |
|---------------|----------------------------------------|-----------|
| "completed"   | Tool ran successfully                  | No        |
| "soft_error"  | Tool ran but returned a minor issue    | Yes       |
| "idempotent"  | Tool was skipped (duplicate detected)  | No        |
| "skipped"     | Skipped by system                      | No        |
| "cancelled"   | Cancelled externally                   | No        |
| "failed"      | Tool failed (timeout / exception)      | Yes       |
| "pending"     | Not yet executed                       | -         |

## Step Task State (task_state) — ADVISORY, YOU MUST OUTPUT

For each COMPLETED step that already ran, judge its task_state for your own
reference. NOTE: task_state is informational ONLY — it does NOT change whether
a step re-runs. The system decides re-runnability from exec_state alone.

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

## RERUN RULES (system-enforced, not negotiable)

- To RETRY a step that ran with soft_error or failed: keep the step in the
  revised plan (you MAY reuse its id). The system will re-run it.
- To REDO a step that is already "completed"/"idempotent": you MUST give the
  step a NEW id. Reusing a completed step's id is silently SKIPPED — the
  redo will not happen.
```

**v2.1 语义变化**：原表格 `should_not_rerun` 列改为 `can rerun`，`soft_error → Yes`；
`step_tasks` 明确标注为 **advisory**（仅供 LLM 参考，不改变重跑判定）。

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
- [x] `should_not_rerun` 判定为纯函数 `ExecState → bool`，不依赖 LLM 输出（v2.1：删除 `task_state` 读入）
- [x] `is_done` 与 `should_not_rerun` 解耦：`is_done` 含 SOFT_ERROR，`should_not_rerun` 不含（v2.1 新增）
- [x] 不存在 Agent Kernel 直接驱动 DagExecutor 而绕过 `should_not_rerun` 的路径
- [x] SOFT_ERROR 结果**不入幂等缓存**，同输入重跑会真实再次执行工具（v2.1 新增，见 `harness/tools/executor.py`）
- [x] `task_state` 不进入任何受信组件判定路径（v2.1 新增）
- [x] 全局无 `StepStatus` / `.get("status")` / `"idempotency_hit"` 残留

---

## 8. 验收标准

- [x] `ExecState` 与 `TaskState` 完全正交，互不可推导
- [x] `StepResult.should_not_rerun` 对 COMPLETED / IDEMPOTENT / SKIPPED / CANCELLED → True（v2.1：不含 SOFT_ERROR）
- [x] `StepResult.should_not_rerun` 对 SOFT_ERROR / PENDING / RUNNING / FAILED → False
- [x] `StepResult.is_done` 对 COMPLETED / SOFT_ERROR / IDEMPOTENT / SKIPPED / CANCELLED → True（v2.1：含 SOFT_ERROR）
- [x] `should_not_rerun` / `is_done` 结果与 `task_state` 取值完全无关（v2.1 约束 4 回归）
- [x] `planner.revise()` 中 SOFT_ERROR 的 step **不在** `executed_step_ids` 内（v2.1 起）
- [x] e2e：SOFT_ERROR 步骤在 LLM 未标记 not_achieved 时仍被重跑（v2.1 约束 4 回归）
- [x] e2e：COMPLETED 步骤即使被标 not_achieved 也不原地重跑（须新 id，v2.1 新增）
- [x] 幂等缓存：同输入 SOFT_ERROR 重跑命中后不再返回 IDEMPOTENCY_HIT，而是真实再执行（v2.1 新增）
- [x] `planner.py` 中无 `isinstance(r, dict)` / `"idempotency_hit"` 残留
- [x] revise prompt 包含 `ExecState`/`TaskState` 定义表 + RERUN RULES（v2.1）
- [x] `build_dag_status_text()` 输出 `exec_state` 信息，SOFT_ERROR 显示 `replan=MAYBE`
- [x] 全局 `StepStatus` grep 零匹配
- [x] `ruff check harness/` 零警告
- [x] 全量测试通过

---

## 9. 相关文件

- 架构文档: `D:\Project\JAgent\JAgent-docs\Dev\ARCHITECTURE_v2.1.md` §3.7
- ADR: `D:\Project\JAgent\JAgent-docs\Prd\ADR-007_任务完成语义与执行态正交分层设计.md`
- PRD: `D:\Project\JAgent\JAgent-docs\Prd\PRD_S1_任务完成语义分层.md`
- 原始评审: `D:\Project\JAgent\JAgent-docs\Reviews\planner_protocol_gaps_review_20260722.md`
- 测试计划: `D:\Project\JAgent\JAgent-docs\Test_Plan\TestPlan_S1_任务完成语义分层.md`
