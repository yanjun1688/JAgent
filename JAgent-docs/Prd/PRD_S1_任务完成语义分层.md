# PRD-S1: 任务完成语义与执行态正交分层

| 属性 | 值 |
|---|---|
| **文档类型** | 产品需求文档 (PRD) |
| **版本** | 1.0 |
| **日期** | 2026-07-23 |
| **相关 ADR** | ADR-007 |
| **相关缺口** | S1 (任务完成概念歧义) |
| **优先级** | P1 |
| **目标版本** | V0.7.1 |

---

## 1. 问题陈述

### 1.1 用户体验症状

当前系统在以下场景中出现非预期行为：

**场景 A：工具遇到次要告警后，Agent 行为异常**
> 用户在任务中使用 `http_request` 请求外部 API，接口返回 HTTP 200 但 body 中有一个次要字段格式异常。
> 工具将其标记为 `SOFT_ERROR`。此时 Agent 理应**检查输出内容**后决定是否继续，但系统却将该 step 视为"未完成"，
> 在下一次 revise 时强行重新规划——导致已经拿到结果的 step 被重新执行。

**场景 B：幂等命中被系统忽略**
> 用户由于网络波动重新提交了相同的任务。工具层检测到幂等键命中后跳过执行（`ToolCompleted`）。
> 但 Planner 未识别"此 step 已经产生等效结果"——再次向 LLM 请求重新规划，LLM 不知道发生了什么，
> 要么误解状态，要么重新发出相同命令，浪费 token。

**场景 C：多轮 revise 后 LLM 失忆**
> 用户下达一个 8 步 DAG 计划。前 5 步均正常完成（`COMPLETED`），第 6 步因外部限流返回 `SOFT_ERROR`。
> Planner 在 revise 时传给 LLM 的系统状态摘要**把已完成的 5 步与新失败的 1 步混杂展示**，
> LLM 无法确定哪些 step "已经不需要管了" vs "需要重做"——导致 Hallucination，生成了已经执行过的步骤。

### 1.2 根因

三个症状同根：**"工具执行态"与"任务达成态"被混为单个枚举 `StepStatus` 承载**。

```
StepStatus.COMPLETED  → 同时表示"工具跑完了"和"目标达成了"
StepStatus.SOFT_ERROR → 同时表示"工具跑完了"和"不知道目标达没达成"
StepStatus.FAILED     → 同时表示"工具没跑通"和"目标没达成"
```

这个混同导致 Planner 的 `completed_step_ids` 过滤逻辑把 `SOFT_ERROR` 排除在外，
系统无法正确告诉 LLM"哪些 step 已经执行过，不需要重新跑"。

**额外发现**: `planner.py:274` 行中 `isinstance(r, dict)` 检查将 `StepResult` 对象全部排除，
使得 `completed_step_ids` 在运行中恒为空集——这是一个**运行时 bug**，不仅是设计缺陷。

---

## 2. 需求

### 2.1 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| **FR-1** | 系统应通过 `ExecState` 枚举独立表达工具执行态（PENDING / RUNNING / COMPLETED / SOFT_ERROR / FAILED / SKIPPED / IDEMPOTENT / CANCELLED） | P1 |
| **FR-2** | Agent (LLM) 应通过 `TaskState` 枚举独立表达任务达成态（UNKNOWN / ACHIEVED / PARTIAL / NOT_ACHIEVED / WAIVED） | P1 |
| **FR-3** | 系统应通过 `StepResult.should_not_rerun` 属性向 Planner/Scheduler 正确告知"该 step 的工具已执行过，不应再次调度" | P1 |
| **FR-4** | `should_not_rerun` 的判定必须是确定性纯函数，恒真条件为 `ExecState in {COMPLETED, SOFT_ERROR, IDEMPOTENT, SKIPPED, CANCELLED}` | P1 |
| **FR-5** | Planner.revise() 应基于 `should_not_rerun` 而非旧 `StepStatus` 过滤已执行步骤 | P1 |
| **FR-6** | revise 时传给 LLM 的上下文应包含每个 step 的 `exec_state`、`task_state` 和 `output`，系统不替 LLM 推导任务完成 | P1 |
| **FR-7** | LLM 在 revise 输出中应包含 `task_state` 字段，空 step 列表表示整体任务完成 | P1 |
| **FR-8** | 迁移分三步渐进：新增枚举 → 逻辑切换 → 旧代码清理，每步保证零回归 | P1 |
| **FR-9** | 删除 `planner.py:276` 死代码 `"idempotency_hit"` 和 `isinstance(r, dict)` 的误判守卫 | P1 |

### 2.2 非功能需求

| ID | 需求 |
|---|---|
| **NFR-1** | Step 1-2 迁移期间所有现有单元测试必须通过，无行为变更 |
| **NFR-2** | `should_not_rerun` 判定延迟 < 1μs（纯属性查找，无 I/O） |
| **NFR-3** | `ExecState` 和 `TaskState` 枚举值必须对 LLM 可读——使用英文值 (`"completed"`, `"achieved"` 等) |
| **NFR-4** | 下游消费者（分析 API、前端）不受影响——`StepResult.output` 字段语义不变 |

---

## 3. 用户故事

### US-1: Operator 查看 DAG 执行状态时区分工具态与任务态

> 作为运营人员，当我在 Dashboard 查看一个 Run 的 DAG 执行进度时，我希望看到：
> - 哪些 step 的工具已经执行完毕（绿灯）
> - 哪些 step 的工具执行完成但 LLM 判定目标未达成（黄灯）
> - 哪些 step 的工具正在执行中（蓝灯）
> 这样我不需要手动读取每个 step 的原始 output 就能判断整体进度。

**验收标准**: 前端 DAG 可视化中，每个节点同时显示 `exec_state` 颜色环和 `task_state` 角标。

### US-2: Agent 在 revise 时准确获知哪些 step 需要关注

> 作为 Agent (LLM)，当系统调用 revise 并给我当前 DAG 状态时，我希望收到：
> - 一个明确的 `executed_step_ids` 列表，告诉我哪些 step 已经跑过了（不管结果好坏）
> - 每个 step 的 `exec_state` + `output`，让我自己判断目标是否达成
> 而不是一个系统帮我"推导"并可能漏掉的 `completed_step_ids`。

**验收标准**: revise prompt 中的状态原文包含 `should_not_rerun` 标记和 `exec_state` 值，不含系统推导的"任务是否完成"的断言。

### US-3: SOFT_ERROR 的 step 不会被重复执行

> 作为系统用户，当我的一个步骤遇到 `SOFT_ERROR`（如 HTTP 200 + 一个字段类型异常）时，
> 系统应该将这次执行视为"已完成"——不重新调度同一工具调用。
> 但 LLM 应该能拿到 output 数据并决定是否需要补充步骤。

**验收标准**: `SOFT_ERROR` 的 step 在下一轮 DAG 执行中被跳过（`should_not_rerun=True`），但 LLM 在 revise prompt 中看到完整 output。

---

## 4. 验收检查点

| 检查点 | 描述 | 验证方法 | 状态 |
|---|---|---|---|---|
| **AC-1** | `ExecState` 枚举与 `TaskState` 枚举正交且不可互相推导 | 代码审查 + 类型检查 | ✅ |
| **AC-2** | `StepResult.should_not_rerun` 对 COMPLETED / SOFT_ERROR / IDEMPOTENT / SKIPPED / CANCELLED 返回 True | 单元测试 | ✅ |
| **AC-3** | `StepResult.should_not_rerun` 对 PENDING / RUNNING / FAILED 返回 False | 单元测试 | ✅ |
| **AC-4** | `planner.revise()` 中无 `isinstance(r, dict)` 守卫 | 代码审查 | ✅ |
| **AC-5** | `planner.revise()` 中无 `"idempotency_hit"` 字面量 | 代码审查 | ✅ |
| **AC-6** | revise prompt 包含每条 step 的 `exec_state` 和 `output` | 集成测试 + prompt 快照 | ✅ |
| **AC-7** | `TaskState` 枚举值出现在 LLM revise 输出 JSON Schema 中 | 集成测试 | ✅ |
| **AC-8** | 两阶段校验的 `output_schema` 行为不受影响 | 回归测试 | ✅ |
| **AC-9** | `build_dag_status_text()` 输出包含 `should_not_rerun` 标记 | 单元测试 | ✅ |
| **AC-10** | 全量测试套件通过，无退化 | CI 运行 | ✅ |

---

## 5. 非目标 (Out of Scope)

- 不引入新的 `DagStepSkipped` / `DagStepCancelled` 事件类型（`SKIPPED` / `CANCELLED` 枚举值仅作分类，暂不持久化）
- 不修改 `ToolExecutor` 的 `output_schema` 两阶段校验逻辑
- 不修改前端 DAG 可视化（前端已能消费 `RunState.plan_history` 中的 step 状态）
- 不修改 Agent Kernel 的 think/act/observe 循环

---

## 6. 术语表

| 术语 | 定义 |
|---|---|
| **工具执行态 (ExecState)** | step 的工具调用的运行时状态：完成了？失败了？被跳过了？由 DagExecutor 和 Tool Layer 写入，LLM 只读 |
| **任务达成态 (TaskState)** | step 的业务目标是否达成：达成了？部分？没达成？由 LLM 在 revise 中判定并写入 |
| **should_not_rerun** | 布尔属性——该 step 的工具是否已执行过？纯函数 `ExecState → bool`，系统计算 |
| **executed_step_ids** | 所有 `should_not_rerun=True` 的 step ID 集合，用于系统层过滤（取代旧 `completed_step_ids`） |
| **正交分层** | 两个枚举互相独立，一个回答"执行层面发生了什么"，一个回答"目标层面达成了没" |

---

## 7. 相关文档

- ADR-007: `D:\Project\JAgent\JAgent-docs\Prd\ADR-007_任务完成语义与执行态正交分层设计.md`
- Architecture: `D:\Project\JAgent\JAgent-docs\Dev\ARCHITECTURE_v2.1.md` §3.7
- Source review: `D:\Project\JAgent\JAgent-docs\Reviews\planner_protocol_gaps_review_20260722.md` 缺口 B/E
- 技术开发文档: `D:\Project\JAgent\JAgent-docs\Dev\TDD_S1_任务完成语义分层.md`
- 测试计划: `D:\Project\JAgent\JAgent-docs\Test_Plan\TestPlan_S1_任务完成语义分层.md`
