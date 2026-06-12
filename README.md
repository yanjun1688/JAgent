# Harness v2.1

**Agent-First 任务执行引擎** — Agent 拥有决策权，系统拥有强制权。

## 核心范式

与传统 Workflow Engine 不同，Harness 不以 DAG/状态机为一等公民。Agent 自主决策（think → act → observe），系统强制约束（事件写入、Guardrails、幂等校验）。

| 概念 | 说明 |
|------|------|
| **受信边界** | Event Store、Tool Layer、DagExecutor、Context Manager、Scheduler 是受信组件；Agent Kernel (LLM)、Planner、工具实现是非受信组件 |
| **系统强制写入** | 所有事件由系统自动写入 Event Store，Agent/Planner 无法绕过 |
| **Tool Layer 自治** | 幂等键自动计算、Guardrails 前置检查、危险操作挂起确认、Sandbox 统一执行入口 |
| **规划与执行分离 (V0.7)** | Planner (LLM) 只负责输出结构化 JSON Plan；DagExecutor (受信) 按拓扑序并行执行 |
| **挂起恢复机制** | 人工确认不是 Agent 的工具，而是系统级挂起/恢复流程 |
| **状态分离** | Agent 逻辑状态持久化在 Event Store；Worker 运行时状态可丢弃重建 |

## 架构概览

```
┌─────────────────────────────────────────────────┐
│  Interface Layer                                 │
│     REST API / WebSocket / Analysis API          │
├─────────────────────────────────────────────────┤
│  PlanningExecutorScheduler    ← 受信              │
│     控制 Plan→Execute→Revise 循环                │
│     自动事件写入 · 挂起/恢复 · 熔断 · 反馈注入    │
│     降级回退串行 AgentLoopScheduler               │
├─────────────────────────────────────────────────┤
│  Planner (LLM)               ← 非受信            │
│     生成/修订 JSON DAG Plan                      │
│     解析失败自动重试 2 次 → 降级串行路径           │
├─────────────────────────────────────────────────┤
│  DagExecutor                 ← 受信              │
│     Kahn 拓扑排序 · asyncio.gather 同层并行       │
│     上游结果摘要化 · 信号量并发控制               │
├─────────────────────────────────────────────────┤
│  Monitoring & Feedback       ← 受信              │
│     RunMonitor: on_append 实时监听               │
│     异常检测 · 循环检测 · Token 预警 · 反馈注入   │
├─────────────────────────────────────────────────┤
│  Agent Kernel (LLM)          ← 非受信            │
│     think → 选择工具 → 推理决策 (串行回退路径)     │
│     被动接收反馈（不感知监控机制）                │
├────────────────┬────────────────────────────────┤
│  Execution Tools             ← 非受信            │
│  browser() · http_request() · file_op()         │
│  mcp_call() · Skill (多步封装)                  │
├────────────────┴────────────────────────────────┤
│  Tool Layer Infrastructure   ← 受信             │
│  8 步执行: 幂等键 · Guardrails(5种) · 确认流程   │
│  Sandbox · RetryRunner · 输出 Schema 校验       │
│  Context Manager: 自动压缩 + Checkpoint (V0.5)  │
├─────────────────────────────────────────────────┤
│  Event Store (append-only)   ← 受信             │
│  run_id + seq → 不可变事件流                     │
│  asyncio.Lock seq 原子性 · 幂等键唯一约束 · 回调   │
└─────────────────────────────────────────────────┘
```

## 设计原则

- **确定性不来自图的约束**，而来自工具幂等性（相同输入相同副作用）+ 事件流完整性（每步可追溯可重放）
- **所有实际副作用发生在 Tool Layer**，Agent 不直接操作 IO、网络、文件系统
- **Guardrails 是最后一道不可绕过的防线**，不依赖 System Prompt 是否提醒 Agent
- **幂等键由 Tool Layer 自动计算**，Agent 不感知幂等机制的存在
- **沙盒是统一执行入口**：进程内工具经 `Sandbox.invoke()`，子进程工具经 `Sandbox.run()`
- **规划与执行分离 (V0.7)**：LLM 只负责战略（JSON Plan），系统负责战术（DAG 并行执行）
- **系统强制注入状态 (V0.7)**：Revise 前注入 `【系统状态 - 不可折叠】` 摘要，防止 LLM 失忆

## 事件类型 (24 种)

**核心循环**:
```
RunStarted → AgentThought → ToolCalled → ToolCompleted / ToolFailed / ToolTimeout
                                        → GuardrailTriggered
                                        → ConfirmationRequested → ConfirmationReceived
                           → RunPaused → RunResumed
                           → RunCompleted / RunFailed
```

**DAG 规划与执行 (V0.7)**:
```
PlanCreated → DagStepStarted → DagStepCompleted / DagStepFailed
            → PlanRevised → PlanCompleted / PlanFailed
```

**上下文管理 (V0.5+)**:
```
ContextCompressed (结构化 EpisodeSummary) · ContextCheckpointed (快照)
```

**监控反馈 (V0.6)**:
```
FeedbackInjected (由 RunMonitor 自动写入)
```

所有事件 Append-Only 存储在 SQLite，`PRIMARY KEY (run_id, seq)` 保证全局有序，`fold_events()` 纯函数可重建任意时刻状态快照。写入后自动通过 WebSocket 广播给前端。

## 项目结构

```
harness/
├── __init__.py                 # 公共导出
├── models/
│   ├── events.py               # 24 种事件 Payload + EventType 枚举 (Pydantic v2)
│   ├── plan.py                 # DagPlan + DagStep 数据模型 (Pydantic v2, Kahn 拓扑排序)
│   └── tools.py                # ToolDefinition + Guardrail + RetryPolicy + DependencyConstraint
├── storage/
│   └── event_store.py          # SQLite Append-Only Event Store (asyncio.Lock seq 原子性)
├── tools/
│   ├── executor.py             # Tool Executor — 8 步执行流程 (幂等/Guardrails/确认/沙盒/输出校验)
│   ├── guardrails.py           # GuardrailRunner (async) + 5 Guardrail
│   ├── idempotency.py          # 幂等键自动计算 (SHA256, 仅 idempotency_key_fields)
│   ├── sandbox.py              # Sandbox — invoke() 进程内 + run() 子进程
│   ├── retry.py                # RetryRunner — 指数退避 + jitter
│   ├── registry.py             # ToolRegistry — 动态注册/查询/移除
│   ├── browser_tool.py         # 浏览器自动化 (Playwright)
│   ├── http_request.py         # HTTP 客户端
│   ├── file_op.py              # 文件读写操作
│   ├── mcp_call.py             # MCP 工具入口
│   └── skill.py                # 多步技能包 (内层工具路由通过 ToolExecutor)
├── core/
│   ├── fold.py                 # fold_events() → RunState 纯函数 (24 事件全覆盖)
│   ├── scheduler.py            # BaseScheduler(ABC) + AgentLoopScheduler(串行) + PlanningExecutorScheduler(V0.7)
│   ├── planner.py              # Planner (LLM Plan 生成/修订) + PlanGuardrail (V0.7)
│   ├── dag_executor.py         # DagExecutor — 拓扑排序 + 同层并行 + 上游摘要化 (V0.7)
│   ├── agent_kernel.py         # AgentKernel ABC + MockAgentKernel + LLMAgentKernel
│   ├── llm_client.py           # LLMClient 抽象 + MockLLMClient + OpenAILLMClient
│   ├── system_prompt.py        # System Prompt 构建器 + 工具 Schema
│   └── context_manager.py      # Context Manager — 自动压缩 + EpisodeSummary + Checkpoint (V0.5+)
├── monitoring/
│   └── run_monitor.py          # RunMonitor — on_append 实时监控 + 循环/失败检测 + 反馈注入
├── analysis/
│   ├── schemas.py              # 9 个 Pydantic 响应模型 (Dashboard/ToolTrace/RetryableInfo)
│   └── service.py              # AnalysisService — 6 个聚合查询引擎 (V1.0)
└── api/
    ├── app.py                  # FastAPI 应用组装 (CORS, lifespan)
    ├── deps.py                 # HarnessAPI 容器 + DI + start_run + WebSocket 广播
    ├── schemas.py              # 请求/响应模型
    ├── routes.py               # REST 端点 (CRUD + pause/resume/confirm)
    ├── ws.py                   # WebSocket 事件推送
    ├── analysis_routes.py      # 分析 API 6 端点 (Dashboard/Tools/Guardrails/Run/Timeline/Traces)
    └── serve.py                # 生产入口 — 装配 Mock/LLM kernel + 工具

frontend/                       # React + Vite + TypeScript 前端
├── src/
│   ├── api/
│   │   ├── client.ts, schema.ts
│   │   ├── analysis-client.ts, analysis-types.ts, analysis-styles.ts
│   ├── pages/
│   │   ├── RunList.tsx, RunDetail.tsx, Dashboard.tsx
│   │   ├── RunAnalysis.tsx, ToolsPanel.tsx, GuardrailPanel.tsx
│   ├── components/
│   │   ├── ConfirmDialog.tsx, ChatDrawer.tsx, ThinkingPanel.tsx, TraceTree.tsx
│   └── App.tsx
├── public/openapi.json
└── package.json

scripts/
├── generate_openapi.py         # 离线导出 OpenAPI + TypeScript
├── test_llm_dag.py             # V0.7 Planner-Executor 集成测试
└── test_v07_integration.py     # V0.7 端到端冒烟测试

tests/                          # 315 项测试全部通过
├── test_event_store.py         # L1: 23
├── test_fold.py                # 折叠: 24
├── test_tool_layer.py          # L2: 44
├── test_scheduler.py           # L3: 12
├── test_kernel.py              # L4: 16
├── test_tools_v02.py           # V0.2 工具: 26
├── test_api.py                 # V0.3 API: 14
├── test_guardrails_v04.py      # V0.4 Guardrails: 32
├── test_context_manager.py     # V0.5 上下文: 20 (+9 V0.5+)
├── test_monitoring.py          # V0.6 监控: 22
├── test_dag_executor.py        # V0.7 DAG 执行: 26
├── test_planner.py             # V0.7 Planner: 26
└── test_analysis.py            # V1.0 分析: 15

## 开发进度

| 层级 | 组件 | 状态 | 测试 |
|------|------|------|------|
| L1 | Event Store 基础设施 (+ asyncio.Lock seq 原子性) | ✓ 完成 | 23 |
| L2 | Tool Layer 核心 (8 步执行 + 5 Guardrails + 输出校验) | ✓ 完成 | 44 |
| L3 | Agent Loop Scheduler (BaseScheduler 层次重构) | ✓ 完成 | 12 |
| L4 | Agent Kernel 接口 + LLMClient | ✓ 完成 | 16 |
| V0.2 | 工具层 (Registry / browser / http / file / MCP / SKILL) | ✓ 完成 | 26 |
| V0.3 | 可观测性 (REST + WebSocket + React 前端 + DI) | ✓ 完成 | 14 |
| V0.4 | Guardrails (5 种) + 确认流程 + PlanGuardrail | ✓ 完成 | 32 |
| V0.5 | Context Manager 自动压缩 + Checkpoint + 断点续传 | ✓ 完成 | 20 |
| V0.5+ | EpisodeSummary 结构化摘要 + 紧急压缩 | ✓ 完成 | 9 |
| V0.6 | RunMonitor + FeedbackInjected + Scheduler 反馈注入 | ✓ 完成 | 22 |
| V0.6+ | 架构加固: Skill 路由/输出校验/循环检测/side_effects/幂等验证 | ✓ 完成 | 10 |
| V0.7 | Planner-Executor + DAG: Planner/DagExecutor/PlanGuardrail/7 新事件/降级 | ✓ 完成 | 52 |
| V1.0 | 分析平台: AnalysisService + 6 API 端点 + 操作锚点 | ✓ 完成 | 15 |

**总计: 315 项测试，全部通过。**

## 快速开始

### 安装

```bash
pip install -e .
# 前端依赖（如需开发前端）
cd frontend && npm install
```

### 命令行跑一个 Mock Agent (V0.7 PlannerExecutor 模式)

```python
import asyncio
from harness import (
    AgentLoopScheduler, EventStore, ExecutionStatus,
    MockLLMClient, RetryPolicy, SideEffect, ThinkResult,
    ToolDefinition, ToolExecutor, PlanningExecutorScheduler,
    DagExecutor, Planner, ToolRegistry,
)

async def main():
    store = EventStore(":memory:")
    await store.initialize()
    try:
        tool_def = ToolDefinition(
            name="greet", description="Greet someone",
            idempotency_key_fields=["name"], side_effects=[],
            timeout_ms=5000, retry_policy=RetryPolicy(),
        )

        # Mock LLM returns a Plan JSON: 2 steps
        llm = MockLLMClient([
            '{"steps": [{"id":"s1","tool":"greet","input":{"name":"World"}}, {"id":"s2","tool":"greet","input":{"name":"DAG"}}]}',
            '{"steps": []}',  # revise → done
        ])

        registry = ToolRegistry()
        registry.register(tool_def, lambda i: f"Hello, {i['name']}!")
        executor = ToolExecutor(store)

        planner = Planner(llm, registry, store)
        dag = DagExecutor(executor, store, registry)
        scheduler = PlanningExecutorScheduler(
            store, executor, planner, dag,
            registry.list_tool_defs(), registry.list_tool_fns(),
        )

        state = await scheduler.run("run-1", "Greet everyone")
        print(state.status)       # completed

    finally:
        await store.close()

asyncio.run(main())
```

### 启动 API 服务 + 前端

```bash
# 终端 1: 启动后端 (Mock 模式 — 2 步 DAG echo)
uvicorn harness.api.serve:app --reload --port 8000

# 终端 2: 启动前端
cd frontend && npm run dev   # http://localhost:5173

# 创建 run — 自动拉起 PlanningExecutorScheduler
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"intent":"echo hello then world"}'
```

打开 `http://localhost:5173` → 实时事件流：

```
[WS] #1 RunStarted      {intent: "echo hello then world"}
[WS] #2 AgentThought    {thought: "Plan: 2 steps..."}
[WS] #3 PlanCreated     {intent: "...", steps_summary: "2 steps in 2 layers"}
[WS] #4 DagStepStarted  {step_id: "s1", tool_name: "echo"}
[WS] #5 ToolCalled      {tool_name: "echo"}
[WS] #6 ToolCompleted   {tool_name: "echo"}
[WS] #7 DagStepCompleted {step_id: "s1"}
[WS] #8 DagStepStarted  {step_id: "s2", tool_name: "echo", depends_on: ["s1"]}
...
```

## API 端点

### Run 生命周期管理
| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/runs` | 列举所有 Run（分页） |
| POST | `/api/v1/runs` | 创建新 Run（拉起 V0.7 PlanningExecutorScheduler） |
| GET | `/api/v1/runs/{run_id}` | 获取 Run 状态快照 |
| GET | `/api/v1/runs/{run_id}/events` | 获取事件流（分页） |
| POST | `/api/v1/runs/{run_id}/pause` | 暂停 Run |
| POST | `/api/v1/runs/{run_id}/resume` | 恢复 Run |
| POST | `/api/v1/runs/{run_id}/confirm` | 提交操作员确认决策 |
| DELETE | `/api/v1/runs/{run_id}` | 删除 Run |
| WS | `/api/v1/runs/{run_id}/events` | 实时事件流推送 |

### 分析平台 (V1.0)
| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/analysis/dashboard` | 全局概况 (时间窗口) |
| GET | `/api/v1/analysis/tools` | 工具使用统计 |
| GET | `/api/v1/analysis/guardrails` | Guardrail 拦截统计 |
| GET | `/api/v1/analysis/runs/{run_id}` | 单 Run 分析概要 |
| GET | `/api/v1/analysis/runs/{run_id}/timeline` | 分页事件时间线 (游标) |
| GET | `/api/v1/analysis/runs/{run_id}/tool-traces` | 完整工具 Trace 列表 |
| POST | `/api/v1/operations/retry` | 🔜 操作层预留 (501) |

## 里程碑

| 阶段 | 目标 | 状态 |
|------|------|------|
| **MVP** | Event Store + Tool Layer + Scheduler + Agent Kernel 基础循环 | ✓ 完成 |
| **V0.2** | ToolRegistry + browser / http / file_op / MCP / SKILL | ✓ 完成 |
| **V0.3** | FastAPI REST+WS + React 前端 (Run 列表/详情/确认 UI) | ✓ 完成 |
| **V0.4** | ScopeGuardrail / RateLimitGuardrail / DestructiveOpGuardrail / DependencyGuardrail | ✓ 完成 |
| **V0.4+** | Orchestrator 动态编排 + PlanGuardrail + 步骤级安全继承 | ✓ 完成 |
| **V0.5** | Context Manager 自动压缩 + Checkpoint + 断点续传 + 100 轮压力测试 | ✓ 完成 |
| **V0.5+** | EpisodeSummary 结构化摘要 + 紧急压缩 | ✓ 完成 |
| **V0.6** | RunMonitor + FeedbackInjected + Scheduler 反馈注入 + 循环检测 | ✓ 完成 |
| **V0.6+** | 架构加固：Skill 路由 / 输出校验 / side_effects 消费 / 幂等键验证 | ✓ 完成 |
| **V0.7** | **Planner-Executor + DAG**: 规划执行分离 / 拓扑并行 / 系统状态注入 | ✓ 完成 |
| **V1.0** | 分析平台: AnalysisService + 6 API + 操作锚点 + 时间窗口 + 分页 | ✓ 完成 |
| **V1.0+** | 生产就绪：分层记忆 + 权限 + 分布式 Worker + 业务适配 | 🔜 待开始 |

## 技术栈

| 组件 | MVP |
|------|-----|
| Agent 运行时 | Python asyncio |
| LLM 调用 | OpenAI / DeepSeek SDK |
| 接口层 | FastAPI |
| Event Store | SQLite |
| 沙盒执行 | subprocess / asyncio coroutine |
| 浏览器工具 | Playwright (async) |
| 前端 | React 18 + Vite + TypeScript |

## License

MIT
