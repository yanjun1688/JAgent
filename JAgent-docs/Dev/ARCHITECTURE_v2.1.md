# Harness v2.1 — 架构文档

> **当前阶段**: V0.7.1 — 任务完成语义与执行态正交分层（S1 完成）
> **未来阶段**: V0.9 — 生命周期恢复（设计文档已就绪，待审查实施）
> **基线**: 700 项测试全通过
> **文档版本**: v2.1.8
> **最后更新**: 2026-07-23

---

## 1. 当前阶段与里程碑

| 里程碑 | 状态 | 核心交付 |
|--------|------|----------|
| MVP | ✅ | Event Store + Tool Layer + Scheduler + Agent Kernel 基础循环 |
| V0.2 | ✅ | ToolRegistry + browser / http_request / file_op / mcp_call / SKILL |
| V0.3 | ✅ | FastAPI REST+WS 后端 + React 前端 |
| V0.4 | ✅ | ScopeGuardrail / RateLimitGuardrail / DestructiveOpGuardrail / DependencyGuardrail |
| V0.4+ | ✅ | Orchestrator 动态编排 + PlanGuardrail + 步骤级安全继承 |
| V0.5 | ✅ | Context Manager（自动压缩/滚动摘要/Checkpoint）+ 断点续传 |
| V0.5+ | ✅ | EpisodeSummary 结构化摘要 + 紧急压缩 |
| V0.6 | ✅ | RunMonitor + FeedbackInjected + Scheduler 反馈注入 |
| V0.6+ | ✅ | 架构加固：Skill 路由 / 输出校验 / 循环检测 / side_effects 消费 / 幂等验证 |
| **V0.6.1** | **✅** | 反馈机制增强：结构化反馈 + per-tool 追踪 + 建议生成 + Planner revise 注入 + Operator API |
| **V0.6.2** | **✅** | **受信控制平面：RUN_COMMAND 事件通道 + 对话模型（conversation_id）** |
| **V0.7** | **✅** | **Planner-Executor + DAG 执行引擎（Phase 1-5）** |
| **V0.9** | **📄 设计完成** | **生命周期恢复：服务器重启孤儿检测 + abandon/retry** |
| V1.0 分析平台 | ✅ | AnalysisService + 6 个 API 端点 |

---

## 2. 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Interface Layer (API)                        │
│  REST: /api/v1/runs CRUD  │  WS: /api/v1/runs/{id}/events          │
│  REST: /api/v1/analysis/* │  POST /confirm /pause /resume /cancel  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                    Scheduler Layer (L3, 受信)                       │
│                                                                     │
│  ┌──────────────────────┐    ┌──────────────────────────────────┐   │
│  │ AgentLoopScheduler   │    │ PlanningExecutorScheduler (V0.7) │   │
│  │ (旧: think→act→obs)  │    │ (新: Plan→Execute→Revise)       │   │
│  │ Fallback 降级目标     │    │                                 │   │
│  └──────────────────────┘    │ ┌──────────┐ ┌──────────────┐   │   │
│                               │ │ Planner  │ │ DagExecutor  │   │   │
│                               │ │ (非受信)  │ │ (受信)        │   │   │
│                               │ └──────────┘ └──────────────┘   │   │
│                               │ ┌──────────┐                    │   │
│                               │ │PlanGuard │ ← dangerous_with   │   │
│                               │ │ rail      │    max_parallel   │   │
│                               │ └──────────┘                    │   │
│                               └──────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────┐        │
│  │ ContextManager (受信) ← 自动压缩 + Checkpoint           │        │
│  └─────────────────────────────────────────────────────────┘        │
│  ┌─────────────────────────────────────────────────────────┐        │
│   │  RunMonitor (受信) ← 异常检测(per-tool+模式识别)           │        │
 │  │         → 结构化 FeedbackInjected (含 tool/error/suggestion) │        │
 │  │         → CONDITION_RESOLVED 分辨率信号                    │        │
│  └─────────────────────────────────────────────────────────┘        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                        Tool Layer (L2, 受信)                        │
│                                                                     │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ ToolExecutor │   │ Guardrail    │   │ IdempotencyKeyGenerator  │  │
│  │              │   │ Runner       │   │                          │  │
│  │ 8-step flow  │   │ Schema/Scope │   │ hash(tool_name           │  │
│  │              │   │ Rate/Dep     │   │   + canonicalize(input)  │  │
│  │ ① Schema    │   │ Destructive  │   │                          │  │
│  │ ② Idem key  │   │ ACL (V1.0)   │   │                          │  │
│  │ ③ Idem check│   └──────────────┘   └──────────────────────────┘  │
│  │ ④ Guardrails│                                                 │
│  │ ⑤ Confirm   │   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ ⑥ Sandbox   │   │ ToolRegistry │   │ Skill Executor           │  │
│  │ ⑦ Validate  │   │ tool_defs +  │   │ (内部走 executor.execute) │  │
│  │   (2-phase) │   │ tool_fns     │   │                          │  │
│  │ ⑧ Write     │   └──────────────┘   └──────────────────────────┘  │
│  └─────────────┘   └──────────────┘   └──────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                  Event Store (L1, 受信, Append-Only)                │
│                                                                     │
│  SQLite (开发) / PostgreSQL + JSONB (生产)                          │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  events (run_id TEXT, seq INT, event_type TEXT,              │   │
│  │          payload JSON, idempotency_key TEXT, created_at REAL)│   │
│  │  PRIMARY KEY (run_id, seq)                                   │   │
│  │  UNIQUE INDEX idx_idem ON (run_id, event_type, idempotency)  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  on_append 回调 → WebSocket 广播 + RunMonitor 事件驱动              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. V0.7 核心架构变更

### 3.1 两种执行模式并存

```
旧循环 (AgentLoopScheduler):    新循环 (PlanningExecutorScheduler):
think → act(串行) → observe     plan → execute(并行) → observe → revise
  ↑ 每轮 1 个 think              ↑ 每轮 N 步 plan，同层并行
  ↑ LLM 既要规划又要执行           ↑ LLM 只负责战略（Plan），系统负责战术（DAG 执行）
```

### 3.2 Planner-Executor 数据流

```
User Intent
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  Planner.plan(intent, state)   [非受信, 调 LLM]          │
│    → LLM 返回 JSON Plan                                   │
│    → PlanGuardrail.validate()                            │
│       ├─ Schema / 工具存在 / 无环                         │
│       ├─ dangerous_with 危险组合检测                      │
│       └─ max_parallel 并行上限检测                        │
│    → 写入 AgentThought + PlanCreated 事件                 │
└───────────────────────┬──────────────────────────────────┘
                        │ DagPlan
                        ▼
┌──────────────────────────────────────────────────────────┐
│  DagExecutor.execute(run_id, plan)   [受信]               │
│    → topological_sort() → [[s1,s2], [s3], [s4]]          │
│    → 逐层 asyncio.gather() 并行执行                       │
│    → 每步: DagStepStarted → Tool Layer → DagStepCompleted │
│    → 上游 output 通过 upstream_selectors 字段路径提取      │
│    → output_summary 截断 200 chars                        │
│    → 失败时写入 PlanFailed, 成功写入 PlanCompleted         │
└───────────────────────┬──────────────────────────────────┘
                        │ {step_id: result}
                        ▼
┌──────────────────────────────────────────────────────────┐
│  Planner.revise(plan, results, system_state)  [非受信]    │
│    → 系统注入 【系统状态 - 不可折叠】 DAG 进度摘要         │
│    → LLM 决定:                                            │
│       ├─ steps=[] → 任务完成, 写 RunCompleted             │
│       ├─ steps=[...] → 继续执行修订计划                   │
│       └─ failed=true → 任务终止, 写 RunFailed             │
└──────────────────────────────────────────────────────────┘
```

### 3.3 受信边界

| 组件 | 受信 | 职责 |
|------|------|------|
| `PlanningExecutorScheduler` | ✅ 受信 | Plan→Execute→Revise 循环驱动、自动事件写入 |
| `Planner` | ❌ 非受信 | 调 LLM 生成/修订 JSON Plan |
| `PlanGuardrail` | ✅ 受信 | Schema 校验、依赖无环、危险组合检测、并行上限 |
| `DagExecutor` | ✅ 受信 | 拓扑排序、并行执行、上游结果摘要化 |

### 3.4 风险管理覆盖

| 风险 | 缓解 | 文件 |
|------|------|------|
| R1 Plan 解析异常 | Planner 重试 2 次 → 降级 `AgentLoopScheduler` | `planner.py` |
| R2 上游 output 膨胀 | `upstream_selectors` 路径提取 + 200 chars 截断 | `dag_executor.py` |
| R3 Revise 时 LLM 失忆 | `【系统状态 - 不可折叠】` 强制注入 | `dag_executor.py:build_dag_status_text()` |
| ~~R4~~ | ~~动态条件分支 (已移除)~~ | `dynamic` 字段已从 `DagPlan` 移除，所有 Plan 统一走 `_execute_plan` | — |
| R5 危险组合漏检 | `_check_dangerous_combinations()` | `planner.py` |
| R6 fold 规则 undefined | 事件分级 fold（不可 fold / 摘要化 / 可跳过） | `fold.py` |

### 3.5 V0.6.2 受信控制平面: RUN_COMMAND

#### 动机

上一轮 review 发现 **C 缺口**: `RunMonitor` 只能写 `FeedbackInjected`（建议性反馈），无法对 Scheduler 发出强制命令（如 `hard_abort`）。Monitor 因此只是"建议者"而非"执法者"——违反 §2.2「系统强制不依赖 Agent 配合」。

V0.6.2 引入 `RUN_COMMAND` 事件类型作为**受信控制平面**：受信组件（RunMonitor、Operator API）通过写入 Event Store 的 `RUN_COMMAND` 事件向 Scheduler 发出强制命令，Scheduler 在每次循环迭代中自动检查并强制执行。

#### 数据流

```
RunMonitor / Operator API                        Scheduler Loop
      │                                              │
      ├─ write RUN_COMMAND {"hard_abort"}            │
      │                                              ├─ _handle_pending_commands()
      │                                              │   └─ _check_pending_commands()
      │                                              │       └─ scan Event Store for unprocessed
      │                                              │          RUN_COMMAND events (by ordinal)
      │                                              │   └─ _process_command()
      │                                              │       ├─ hard_abort/soft_abort → _fail()
      │                                              │       ├─ pause → pause()
      │                                              │       ├─ resume → resume()
      │                                              │       └─ skip_tool → ack only
      │                                              ├─ re-evaluate terminal/paused status
      │                                              └─ return refreshed RunState
```

#### 设计关键

- **顺序保证**: RUN_COMMAND 在同一 run_id 内按 1-based ordinal 排序（独立于其他事件 seq），Scheduler 仅处理上次已处理序号之后的新命令
- **永久存储**: RUN_COMMAND 事件持久化在 Event Store，不会被上下文压缩清除
- **幂等**: 已处理的命令 ordinals 由 `_last_processed_command_seq` 跟踪，重复写入同一条命令不会导致重复执行
- **故障隔离**: `_check_pending_commands` 中 Store 读取异常被捕获，返回 "无命令" 继续正常循环——控制平面故障不能终止 Agent 循环

#### 命令清单

| 命令 | 来源 | 行为 |
|------|------|------|
| `hard_abort` | RunMonitor | 立即写入 `RunFailed`，终止循环 |
| `soft_abort` | RunMonitor | 同 `hard_abort`，语义区分预留 |
| `pause` | Operator API | 等效 `POST /pause` |
| `resume` | Operator API | 等效 `POST /resume` |
| `skip_tool` | （预留） | 工具级跳过，当前仅 ack |

#### 受信边界

| 组件 | 受信 | 职责 |
|------|------|------|
| `RUN_COMMAND` 写入者 | ✅ 受信 | RunMonitor / Operator API / 受信组件 |
| `RUN_COMMAND` 消费者 | ✅ 受信 | `BaseScheduler._handle_pending_commands()` |
| Agent Kernel | ❌ 无感知 | 命令通道对 LLM 完全透明 |

### 3.6 对话模型（Conversation）

V0.6.2 引入对话级概念：多个 Run 可归属于同一 `conversation_id`。

| 事件类型 | 写入方 | 关键 payload |
|----------|--------|-------------|
| `ConversationStarted` | API | `conversation_id, title, user_id` |
| `ConversationMessage` | API | `conversation_id, run_id, role, content` |
| `ConversationEnded` | API | `conversation_id, summary` |

- `RunStartedPayload.conversation_id` — 创建 Run 时关联到对话
- `RunState.conversation_id` — fold_events 中恢复
- Store 级 `evict_run_to_conv(run_id)` — Scheduler cleanup 时清理内存映射

### 3.7 架构缺口已修复 — 任务完成概念歧义（S1，已完成)

> **缺口 ID**: `S1` · **优先级**: **P1** · **状态**: ✅ **已完成** (V0.7.1) · **来源**: `Reviews/planner_protocol_gaps_review_20260722.md` 缺口 B/E
> **设计文档**: ADR-007 / PRD_S1 · **测试**: TestPlan_S1
> **最后更新**: 2026-08-07（v2.2 完成门 + step_normal + probe + 可溯源，见 7.7）

#### 7.1 问题背景（已解决）

V0.7 将 **"工具完成状态"与"任务完成语义"混为一谈**：
- `COMPLETED`（step 完成）≠ 任务完成
- `SOFT_ERROR`（工具成功但有告警）≠ 任务完成
- `IDEMPOTENCY_HIT`（幂等命中）≠ 任务完成

核心歧义：`planner.revise()` 判定"step 不应重排"的判据使用 `StepStatus`（执行态），但真正需要的是与任务达成度无关的"工具是否已跑过"判定。详见 ADR-007 和 PRD_S1。

#### 7.2 设计方案（ADR-007）

三分层正交架构：

| 概念 | 枚举 | 职责 | 值 |
|------|------|------|----|
| **执行态** | `ExecState` (8值) | 工具的纯执行结果，系统强制 | `pending,running,completed,unsuccessful,failed,skipped,idempotent,cancelled`（v2.2 起 `soft_error`→`unsuccessful`） |
| **任务态** | `TaskState` (5值) | LLM 判定的任务达成度，非受信，**纯审计便签（v2.2 D11）** | `unknown,achieved,partial,not_achieved,waived` |
| **决策属性** | `should_not_rerun` | 由 `exec_state` 推导的纯函数，决定是否跳过重排（v2.1 起**不含 UNSUCCESSFUL**；v2.2 D9 移除 SKIPPED） | `COMPLETED/IDEMPOTENT/CANCELLED` → True；`UNSUCCESSFUL/SKIPPED/PENDING/RUNNING/FAILED` → False |
| **正常判定** | `step_normal`（v2.2 D3） | 完成门的原子判据，纯函数 `(exec_state, probe) → bool`，不读 `task_state`（约束 4） | `COMPLETED/IDEMPOTENT` → True；`UNSUCCESSFUL and probe` → True；其余 → False |
| **输出可用** | `output_available`（v2.2 由 `is_done` 改名收窄） | 仅用于 planner `available_step_ids`（`$var.field` 可用集），不用于完成计数/门控 | `COMPLETED/UNSUCCESSFUL/IDEMPOTENT` → True；`SKIPPED/CANCELLED` → False |
| **完成门** | 系统聚合（v2.2 D5） | 最终计划所有步骤 `step_normal` 的聚合 = 任务完成 | 全 normal → 完成；否则列出未达成 |

#### 7.3 核心改动（全部完成）

| 文件 | 改动 |
|------|------|
| `harness/core/dag_types.py` | 新增 `ExecState`/`TaskState` 枚举；`StepResult` 新增字段 + 便捷属性保留；`should_not_rerun` 纯函数；Step 3：删除 `StepStatus`、`status` 字段、`get()` shim |
| `harness/core/planner.py` | `completed_step_ids` → `executed_step_ids`；删除死代码 `"idempotency_hit"` |
| `harness/core/dag_executor.py` | `build_dag_status_text()` 输出 `exec_state` + `should_not_rerun` 标记 |
| `harness/core/scheduler/plan.py` | 拓扑排序改用 `should_not_rerun` |

#### 7.4 设计原则（归档）

1. **工具成功 ≠ 任务完成**：系统只能告知"工具返回了什么"，不推导任务达成度
2. **工具失败 ≠ 任务失败**：FAILED step 仍可由 LLM 决策重试
3. ~~**完成的判定权归 Agent，执行态归系统**~~ → **v2.2 失效（见 7.7）**：完成判定为系统机械聚合 `step_normal`
4. ~~**不**在受信组件层用启发式规则推导任务完成~~ → **v2.2 起**：完成判定为机械聚合（非启发式），由受信组件强制

#### 7.5 验收状态

**700 项测试全通过**（含 43 项新增 S1 测试），零退化。Step 3 旧代码清理（删除 `StepStatus`、`status` 字段、`get()` backward-compat shim）已一并完成。

#### 7.6 v2.1 受信边界修正（Bug S1.1，2026-08-04）

> **问题**：v2.0 实现中 `should_not_rerun` 在纯 `ExecState` 判定之外**额外读取了 `task_state`**：
> `... and self.task_state != NOT_ACHIEVED`。这使系统强制（约束 4）依赖 LLM 配合——LLM 忘记标
> `not_achieved` 时 SOFT_ERROR 步骤永不重跑（自愈静默失效）；LLM 对 COMPLETED 标 `not_achieved` 时
> 会原地重跑（副作用重复执行）。**调度决策权落入非受信组件。**

**修正内容**：

| 改动 | 位置 | 说明 |
|------|------|------|
| `should_not_rerun` 删除 `task_state` 读入，且**不含 SOFT_ERROR** | `harness/core/dag_types.py` | 纯 `ExecState` 状态机；SOFT_ERROR 可重跑 |
| `is_done` 与 `should_not_rerun` 解耦，**含 SOFT_ERROR** | `harness/core/dag_types.py` | 管"层失败判定 / 上游上下文注入 / 完成计数" |
| `task_state` 降级为纯注解 | 全局 | 只在 `build_dag_status_text` 展示给 LLM，不进入任何受信判定 |
| 两处 step_tasks 合并逻辑提取公共函数 | `harness/core/scheduler/plan.py` | 纯观测，不再影响调度 |
| SOFT_ERROR 结果**不入幂等缓存** | `harness/tools/executor.py` | 同输入重跑必须真实再执行工具 |
| revise prompt 增加 RERUN RULES | `harness/core/system_prompt.py` | COMPLETED 重做须新 id；task_state 标注为 advisory |

**边界语义（v2.1）**：
- 系统定边界（permission）：SOFT_ERROR/FAILED 可被 revise 保留重跑；v2.2 D9 起 SKIPPED 也可重跑（它表示工具从未执行）；COMPLETED/IDEMPOTENT/CANCELLED 不可原地重跑。
- Agent 在边界内决策：revised plan 保留/移除哪些步骤、用什么输入。
- **强制权归系统，决策权归 Agent**：系统不信任 LLM 的重跑意愿，只提供"允许重跑"的边界；LLM 在边界内自由选择。

#### 7.7 v2.2 完成门 + step_normal + probe + 可溯源（2026-08-07）

> **来源**: `Handover/completion_semantics_chain_redesign_handover_20260807.md` D1–D11
> **目标**: U1（自愈不收敛）/ U2（完成脱钩）根治，修复审计 P0-03（SOFT_ERROR 被当 done 传递）。

**背景**：溯源调研发现 5 个链路缺口——
1. `step ↔ tool_call` 在事件流无法 JOIN（工具事件无 `step_id`，DAG 事件无 `tool_call_id`）
2. 计划结构不落事件（只有 `steps_summary: "N steps in M layers"`），事后无法重建 DAG 蓝图
3. 完成口径用 `is_done`（含 SOFT_ERROR），SOFT_ERROR 被算进 "Completed 3/3"
4. `task_state` 不落事件（LLM 便签双重无用：不可审计）
5. run 终态无证据（`RUN_COMPLETED` 只有 LLM 自由文本 `result_summary`）

**核心设计**：

```
任务完成（完成门）= 最终计划所有步骤 step_normal 的聚合        ← D5（替代 LLM"空 steps"判定）
  step_normal = (exec_state ∈ {COMPLETED, IDEMPOTENT})
                or (exec_state == UNSUCCESSFUL and step.probe)  ← D3（纯系统计算）
  probe        = 探测型步骤声明，仅无副作用工具可标             ← D4/D10（PlanGuardrail 强制）
  下游门控      = 依赖非 normal → 下游 SKIP 并落记录            ← D7/D9（P0-03 修复）
```

**改动一览**：

| 阶段 | 内容 | 关键文件 |
|------|------|----------|
| A | 改名 `SOFT_ERROR`→`UNSUCCESSFUL`（全库零残留） | 14 源码文件、107 测试、前端/分析 API |
| B | `step_normal` 口径：完成计数/门控改用 normal；`is_done`→`output_available`（仅 `available_step_ids` 用）；门控产生 SKIPPED 落记录 | `scheduler/plan.py`、`dag_executor.py`、`dag_types.py`、`planner.py` |
| C | 可溯源：工具事件带 `step_id`、计划结构落事件 | `events.py`、`executor.py`、`dag_executor.py`、`fold.py` |
| D | 完成判定机械化：全 normal → 完成，否则列未达成；`task_state` 落事件（`PlanRevisedPayload` 补 `step_tasks`） | `scheduler/plan.py`、`events.py`、`fold.py`、`routes.py` |
| E | `probe` 声明 + 退化修订守卫（U1 收敛） | `models/plan.py`、`planner.py`、`guardrails.py` |

**受信边界（v2.2）**：
- `step_normal` / 完成门 / 下游门控 **不读 `task_state`**、不依赖 LLM 配合（约束 4）
- `probe` 是 LLM 的 step 级声明，但**必须**由 PlanGuardrail（受信）校验仅无副作用工具可标（D10）
- 门控条件**唯一** = `step_normal`，系统不猜下游能否消费 probe 否定答案（D7，fail-safe 自动成立）
- `task_state` 落事件供审计；未来计划展示 **"LLM 自评 vs 系统机械判定"差异对比**（D11）——
  该功能**暂不实现**，仅在代码注释中标注定位。**永不参与受信判定。**
- 完成判定为机械聚合（非启发式），属系统强制，不违反"受信组件不含 LLM 推理"。
- 修订计划不得缩小 Run 的目标集合：Scheduler 保留原始步骤全集；替代步骤只能通过受信别名替代失败步骤，已被依赖门控为 `SKIPPED` 的下游步骤在前驱恢复后必须重新激活。最终完成门必须针对原始步骤全集聚合，不能只检查当前 LLM 修订计划（D12）。

### 3.8 用户输出与会话上下文边界（P0-06，已完成）

- `RunStartedPayload.intent/current_request` 只保存本次用户请求；会话历史不得拼接进事件 intent。
- 会话历史作为独立的 `conversation_context` 参数按需注入 Planner/Kernels，classify 和 revise 不重复携带完整历史。
- `RunFailedPayload.result_summary` 是内部遥测；`user_facing_message` 是唯一允许写入对话 assistant 消息的失败文本。
- LLM 请求必须支持 `run_id` 追踪；共享客户端复用连接池并通过并发信号量隔离请求。
- `step_id` 是工具调用、超时、Guardrail、确认和 DAG 事件之间的受信 JOIN 键。

---

## 4. 组件接口设计

### 4.1 Event Store 接口

```python
# harness/storage/event_store.py
class EventStore:
    async def initialize() -> None
    async def close() -> None
    async def append_event(run_id, event_type, payload, *, idempotency_key=None, max_retries=3) -> Event
    async def get_events(run_id) -> list[Event]
    async def get_event_range(run_id, from_seq, to_seq) -> list[Event]
    async def get_latest_seq(run_id) -> int
    async def event_count(run_id) -> int
    async def find_by_idempotency_key(run_id, event_type, key) -> Event | None
    async def list_runs(limit=50, offset=0) -> list[dict]
    async def list_all_run_ids() -> list[str]    # V0.8: 启动孤儿检测
    async def total_run_count() -> int
    async def get_events_for_runs(run_ids) -> dict[str, list[Event]]
    async def find_confirmation_by_id(run_id, confirmation_id) -> Event | None
    async def execute_query(sql, params) -> list[dict]
    async def execute_query_one(sql, params) -> dict | None
    def on_append(callback) -> None  # 注册写入后回调
```

### 4.2 事件类型清单（共 43 种）

| 事件类型 | 写入方 | 关键 payload 字段 |
|----------|--------|-------------------|
| `RunStarted` | Scheduler | `intent, current_request, context_snapshot, conversation_id` |
| `AgentThought` | Scheduler | `thought, tool_choice, token_count, tool_calls` |
| `ToolCalled` | Tool Layer | `tool_call_id, tool_name, input, idempotency_key` |
| `ToolCompleted` | Tool Layer | `tool_call_id, tool_name, output, duration_ms` |
| `ToolFailed` | Tool Layer | `tool_call_id, tool_name, error, retryable` |
| `ToolTimeout` | Tool Layer | `tool_call_id, tool_name, timeout_ms, step_id` |
| `GuardrailTriggered` | Tool Layer | `tool_call_id, tool_name, guardrail_id, reason, step_id` |
| `ConfirmationRequested` | Tool Layer | `confirmation_id, tool_call_id, tool_name, input, risk_level, step_id` |
| `ConfirmationReceived` | 外部接口 | `confirmation_id, confirmed, operator_id, step_id` |
| `ContextCompressed` | Context Mgr | `original_tokens, compressed_tokens, summary_ref` |
| `ContextCheckpointed` | Context Mgr | `checkpoint_seq, snapshot_ref, token_count` |
| `RunPaused` | Scheduler | `reason` |
| `RunResumed` | Scheduler | `resume_from_seq` |
| `RunCompleted` | Scheduler | `result_summary`（v2.2 D 阶段补机械达成证据：`all_normal, unmet_step_ids`） |
| `RunFailed` | Scheduler/Tool | `final_error, event_count, result_summary, user_facing_message` |
| **`RunCommand`** | **V0.6.2** | **`command(hard_abort\|soft_abort\|pause\|resume\|skip_tool), reason, issued_by`** |
| **`RunOrphaned`** | **V0.9 Lifecycle** | `reason, last_seq, timestamp` |
| `FeedbackInjected` | RunMonitor / Operator API | `feedback_text, priority, source(monitor\|operator), category, affected_tool, error_type, error_detail, suggestion, expires_at_seq, resolves_feedback_id` |
| **`ConversationStarted`** | **V0.6.2 API** | `conversation_id, title, user_id` |
| **`ConversationMessage`** | **V0.6.2 API** | `conversation_id, run_id, role, content` |
| **`ConversationEnded`** | **V0.6.2 API** | `conversation_id, summary` |
| **`PlanCreated`** | **V0.7** | `plan_id, intent, steps_summary, layer_count`（v2.2 C 阶段补 `steps: [{step_id, tool_name, input, depends_on, description, probe}]`） |
| **`DagStepStarted`** | **V0.7** | `plan_id, step_id, tool_name, depends_on` |
| **`DagStepCompleted`** | **V0.7** | `plan_id, step_id, output_summary`（v2.2 C 阶段补 `tool_call_id`） |
| **`DagStepFailed`** | **V0.7** | `plan_id, step_id, error, retryable`（v2.2 C 阶段补 `tool_call_id`） |
| **`DagStepSkipped`** | **V0.7 v2.2** | `plan_id, step_id, reason(dep_not_normal)` — 门控产生 SKIPPED 落记录（D9） |
| **`PlanRevised`** | **V0.7** | `plan_id, revision_reason, remaining_steps_summary`（v2.2 C 阶段补 `steps` 结构；D 阶段补 `step_tasks` 审计便签） |
| **`PlanCompleted`** | **V0.7** | `plan_id, completed_steps, total_layers, summary` |
| **`PlanFailed`** | **V0.7** | `plan_id, completed_steps, total_layers, final_error` |
| `OrchestrationStarted/Completed/Failed` | Orchestrator | （旧 V0.4+，与 V0.7 共存） |
| `StepCompleted/StepFailed` | Orchestrator | |

### 4.3 ToolDefinition 契约

```python
# harness/models/tools.py
class ToolDefinition(BaseModel):
    name: str                              # 全局唯一
    description: str                       # Agent 可读
    input_schema: JSONSchema               # 输入 JSON Schema
    output_schema: JSONSchema              # 输出 JSON Schema
    idempotency_key_fields: list[str]      # 幂等键字段集合
    side_effects: list[SideEffect]         # write / delete / external
    timeout_ms: int                        # 超时上限
    retry_policy: RetryPolicy              # 重试策略
    guardrails: list[Guardrail] | None     # 前置检查列表
    requires_confirmation: bool            # 是否需要人工确认
    depends_on: list[DependencyConstraint]  # 声明式事件前置条件
    dangerous_with: list[str]              # V0.7: 危险组合工具名列表
    max_parallel: int = 10                 # V0.7: 同层并行实例上限
```

**`output_schema` 两阶段校验**（V0.7, 2026-06-11）：

工具执行后，`output_schema` 经两阶段校验：
- **Phase 1**：严格 `jsonschema.validate()` — 类型、必填字段、格式全部检查
- **Phase 2**（Phase 1 失败时）：`_structurally_usable()` 结构性兜底 — 只检查输出是否为 `dict` 或 `list`（导航可用），不是则拒绝（`None`/`bool`/`str`/`int`/`float`）

Phase 2 通过 → `ToolCompleted`（输出被接受），Phase 2 失败 → `ToolFailed`（error 脱敏，仅含类型名）。适用于 `http_request` 等变量内容字段无法预知远端格式的场景。详见 §7.3。

### 4.4 DAG 数据模型

```python
# harness/models/plan.py
@dataclass
class DagStep:
    id: str                                # 唯一标识，如 "s1"
    tool: str                              # 工具名
    input: dict[str, Any]                  # 输入参数
    depends_on: list[str]                  # 依赖的上游 step id
    description: str                       # 人类可读描述
    upstream_selectors: dict[str, str] | None  # 上游字段路径，如 {"s1": "weather.summary"}
    max_parallel: int = 10                 # 该工具在单层中的并行上限
    branches: dict | None                  # 预留条件分支

@dataclass
class DagPlan:
    intent: str                            # 用户意图
    steps: list[DagStep]                   # 步骤列表
    # V0.8: `dynamic` 字段已移除 — 所有 Plan 统一走 _execute_plan 路径

    def topological_sort(self) -> list[list[str]]  # Kahn 算法，返回分层 step id
        # 返回如 [["s1","s2"], ["s3"], ["s4"]]

    def upstream_outputs(self, step_id, results) -> dict[str, Any]
        # 按 upstream_selectors 路径提取上游结果
```

### 4.5 Planner + PlanGuardrail 接口

```python
# harness/core/planner.py
class Planner:
    def __init__(self, llm_client, registry, store=None, max_plan_retries=2)
    async def plan(intent, state=None) -> DagPlan | None
        # 调 LLM → 解析 → Guardrail 校验 → 重试最多 3 次
    async def revise(plan, results, system_state) -> DagPlan | None
        # 注入 DAG 状态摘要 → LLM 决定继续/完成/终止
    @property
    last_raw_response: str                 # LLM 原始输出（用于 AgentThought 事件）

class PlanGuardrail:
    def validate(plan) -> list[str]        # 返回错误列表，空 = 通过
        # 检查：工具存在 / input 类型 / 重复 ID / depends_on 有效性
        # 拓扑排序有环检测
        # dangerous_with 组合检测
        # max_parallel 按工具计数检测
```

### 4.6 DagExecutor 接口

```python
# harness/core/dag_executor.py
class DagExecutor:
    def __init__(self, executor, store, registry)
    async def execute(run_id, plan) -> dict[str, Any]
        # 完整 execute: PlanCreated → 逐层执行 → PlanCompleted/PlanFailed
        # 用于 dynamic 退化路径
    async def execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, results) -> bool
        # 单层执行，用于 scheduler 外层循环，返回 True=成功, False=失败
    @staticmethod
    def build_dag_status_text(plan, results, current_layer) -> str
        # 构建含 【系统状态 - 不可折叠】 的状态摘要
```

### 4.7 Scheduler 接口

```python
# harness/core/scheduler.py
class SchedulerConfig:
    max_iterations: int = 50
    max_consecutive_failures: int = 5
    pause_timeout_ms: int = 300_000

class BaseScheduler(ABC):
    def __init__(self, ..., run_end_cb=None)   # Cleanup: run() finally 调用 run_end_cb(run_id)
    async def run(run_id, intent) -> RunState
    async def pause(run_id) -> bool           # 写 RunPaused 事件，返回 True=成功/False=状态不符
    async def resume(run_id) -> bool          # 写 RunResumed 事件，返回 True=成功/False=状态不符 (V0.8: 严格仅 PAUSED)
    async def cancel(run_id) -> None         # 设置 cancel flag
    def is_active(run_id) -> bool
    def is_paused(run_id) -> bool

    # V0.8: Checkpoint 恢复
    async def _try_checkpoint_recovery(events) -> bool  # 从 ContextManager 查找 checkpoint

    # V0.6.2: 受信控制平面 — RUN_COMMAND
    async def _check_pending_commands(run_id) -> str | None  # 返回最新未处理命令（按 1-based ordinal 排序）
    async def _process_command(run_id, command) -> bool       # 执行命令（hard_abort→_fail, pause→pause, resume→resume）
    async def _handle_pending_commands(run_id) -> RunState | None  # 检查+执行一条命令，返回刷新后的状态
    # RUN_COMMAND 在 fold_events 中为 pass-through（不改变 RunState 字段）
    # 已处理命令的 ordinal 由 _last_processed_command_seq: dict[str, int] 跟踪

**Cleanup 契约**: `run()` 的 `finally` 块保证清理：
  1. `_running_tasks` / `_cancel_flags` / `_pause_events` / `_last_processed_command_seq`（Scheduler 内部 run-scoped dict）
  2. `monitor.cleanup(run_id)`（RunMonitor 的 15 个 per-run 字典）
  3. `store.evict_run_to_conv(run_id)`（V0.6.2: 清理 run→conversation 内存映射）
  4. `run_end_cb(run_id)`（API 层注册的回调，清理 `_schedulers` / `_ws_clients`）
  5. 异常/取消路径均走同一 `finally`，无遗漏。

class AgentLoopScheduler(BaseScheduler):
    # 旧串行调度器：think → act → observe
    # 当前 serve.py 默认使用此调度器
    # 在 Planner 全失败时用作降级目标

class PlanningExecutorScheduler(BaseScheduler):
    # V0.7 新调度器：Plan → Execute(并行) → Revise
    def __init__(self, store, executor, planner, dag_executor, ...)
    async def _plan_execute_revise_loop(run_id, intent) -> RunState
        # 主循环：plan() → DAG 执行 → revise() → 迭代
        # DAG 执行: 同层并行 asyncio.gather, 逐层执行（V0.8: `dynamic` 分支已移除，统一路径）
        # 降级: plan() 全重试失败 → _get_or_fallback() → AgentLoopScheduler
```

### 4.8 REST API 端点

| 方法 | 路径 | 请求 | 响应 | 说明 |
|------|------|------|------|------|
| GET | `/api/v1/runs` | — | `RunListResponse` | 所有 Run 列表 |
| POST | `/api/v1/runs` | `{"intent": str}` | `{"run_id": str}` | 创建新 Run |
| GET | `/api/v1/runs/{run_id}` | — | `RunDetailResponse` | 折叠状态快照 |
| GET | `/api/v1/runs/{run_id}/events` | `?from_seq=` | `EventListResponse` | 事件流 |
| POST | `/api/v1/runs/{run_id}/pause` | — | `{"success": bool}` | 暂停 |
| POST | `/api/v1/runs/{run_id}/resume` | — | `{"success": bool}` | 恢复 |
| POST | `/api/v1/runs/{run_id}/confirm` | `{"confirmation_id", "confirmed", "operator_id"}` | `{"success": bool}` | 确认决策 |
| POST | `/api/v1/runs/{run_id}/feedback` | `{"text","priority","suggestion"}` | `{"status","feedback_id"}` | V0.6.1: Operator 手动反馈注入 |
| DELETE | `/api/v1/runs/{run_id}` | — | `{"success": bool}` | 取消/终止 |
| POST | `/api/v1/runs/{run_id}/abandon` | — | `{"success": bool}` | V0.8: 放弃孤儿 Run（仅 `orphaned==True`） |
| POST | `/api/v1/runs/{run_id}/retry` | — | `{"run_id", "retry_of"}` | V0.8: 重试孤儿 Run（创建新 Run） |
| WS | `/api/v1/runs/{run_id}/events` | — | 实时 Event JSON | WebSocket 事件流 |

**分析 API：**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/analysis/dashboard` | 全局概况卡片数据 |
| GET | `/api/v1/analysis/tools` | 工具使用统计 |
| GET | `/api/v1/analysis/guardrails` | Guardrail 拦截统计 |
| GET | `/api/v1/analysis/runs/{run_id}` | 单 Run 分析摘要 |
| GET | `/api/v1/analysis/runs/{run_id}/timeline` | 分页事件时间线 |
| GET | `/api/v1/analysis/runs/{run_id}/tool-traces` | 工具 Trace 列表 |
| POST | `/api/v1/operations/retry` | 501 占位（预留） |

### 4.9 fold_events 输出结构

```python
# harness/core/fold.py
@dataclass
class RunState:
    run_id: str
    status: RunStatus              # running / paused / completed / failed
    seq: int                       # 当前最大事件序号
    intent: str                    # 用户意图
    context_snapshot: dict
    thought_history: list[ThoughtEntry]      # agent 思考历史
    latest_thought: ThoughtEntry | None
    tool_calls: list[ToolCalledPayload]      # 工具调用记录
    tool_results: list[ToolResult]           # 工具结果
    last_error: str | None
    summary: EpisodeSummary | str | None     # 压缩摘要
    pending_confirmations: list[...]         # 待确认列表
    last_checkpoint_seq: int | None
    orchestration_history: list[dict]        # 旧编排历史
    latest_orchestration: dict | None
    feedbacks: list[FeedbackInjectedPayload] # 监视器反馈 (结构化：含 category/tool/error/suggestion/expires/resolves)
    orphaned: bool                           # V0.9: 孤儿标记（服务器重启后与 Scheduler 失联）
    plan_history: list[dict]                 # V0.7: DAG 规划历史
    latest_plan: dict | None                 # V0.7: 当前最新计划
    conversation_id: str | None              # V0.6.2: 对话 ID（多 Run 归属同一对话）
```

---

## 5. 关键数据流

### 5.1 正常 DAG 执行流

```
POST /api/v1/runs {"intent": "Search weather and news"}
  → create_run: RunStarted
  → PlanningExecutorScheduler.run()
    → _plan_execute_revise_loop()
      → 1. Planner.plan()
        → LLM → JSON Plan: [search(weather), search(news)]
        → PlanGuardrail ✅
        → AgentThought + PlanCreated
      → 2. DagExecutor.execute_layer (layer 0)
        → [search weather, search news] 并行
        → DagStepStarted × 2
        → Tool Layer 执行 × 2
        → DagStepCompleted × 2
      → 3. PlanCompleted
      → 4. Planner.revise()
        → 注入系统状态摘要
        → LLM → {"steps": []}  (任务完成)
        → PlanRevised
      → 5. RunCompleted
```

### 5.2 降级回退流

```
Planner.plan() → 重试 2 次 → 全部失败
  → _get_or_fallback()  [scheduler.py:856]
    → 创建 _FallbackKernel(Planner.llm)
    → 创建 AgentLoopScheduler(...)
    → 运行串行调度器（旧 think→act→observe）
    → 返回 RunState
```

### 5.3 上下文压缩流

```
EventStore.append_event(RUN_COMPLETED)
  → on_append 回调
    ├→ WebSocket 广播（前端收到 Run 完成通知）
    └→ （无额外异步任务）
```

### 5.4 错误处理流

```
DagExecutor._run_step()
  → ToolExecutor.execute()
    → ToolFailed (exception / timeout / guardrail)
  → _run_step 写 DagStepFailed → 返回 {"status": "error"}
  → _execute_layer 写 PlanFailed → return False
  → Scheduler 写 RunFailed
```

---

## 6. 文件组织结构

```
harness/
├── api/
│   ├── app.py               # FastAPI 应用组装
│   ├── routes.py             # Run CRUD 端点
│   ├── ws.py                 # WebSocket 事件流
│   ├── analysis_routes.py    # 分析 API 端点
│   ├── schemas.py            # 请求/响应 Pydantic 模型
│   ├── deps.py               # HarnessAPI 依赖容器
│   └── serve.py              # 生产入口装配
├── core/
│   ├── scheduler.py          # [V0.7] AgentLoopScheduler + BaseScheduler + PlanningExecutorScheduler ; [V0.6.2] RUN_COMMAND 控制平面
│   ├── lifecycle.py          # [V0.8] 孤儿 Run 检测 + mark/abandon/retry（与 Scheduler 解耦）
│   ├── planner.py            # [V0.7] Planner + PlanGuardrail
│   ├── dag_executor.py       # [V0.7] DagExecutor + build_dag_status_text
│   ├── fold.py               # [V0.7] plan_history + 新事件 fold; [V0.9] orphaned 标记; [V0.6.2] conversation_id + RUN_COMMAND pass-through
│   ├── context_manager.py    # 自动压缩 + Checkpoint [V0.6.2: emergency/threshold overflow boundary fix]
│   ├── agent_kernel.py       # LLMAgentKernel + MockAgentKernel
│   ├── llm_client.py         # OpenAILLMClient + MockLLMClient
│   ├── system_prompt.py      # System Prompt 构建
│   ├── logger.py             # 日志工具
│   └── __init__.py           # 导出全部新类
├── models/
│   ├── plan.py               # [V0.7] DagStep + DagPlan
│   ├── events.py             # [V0.7] 7 个新事件 + [V0.6.2] RUN_COMMAND + ConversationStarted/Message/Ended + RunCommandPayload
│   ├── conversation.py       # [V0.6.2] 对话模型
│   └── tools.py              # [V0.7] dangerous_with + max_parallel
├── storage/
│   └── event_store.py        # Append-Only Event Store
├── tools/
│   ├── executor.py           # ToolExecutor 8 步执行
│   ├── registry.py           # ToolRegistry
│   ├── browser_tool.py       # Playwright 封装
│   ├── http_request.py       # HTTP 请求
│   ├── file_op.py            # 沙箱文件操作
│   └── mcp_call.py           # MCP 工具调用
├── monitoring/
│   └── run_monitor.py        # RunMonitor 异常检测 (per-tool 追踪 + 模式识别 + 结构化反馈注入)
└── analysis/
    ├── schemas.py             # 分析响应模型
    └── service.py             # AnalysisService 聚合查询
```

---

## 7. 下一步

### 7.0 V0.6.2 已完成变更（2026-07-23）

| 变更 | 文件 | 说明 |
|------|------|------|
| RUN_COMMAND 控制平面 | `events.py`, `base.py`, `loop.py`, `plan.py`, `fold.py` | 新事件类型 `RunCommand` + `RunCommandPayload`；`BaseScheduler._check_pending_commands/_process_command/_handle_pending_commands`；`AgentLoopScheduler` 和 `PlanningExecutorScheduler` 每轮循环自动检查；fold_events 中 pass-through |
| 对话模型 | `events.py`, `fold.py`, `base.py`, `__init__.py`, `conversation.py`(new) | `ConversationStarted/Message/Ended` 事件 + `RunStartedPayload.conversation_id` + `RunState.conversation_id`；Scheduler cleanup 时 evict_run_to_conv |
| Context Manager 边界修正 | `context_manager.py` | emergency threshold 改为 `max(emergency_threshold, token_limit)` 防止估算超限但未触达硬上限时误用紧急压缩 |

### 7.1 已完成架构修复（2026-06-07）

| 修复 | 文件 | 说明 |
|------|------|------|
| DAG_STEP fold 去重 | `fold.py` | 按 step_id upsert，不再 append 重复 |
| seq 分配原子性 | `event_store.py` | 追加 `asyncio.Lock` per run_id + 自动清理 |
| 执行/事件写入分离 | `dag_executor.py` | `_run_step` → `_execute_step_only`，事件由 `_execute_layer` 统一批量写入 |
| Revise 策略修复 | `planner.py` | `parameters`→`input` 兼容映射；`_REVISE_PROMPT` 加入 intent |
| 最终总结回答 | `scheduler.py` | 新增 `_finalize_with_summary()`，完成时写入 `AgentThought(ANSWER:)` |
| 压缩白名单 | `context_manager.py` | plan_history 纳入摘要，保留步骤参数 |
| 信号量并发控制 | `dag_executor.py` | `asyncio.Semaphore(max_parallel)` 替代语法限制 |
| Mock 压缩禁用 | `serve.py` | `token_limit=0` |
| `max_response_bytes` | `http_request.py` | 默认 4KB → 64KB |
| 顶层导出 | `harness/__init__.py` | 补充 V0.7 类型 |
| P3 代码规范 | 多处 | 中文→英文；函数体 import 修复；未使用参数标记 |

### 7.2 V0.7 运行时修复（2026-06-08）

| 修复 | 文件 | 说明 |
|------|------|------|
| 移除 flattening | `http_request.py` | 删 `result.update(body)`，输出=output_schema，不再运行时添加顶层字段；LLM 通过 `$s1.body.uuid` 访问 JSON body 字段 |
| revise intent 传递 | `scheduler.py` + `planner.py` | `revise()` 新增 `intent_fallback` 参数，4 个调用点传入 `s.intent`；revise 时 LLM 不再看到 `(unknown)` |
| `_parse_plan` 前缀文本 | `planner.py` | JSON 解析失败时回退提取 `{...}` 内容，LLM 先解释再输出 JSON 不再导致解析失败 |

### 7.3 V0.7 输出校验加固（2026-06-11）

| 修复 | 文件 | 说明 |
|------|------|------|
| output_schema 两阶段校验 | `executor.py` | Phase 1: 严格 jsonschema.validate()；Phase 2: `_structurally_usable()` 结构性兜底 — dict/list 通过（ToolCompleted），None/bool/str/int/float 拒绝（ToolFailed） |
| 错误文本脱敏 | `executor.py` | Phase 2 失败时 error 仅含 `type(output).__name__`（如 `got NoneType`），不含原始 API 响应数据；保护 ToolResult 直接展示 + Monitor feedback injected error_detail 两条 LLM 数据通路 |
| 测试覆盖 | `test_tool_layer.py` | 新增 8 项：dict 兜底 / list 兜底 / None 拒绝 / str 拒绝 / bool 拒绝 / int 拒绝 / 脱敏验证 / Schema 匹配不变 |
| 返回值 | — | 355 项测试全通过 |

**两阶段校验数据流**:

```
输出 → Phase 1 (严格 jsonschema)
         ├─ 通过 → ToolCompleted (不变)
         └─ 失败 → Phase 2 (_structurally_usable)
                   ├─ 通过 → ToolCompleted (dict/list 可用，仅 log warning)
                   └─ 失败 → ToolFailed (error 脱敏: "expected structured data, got {type}")
```

**设计原理**: `output_schema` 用于校验工具输出对下游步骤的结构可用性。对于 `http_request` 等工具的变量内容字段（如 `body`），无法在工具定义时预知远端 API 返回格式（对象/数组/字符串/null）。两阶段校验允许严格 schema 在类型不匹配时由结构性兜底判定输出是否仍可导航（`dict`/`list`），而不因过严的类型约束丢弃有效数据。

### 7.4 待做

| 任务 | 说明 | 优先级 | 状态 |
|------|------|--------|------|
| 结构化 tool_calls | `LLMClient.chat -> str` 压平 OpenAI Function Calling 的 `tool_calls` 为文本再正则还原；`tool_call_id` 丢失；多轮历史未按 `assistant.tool_calls + role=tool` 协议回放。详见 `Reviews/structured_tool_calls_review_20260722.md` | **P0** | 🔴 未修 |
| `planner.py` print 残留 | `planner.py:228` 裸 `print(self.registry.list_tool_defs())` — 本次变更中**新增**（非删除），需移除 | P1 | 🔴 回归 |
| 生命周期恢复 | 服务器重启孤儿 Run 检测 + abandon/retry | P0 | 📄 设计完成 |
| 多租户隔离 | `tenant_id` + `RBAC/ToolACL` + `ScopedEventStore` + 按用户隔离语义记忆 + 业务适配器层（详见原 `harness_v2.1.md` §4） | P1 | ⏳ 待做 |
| Predictive Guardrails | PlanRiskReport + self-correction | P1 | ⏳ 待做 |
| 失败原因分类 | schema_error → 重试；tool_unavailable → skip | P1 | ⏳ 待做 |
| 旧 Scheduler 退役 | `serve.py` 切换到 `PlanningExecutorScheduler` | P1 | ⏳ 待做 |
| 分布式 Worker | 多 Worker 共享 Event Store | P1 | ⏳ 待做 |

---

## 8. 技术栈

| 组件 | 开发期 | 生产 |
|------|--------|------|
| Agent 运行时 | Python asyncio | Python asyncio |
| LLM 调用 | OpenAI / DeepSeek SDK | OpenAI / DeepSeek SDK |
| 接口层 | FastAPI | FastAPI + K8s Ingress |
| Event Store | SQLite | PostgreSQL + JSONB |
| 任务队列 | asyncio.Queue | Redis Streams |
| 沙盒执行 | subprocess（进程隔离） | gVisor 容器 |
| 浏览器工具 | Playwright (async) | Playwright (async) |
| MCP 集成 | mcp Python SDK | mcp Python SDK |

---

## 9. 确定性与可追溯性

**确定性边界**: 相同事件流 → 相同工具调用序列 → 相同副作用

Agent 的 thought 文本在重放时可能因上下文截断或 LLM sampling 差异而不同，这是可接受的。

**重放安全**:
1. 从 Event Store 读取所有事件，按 seq 排序
2. 依次折叠事件，恢复 Agent 上下文
3. 工具不会被重新执行，副作用不会重复产生
4. 调试时可在任意 seq 停止，检查该时刻的完整状态

**断点续传**:
1. 读取最近的 `ContextCheckpointed` 事件，加载上下文快照
2. 从快照对应的 seq 之后读取增量事件
3. 将增量事件折叠进上下文
4. Scheduler 恢复 think → act → observe 循环

**Agent 状态与 Worker 状态分离**:
- Agent 逻辑状态: Event Store 永久存储，崩溃不丢失
- Worker 运行时状态: Worker 内存临时存储，崩溃后可丢弃重建，目标恢复时间 < 30 秒

**服务器重启恢复（V0.8）**:
1. 启动时 `app.py` lifespan 调用 `lifecycle.mark_orphans()` 扫描 Event Store
2. 所有 `RUNNING`/`PAUSED` 状态的 run 被写入 `RunOrphaned` 事件（幂等）
3. `fold_events` 设置 `state.orphaned = True`，不影响 `status` 字段
4. 前端展示孤儿标记，用户可选择 `abandon`（写 `RunFailed`）或 `retry`（创建新 Run）
5. 不自动 resume，不自动 fail——系统强制不猜测用户意图

---

## 10. 已知技术债务（Known Technical Debt）

### 10.1 `fold.py: tool_calls/feedbacks` 不截断

`fold_events` 在 `CONTEXT_COMPRESSED` 时截断 `thought_history` 和 `tool_results`，
但绕过 `tool_calls` 和 `feedbacks`。超长 Run 下这两个 list 持续增长。
Run 结束后自然 GC 释放，不影响系统整体，但单 Run 内存峰值可能偏高。

### 10.2 `event_store.py: _seq_locks` 计数淘汰可能漏锁

`_seq_locks` 每 50 次写入检查一次未锁定锁并淘汰之。如果锁恰好被持有跨越
这个计数窗口，会永远留在 dict 里。需改为基于超时的 TTL 淘汰或用
数据库行锁替代。

### 10.3 `guardrails.py: _call_history` 类级字典无生产清理

`RateLimitGuardrail._call_history` 是类级 `dict[str, list[float]]`，
key 按 `(scope, tool_name)` 组合增长，生产环境无清理机制。
`reset()` 仅测试中调用。需改为实例级存储或定时清理。

---

*基于 `harness_v2.1.md` 架构方向，对齐 `AGENTS.md` v2.1 受信边界约束*
