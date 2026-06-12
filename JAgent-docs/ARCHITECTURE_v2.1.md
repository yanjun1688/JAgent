# Harness v2.1 — 架构文档

> **当前阶段**: V0.7 — Planner-Executor + DAG 执行引擎（Phase 5 完成）
> **未来阶段**: V0.8 — 生命周期恢复（设计文档已就绪，待审查实施）
> **基线**: 355 项测试全通过
> **文档版本**: v2.1.5
> **最后更新**: 2026-06-11

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
| **V0.7** | **✅** | **Planner-Executor + DAG 执行引擎（Phase 1-5）** |
| **V0.8** | **📄 设计完成** | **生命周期恢复：服务器重启孤儿检测 + abandon/retry** |
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
| R4 动态条件分支 | `dynamic: true` → 逐层串行 + 每步 revise | `scheduler.py` |
| R5 危险组合漏检 | `_check_dangerous_combinations()` | `planner.py` |
| R6 fold 规则 undefined | 事件分级 fold（不可 fold / 摘要化 / 可跳过） | `fold.py` |

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

### 4.2 事件类型清单（共 38 种）

| 事件类型 | 写入方 | 关键 payload 字段 |
|----------|--------|-------------------|
| `RunStarted` | Scheduler | `intent, context_snapshot` |
| `AgentThought` | Scheduler | `thought, tool_choice, token_count, tool_calls` |
| `ToolCalled` | Tool Layer | `tool_call_id, tool_name, input, idempotency_key` |
| `ToolCompleted` | Tool Layer | `tool_call_id, tool_name, output, duration_ms` |
| `ToolFailed` | Tool Layer | `tool_call_id, tool_name, error, retryable` |
| `ToolTimeout` | Tool Layer | `tool_call_id, tool_name, timeout_ms` |
| `GuardrailTriggered` | Tool Layer | `tool_call_id, tool_name, guardrail_id, reason` |
| `ConfirmationRequested` | Tool Layer | `confirmation_id, tool_call_id, tool_name, input, risk_level` |
| `ConfirmationReceived` | 外部接口 | `confirmation_id, confirmed, operator_id` |
| `ContextCompressed` | Context Mgr | `original_tokens, compressed_tokens, summary_ref` |
| `ContextCheckpointed` | Context Mgr | `checkpoint_seq, snapshot_ref, token_count` |
| `RunPaused` | Scheduler | `reason` |
| `RunResumed` | Scheduler | `resume_from_seq` |
| `RunCompleted` | Scheduler | `result_summary` |
| `RunFailed` | Scheduler/Tool | `final_error, event_count, result_summary` |
| **`RunOrphaned`** | **V0.8 Lifecycle** | `reason, last_seq, timestamp` |
| `FeedbackInjected` | RunMonitor / Operator API | `feedback_text, priority, source(monitor\|operator), category, affected_tool, error_type, error_detail, suggestion, expires_at_seq, resolves_feedback_id` |
| **`PlanCreated`** | **V0.7** | `plan_id, intent, steps_summary, layer_count` |
| **`DagStepStarted`** | **V0.7** | `plan_id, step_id, tool_name, depends_on` |
| **`DagStepCompleted`** | **V0.7** | `plan_id, step_id, output_summary` |
| **`DagStepFailed`** | **V0.7** | `plan_id, step_id, error, retryable` |
| **`PlanRevised`** | **V0.7** | `plan_id, revision_reason, remaining_steps_summary` |
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
    dynamic: bool = False                  # true→退化逐层串行+revise

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
    async def pause(run_id) -> None          # 写 RunPaused 事件
    async def resume(run_id) -> None         # 写 RunResumed 事件
    async def cancel(run_id) -> None         # 设置 cancel flag
    def is_active(run_id) -> bool
    def is_paused(run_id) -> bool

    # V0.8: Checkpoint 恢复
    async def _try_checkpoint_recovery(events) -> bool  # 从 ContextManager 查找 checkpoint

**Cleanup 契约**: `run()` 的 `finally` 块保证清理：
  1. `_running_tasks` / `_cancel_flags` / `_pause_events`（Scheduler 内部 run-scoped dict）
  2. `monitor.cleanup(run_id)`（RunMonitor 的 15 个 per-run 字典）
  3. `run_end_cb(run_id)`（API 层注册的回调，清理 `_schedulers` / `_ws_clients`）
  4. 异常/取消路径均走同一 `finally`，无遗漏。

class AgentLoopScheduler(BaseScheduler):
    # 旧串行调度器：think → act → observe
    # 当前 serve.py 默认使用此调度器
    # 在 Planner 全失败时用作降级目标

class PlanningExecutorScheduler(BaseScheduler):
    # V0.7 新调度器：Plan → Execute(并行) → Revise
    def __init__(self, store, executor, planner, dag_executor, ...)
    async def _plan_execute_revise_loop(run_id, intent) -> RunState
        # 主循环：plan() → 动态或静态 DAG 执行 → revise() → 迭代
        # 静态 DAG: 同层并行 asyncio.gather, 逐层执行
        # 动态 DAG: 每个 step 串行, 每步后 revise
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
    orphaned: bool                           # V0.8: 孤儿标记（服务器重启后与 Scheduler 失联）
    plan_history: list[dict]                 # V0.7: DAG 规划历史
    latest_plan: dict | None                 # V0.7: 当前最新计划
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

### 5.3 动态 Plan 流

```
Planner.plan() → DagPlan(dynamic=True, steps=[s1, s2])
  → 写入 AgentThought + PlanCreated
  → for step in plan.steps:
    → 1. DagExecutor 执行当前 step
    → 2. Planner.revise() 检查是否继续
    → 3. revise 返回 empty → 任务完成
    → 4. revise 返回非空 → 循环继续
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
│   ├── scheduler.py          # [V0.7] AgentLoopScheduler + BaseScheduler + PlanningExecutorScheduler
│   ├── lifecycle.py          # [V0.8] 孤儿 Run 检测 + mark/abandon/retry（与 Scheduler 解耦）
│   ├── planner.py            # [V0.7] Planner + PlanGuardrail
│   ├── dag_executor.py       # [V0.7] DagExecutor + build_dag_status_text
│   ├── fold.py               # [V0.7] plan_history + 新事件 fold; [V0.8] orphaned 标记
│   ├── context_manager.py    # 自动压缩 + Checkpoint
│   ├── agent_kernel.py       # LLMAgentKernel + MockAgentKernel
│   ├── llm_client.py         # OpenAILLMClient + MockLLMClient
│   ├── system_prompt.py      # System Prompt 构建
│   ├── logger.py             # 日志工具
│   └── __init__.py           # 导出全部新类
├── models/
│   ├── plan.py               # [V0.7] DagStep + DagPlan
│   ├── events.py             # [V0.7] 7 个新事件类型 + Payload
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
| 生命周期恢复 | 服务器重启孤儿 Run 检测 + abandon/retry | P0 | 📄 设计完成 |
| Predictive Guardrails | PlanRiskReport + self-correction | P1 | ⏳ 待做 |
| 失败原因分类 | schema_error → 重试；tool_unavailable → skip | P1 | ⏳ 待做 |
| 旧 Scheduler 退役 | `serve.py` 切换到 `PlanningExecutorScheduler` | P1 | ⏳ 待做 |
| 分布式 Worker | 多 Worker 共享 Event Store | P1 | ⏳ 待做 |
| 多租户隔离 | `tenant_id` + `ScopedEventStore` | P1 | ⏳ 待做 |

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

## 7. 已知技术债务（Known Technical Debt）

### 7.1 `fold.py: tool_calls/feedbacks` 不截断

`fold_events` 在 `CONTEXT_COMPRESSED` 时截断 `thought_history` 和 `tool_results`，
但绕过 `tool_calls` 和 `feedbacks`。超长 Run 下这两个 list 持续增长。
Run 结束后自然 GC 释放，不影响系统整体，但单 Run 内存峰值可能偏高。

### 7.2 `event_store.py: _seq_locks` 计数淘汰可能漏锁

`_seq_locks` 每 50 次写入检查一次未锁定锁并淘汰之。如果锁恰好被持有跨越
这个计数窗口，会永远留在 dict 里。需改为基于超时的 TTL 淘汰或用
数据库行锁替代。

### 7.3 `guardrails.py: _call_history` 类级字典无生产清理

`RateLimitGuardrail._call_history` 是类级 `dict[str, list[float]]`，
key 按 `(scope, tool_name)` 组合增长，生产环境无清理机制。
`reset()` 仅测试中调用。需改为实例级存储或定时清理。

---

*基于 `harness_v2.1.md` 架构方向，对齐 `AGENTS.md` v2.1 受信边界约束*
