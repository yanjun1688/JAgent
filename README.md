# Harness (JAgent)

**Agent-First 任务执行引擎** — Agent 拥有决策权，系统拥有强制权。

> 当前版本：**v3.3**（Workspace 多租户 + S1 完成语义链 + 质量门禁）
> 架构文档见 `JAgent-docs/Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md`

## 核心范式

与传统 Workflow Engine 不同，Harness 不以 DAG/状态机为一等公民。Agent 自主决策（think → act → observe），系统强制约束（事件写入、Guardrails、幂等校验、交付契约完成门）。

| 概念 | 说明 |
|------|------|
| **受信边界** | Event Store、ScopedEventStore、Scheduler、Tool Layer 基础设施、RunMonitor、ContextManager 是受信组件；Agent Kernel (LLM)、Planner、工具实现是非受信组件 |
| **决策权归 Agent，强制权归系统** | Agent 决定"做什么"；受信组件决定"不允许做什么"——系统强制不依赖 Agent 配合 |
| **状态驱动而非流程驱动** | 系统状态由 Event Store 事件流折叠得到；每次 think → act → observe 是一个状态跃迁，产物由系统强制写入 |
| **规划与执行分离 (V0.7+)** | Planner (LLM) 只输出结构化 JSON DAG Plan；DagExecutor (受信) 按 `depends_on` 拓扑序并行执行 |
| **交付契约完成门 (S1)** | `DeliveryContract` 是用户要求 + 最终验收标准；完成门只信契约 + StepResult，机械/交付双维判定（`CompletionVerdict` / `DeliverableVerdict`） |
| **Workspace 边界 (V3.3)** | 边界从静态工具配置升级为受信组件注入的运行时对象（Tenant + Workspace + ExecutionTarget）；Tool Layer 经 ExecutionBackend 强制消费 |
| **多租户逻辑隔离** | 所有业务数据访问经 `ScopedEventStore`（自动 `WHERE tenant_id=?`）；`X-Tenant-Id` Header → contextvar |
| **挂起恢复机制** | 人工确认不是 Agent 的工具，而是系统级挂起/恢复流程（`ConfirmationRequested` → `ConfirmationReceived`） |
| **运行总预算 (Q-07)** | `run_timeout_ms` 是 Run 唯一 deadline，watchdog 到期强制 `RunFailed(run_timed_out)` |

## 架构概览

```
┌──────────────────────────────────────────────────────────┐
│  Interface Layer                                          │
│     REST API / WebSocket / Analysis API / Unified Query   │
│     Middleware: X-Tenant-Id → TenantContext (contextvar)  │
├──────────────────────────────────────────────────────────┤
│  ScopedEventStore        ← 受信                            │
│     自动 WHERE tenant_id=? · workspace/run/conversation    │
├──────────────────────────────────────────────────────────┤
│  Scheduler (PlanningExecutorScheduler)  ← 受信            │
│     classify 预检 → 契约解析 → Plan→Execute→Revise 循环     │
│     DAG 并行执行 · 自动事件写入 · 挂起/恢复 · 熔断          │
│     退化修订守卫 · Planner 失败降级 AgentLoopScheduler      │
│     Q-07 总预算 watchdog · S10 取消/回收                    │
├──────────────────────────────────────────────────────────┤
│  Planner (LLM)             ← 非受信                        │
│     生成/修订 JSON DAG Plan · 自检声明 declared_operations  │
├──────────────────────────────────────────────────────────┤
│  DagExecutor               ← 受信                          │
│     depends_on 拓扑排序 · asyncio.gather 同层并行           │
│     上游结果摘要化 · 信号量并发控制                        │
├──────────────────────────────────────────────────────────┤
│  Monitoring & Feedback     ← 受信                          │
│     RunMonitor: on_append 实时监听 · 反馈注入              │
│     LangfuseTracer · Token 预警 · 循环检测                  │
├──────────────────────────────────────────────────────────┤
│  Context Manager           ← 受信                          │
│     自动压缩 + Checkpoint + 断点续传（token_limit=3000）    │
├──────────────────────────────────────────────────────────┤
│  Agent Kernel (LLM)        ← 非受信                        │
│     think → 选择工具 → 推理决策（串行降级路径）              │
├────────────────┬─────────────────────────────────────────┤
│  Execution Tools           ← 非受信                       │
│  FileOpTool · HttpRequestTool · BrowserTool · McpCallTool │
├────────────────┴─────────────────────────────────────────┤
│  Tool Layer Infrastructure   ← 受信                        │
│    幂等键 · Guardrails(Scope/Whitelist/等) · 确认流程       │
│    ExecutionBackend 注入 · 输出 Schema 校验               │
├──────────────────────────────────────────────────────────┤
│  ExecutionBackend          ← 受信                          │
│     directory / docker(sandbox) / ssh-sftp（载体透明）      │
├──────────────────────────────────────────────────────────┤
│  Event Store (append-only) ← 受信                          │
│     run_id + seq → 不可变事件流 · tenant/workspace 列       │
│     seq 原子性 · 幂等键唯一约束 · 回调 · 终态守卫           │
└──────────────────────────────────────────────────────────┘
```

## 设计原则

- **确定性来自工具幂等性 + 事件流完整性**：相同输入相同副作用、每步可追溯可重放
- **所有实际副作用发生在 Tool Layer**，Agent 不直接操作 IO、网络、文件系统
- **Guardrails 是最后一道不可绕过的防线**，不依赖 System Prompt 是否提醒 Agent
- **幂等键由 Tool Layer 自动计算**，Agent 不感知幂等机制的存在
- **边界注入经受信链路**：Scheduler → ToolExecutor → ExecutionBackend，Agent 无法绕过
- **载体透明**：tool 层与 guardrail 层只与 `ExecutionBackend` 接口交互，不感知目录/容器/远端差异
- **执行依赖唯一**：DAG 步骤间唯一执行依赖是 `DagStep.depends_on`
- **完成判定只信契约**：mutating 覆盖、任务完成判定只认 `DeliveryContract` + `StepResult`

## 事件类型 (38 种)

**核心循环**:
```
RunStarted → DeliveryContractsResolved → AgentThought → ToolCalled
                                          → ToolCompleted / ToolFailed / ToolTimeout
                                          → GuardrailTriggered
                                          → ConfirmationRequested → ConfirmationReceived
                                          → RunPaused → RunResumed → RunCommand
                                          → RunCompleted / RunFailed
```

**DAG 规划与执行**:
```
PlanCreated → DagStepStarted → DagStepCompleted / DagStepFailed / DagStepSkipped
            → PlanRevised → PlanCompleted / PlanFailed
```

**上下文与监控**:
```
ContextCompressed (EpisodeSummary) · ContextCheckpointed · ContextPruned · EpisodeArchived
FeedbackInjected · RunOrphaned · LateEventRejected · PhaseTimedOut · TaskCleanupTimeout
```

**Workspace 审计 (V3.3)**:
```
WorkspaceCreated · WorkspaceUpdated · WorkspaceDeleted
```

**Conversation**:
```
ConversationStarted · ConversationMessage · ConversationEnded
```

所有事件 Append-Only 存储在 SQLite，`PRIMARY KEY (run_id, seq)` 保证全局有序，`fold_events()` 纯函数可重建任意时刻状态快照。写入后自动通过 WebSocket 广播给前端。事件列含 `tenant_id` / `workspace_id` / `is_audit`。

## 项目结构

```
harness/
├── api/                       # FastAPI 接口层
│   ├── app.py                 # 应用组装 (CORS, lifespan, API version 0.3.0)
│   ├── deps.py                # HarnessAPI DI 容器 + start_run + WS 广播
│   ├── routes.py              # REST 端点 (workspace/run/conversation CRUD + confirm/feedback)
│   ├── query.py               # Unified Query — 18 种 type 聚合查询
│   ├── ws.py                  # WebSocket 事件推送 (seq 有序回放 + 实时)
│   ├── analysis_routes.py     # 分析 API (dashboard/tools/guardrails/timeline/traces)
│   ├── serve.py               # 生产入口 — 装配 Real LLM/Mock + 工具 + 日志
│   └── loop.py                # event loop policy (win32 Proactor)
├── core/
│   ├── scheduler/
│   │   ├── base.py            # BaseScheduler — 生命周期/熔断/watchdog/终态守卫
│   │   ├── loop.py            # AgentLoopScheduler — 串行循环 (降级路径)
│   │   └── plan.py            # PlanningExecutorScheduler — 默认主调度器 (Plan→DAG→Revise)
│   ├── contract_extractor.py  # 从 intent 抽取 DeliveryContract (S07 方案 B)
│   ├── planner.py             # Planner (LLM DAG Plan 生成/修订) + PlanGuardrail
│   ├── dag_executor.py        # DagExecutor — 拓扑排序 + 同层并行 + 摘要化
│   ├── dag_types.py           # ExecState/TaskState 正交状态模型 (S1)
│   ├── lifecycle.py           # 启动扫描孤儿 Run → RunOrphaned
│   ├── tenant.py              # TenantContext (contextvar)
│   ├── agent_kernel.py        # AgentKernel ABC + Mock/LLM 实现
│   ├── llm_client.py          # LLMClient + MockLLMClient + OpenAILLMClient
│   ├── context_manager.py     # 自动压缩 + EpisodeSummary + Checkpoint
│   └── fold.py                # fold_events() → RunState 纯函数
├── execution/                 # ExecutionBackend (V3.3)
│   ├── base.py                # 抽象接口 + resolve()
│   ├── local.py               # LocalDirectoryBackend
│   ├── docker.py              # DockerSandboxBackend
│   └── ssh.py                 # SSHSFTPBackend (remote)
├── models/                    # Pydantic v2 数据模型（唯一事实来源）
│   ├── events.py              # 38 种 EventType + Payload
│   ├── workspace.py           # Tenant / Workspace / ExecutionTarget / WorkspaceScope
│   ├── intent.py              # DeliveryContract / OperationContract
│   ├── conversation.py        # Conversation 模型
│   ├── plan.py                # DagPlan + DagStep + DependencyConstraint
│   └── tools.py               # ToolDefinition + Guardrail + RetryPolicy
├── monitoring/
│   ├── run_monitor.py         # on_append 实时监控 + 反馈注入
│   └── langfuse_tracer.py     # Langfuse 追踪
├── storage/
│   ├── event_store.py         # SQLite Append-Only Event Store
│   └── scoped.py              # ScopedEventStore — 租户隔离包装
└── tools/
    ├── executor.py            # ToolExecutor — 幂等/Guardrails/确认/backend 注入
    ├── guardrails.py          # GuardrailRunner + ScopeGuardrail + ToolWhitelistGuardrail
    ├── idempotency.py         # 幂等键自动计算 (SHA256)
    ├── base.py                # BaseTool 抽象 + register_tool (ADR-010)
    ├── file_op.py             # FileOpTool — backend 驱动（无全局沙盒）
    ├── http_request.py        # HttpRequestTool
    ├── browser_tool.py        # BrowserTool (Playwright)
    ├── mcp_call.py / mcp_manager.py  # MCP 工具入口与管理
    ├── sandbox.py / retry.py / registry.py / semantic.py / skill.py

frontend/                      # React 18 + Vite + TypeScript (v0.3.0)
├── src/
│   ├── pages/                 # Workspace / Chat / History / Overview / Ops 系列 / Analysis
│   ├── components/            # 实时面板 / TraceTree / 确认卡片 / 会话侧栏 / 3D 可视化
│   ├── stores/                # runStore / conversationStore / uiStore (zustand)
│   ├── api/                   # 从 OpenAPI 生成的类型 (schema.ts)
│   └── design-system/         # token + 组件库
└── public/openapi.json        # 后端 OpenAPI 规范（自动生成，唯一契约来源）

scripts/
├── generate_openapi.py        # 离线导出 OpenAPI + 前端类型
├── test_llm_dag.py            # Planner-Executor 集成测试
└── test_real_llm_flow.py      # 真实 LLM 流测试

tests/                         # 59 个测试文件，~1109 项测试全部通过
```

## 开发进度

| 层级 | 组件 | 状态 |
|------|------|------|
| L1 | Event Store 基础设施（seq 原子性 + 幂等键唯一约束） | ✓ 完成 |
| L2 | Tool Layer 核心（执行流 + Guardrails + 输出校验） | ✓ 完成 |
| L3 | Scheduler 层次（BaseScheduler + AgentLoop + PlanningExecutor） | ✓ 完成 |
| L4 | Agent Kernel 接口 + LLMClient | ✓ 完成 |
| V0.2-V0.4 | 工具层 / 可观测性 / Guardrails + 确认流程 | ✓ 完成 |
| V0.5-V0.6 | Context Manager 压缩/断点续传 / RunMonitor + 反馈注入 | ✓ 完成 |
| V0.7 | Planner-Executor + DAG（拓扑并行 + 系统状态注入 + 降级） | ✓ 完成 |
| V1.0 | 分析平台（AnalysisService + API + 操作锚点） | ✓ 完成 |
| V0.7.1 (S1) | 任务完成语义链（DeliveryContract 完成门 + ExecState/TaskState 正交） | ✓ 完成 |
| S2-S10 | 契约细化 / PlanGuardrail / 输出引用 / 覆盖检查 / Reviser 限制 / 终态守卫 / 生命周期取消 | ✓ 完成 |
| S11 | 可观测性（统一 Query + Langfuse + 日志角色化） | ✓ 完成 |
| S12 | 回归与真实 LLM 验证（1109 项测试 + 黑盒用例） | ✓ 完成 |
| V3.3 | Workspace 多租户 + 执行载体（directory / docker / remote） | ✓ 完成 |
| Q01-Q08 | 质量门禁与执行依赖分离（ADR-009） | ✓ 完成 |

**测试基线：1109 passed / 2 skipped**（`python -m pytest -q -p no:cacheprovider`，~52s）

## 快速开始

### 安装

```bash
pip install -e .
# 前端依赖（如需开发前端）
cd frontend && npm install
```

### 启动 API 服务 + 前端

```bash
# 终端 1: 启动后端（Real LLM 模式需配置 .env 的 LLM_API_KEY；否则 Mock 模式）
python -m harness.api.serve
#   或: uvicorn harness.api.serve:app --host 0.0.0.0 --port 8000
#   Windows 上建议加 --loop harness.api.loop:event_loop_factory（Proactor，修复 Docker 子进程）

# 终端 2: 启动前端
cd frontend && npm run dev   # http://localhost:5173
```

### 创建 Workspace + Run

```bash
# 1. 创建 workspace（directory 载体）
curl -X POST http://localhost:8000/api/v1/workspaces \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"name":"my-ws","scope":{"target":{"type":"directory","filesystem_root":"data/workspaces/my-ws"}}}'

# 2. 创建 Run — 自动拉起 PlanningExecutorScheduler
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"intent":"write hello into hello.txt","workspace_id":"<workspace_id>"}'

# 3. 订阅实时事件流（WebSocket）
#    ws://localhost:8000/api/v1/runs/{run_id}/events
```

### 环境变量

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `LLM_API_KEY` | — | 存在即进入 Real LLM 模式，否则 Mock 模式 |
| `LLM_MODEL_NAME` | `qwen3.7-max` | LLM 模型名 |
| `LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `HARNESS_RUN_TIMEOUT_MS` | `600000` (10min) | Run 全局总预算（Q-07 watchdog） |
| `JAGENT_WORKSPACE_BASE_DIR` | `data/workspaces` | Workspace 受信基目录 |
| `HARNESS_DB_PATH` | `.harness.db` | SQLite Event Store 路径 |
| `HARNESS_LOG_DIR` | `data/logs` | 轮转日志目录 |
| `HARNESS_PORT` | `8000` | uvicorn 端口 |
| `LANGFUSE_ENABLED` | `false` | Langfuse 追踪开关 |

## API 端点

### Workspace（V3.3）
| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/v1/workspaces` | 创建工作区（target 必须落在受信基目录内） |
| GET | `/api/v1/workspaces` | 列表（分页 + run_count） |
| GET | `/api/v1/workspaces/{workspace_id}` | 详情 |
| PATCH | `/api/v1/workspaces/{workspace_id}` | 更新（写审计事件） |
| DELETE | `/api/v1/workspaces/{workspace_id}` | 删除（`default` 返回 409） |
| GET | `/api/v1/workspaces/{workspace_id}/events` | 审计事件流 |

### Run 生命周期
| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/runs` | 列表（workspace 过滤 + 分页） |
| POST | `/api/v1/runs` | 创建 Run（`client_request_id` 幂等 + 可选 `required_operations`） |
| GET | `/api/v1/runs/{run_id}` | 状态快照（折叠） |
| GET | `/api/v1/runs/{run_id}/events` | 事件流（from_seq + 分页） |
| POST | `/api/v1/runs/{run_id}/pause` | 暂停 |
| POST | `/api/v1/runs/{run_id}/resume` | 恢复 |
| POST | `/api/v1/runs/{run_id}/confirm` | 提交操作员确认（按 confirmation_id 幂等） |
| POST | `/api/v1/runs/{run_id}/feedback` | 操作员反馈注入 |
| DELETE | `/api/v1/runs/{run_id}` | 取消 Run |
| WS | `/api/v1/runs/{run_id}/events` | 实时事件流（回放 + 推送） |

### Conversation
| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/v1/conversations` | 创建会话 |
| GET | `/api/v1/conversations` | 会话列表 |
| GET | `/api/v1/conversations/{id}` | 详情 + 消息 |
| POST | `/api/v1/conversations/{id}/messages` | 发消息（携上下文创建 Run） |
| PATCH | `/api/v1/conversations/{id}` | 更新 title/status |
| DELETE | `/api/v1/conversations/{id}` | 软删除 |

### 分析平台
| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/analysis/dashboard` | 全局概况 |
| GET | `/api/v1/analysis/tools` | 工具使用统计 |
| GET | `/api/v1/analysis/guardrails` | Guardrail 拦截统计 |
| GET | `/api/v1/analysis/runs/{run_id}` | 单 Run 分析概要 |
| GET | `/api/v1/analysis/runs/{run_id}/timeline` | 事件时间线（游标） |
| GET | `/api/v1/analysis/runs/{run_id}/tool-traces` | 工具 Trace 列表 |
| POST | `/api/v1/operations/retry` | 操作层预留 (501) |

### Unified Query（S11）
`GET /api/v1/query?type=<type>&run_id=&since=&until=&page=`
18 种 type：`runs` / `run` / `events` / `dashboard` / `tool-stats` / `guardrail-stats` / `run-analysis` / `timeline` / `tool-traces` / `feedback` / `monitor` / `health` / `tool-defs` / `schedulers` / `mcp` / `plans` / `system` / `ws-clients`

## 技术栈

| 组件 | 选型 |
|------|------|
| Agent 运行时 | Python 3.11 + asyncio |
| LLM 调用 | OpenAI 兼容 SDK（DeepSeek / DashScope / 自定义端点） |
| 接口层 | FastAPI + Uvicorn |
| Event Store | SQLite (aiosqlite, Append-Only) |
| 沙盒执行 | ExecutionBackend：本地目录 / Docker 容器 / SSH-SFTP |
| 浏览器工具 | Playwright (async) |
| 前端 | React 18 + Vite + TypeScript + zustand + three.js |
| 观测 | Langfuse · 统一 Query API |
| 测试 | pytest (asyncio_mode=auto) + ruff + vitest |

## License

MIT
