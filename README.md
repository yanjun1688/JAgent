# Harness v2.2

**Agent-First 任务执行引擎** — Agent 拥有决策权，系统拥有强制权。

## 核心范式

与传统 Workflow Engine 不同，Harness 不以 DAG/状态机为一等公民。Agent 自主决策（think → act → observe），系统强制约束（事件写入、Guardrails、幂等校验）。

| 概念 | 说明 |
|------|------|
| **受信边界** | Event Store、Tool Layer、Sandbox、Context Manager、Scheduler 是受信组件；Agent Kernel (LLM) 和工具实现是非受信组件 |
| **系统强制写入** | 所有 think/act/observe 事件由系统自动写入 Event Store，Agent 无法绕过 |
| **Tool Layer 自治** | 幂等键自动计算、Guardrails 前置检查、危险操作挂起确认、Sandbox 统一执行入口 |
| **挂起恢复机制** | 人工确认不是 Agent 的工具，而是系统级挂起/恢复流程；恢复后自动重新执行原工具调用 |
| **状态分离** | Agent 逻辑状态持久化在 Event Store；Worker 运行时状态可丢弃重建 |

## 架构概览

```
┌─────────────────────────────────────────────────┐
│  Interface Layer                                 │
│     REST API / WebSocket  / CLI                  │
├─────────────────────────────────────────────────┤
│  Agent Loop Scheduler        ← 受信              │
│     控制 think/act/observe 节奏                  │
│     自动事件写入 · 挂起/恢复控制 · 熔断保护       │
├─────────────────────────────────────────────────┤
│  Agent Kernel (LLM)          ← 非受信            │
│     think → 选择工具 → 推理决策                   │
├────────────────┬────────────────────────────────┤
│  Planning Tools│  Execution Tools    ← 非受信    │
│  make_plan()   │  browser()   http_request()    │
│  revise_plan() │  run_code()  file_op()         │
│                │  mcp_call()                    │
├────────────────┴────────────────────────────────┤
│  Tool Registry + Tool Layer Infra  ← 受信       │
│  幂等键 · Guardrails · Sandbox · 超时重试        │
├─────────────────────────────────────────────────┤
│  Event Store (append-only)   ← 受信             │
│  run_id + seq → 不可变事件流                     │
│  PK 冲突重试 · 幂等键唯一约束 · on_append 通知    │
└─────────────────────────────────────────────────┘
```

## 设计原则

- **确定性不来自图的约束**，而来自工具幂等性（相同输入相同副作用）+ 事件流完整性（每步可追溯可重放）
- **所有实际副作用发生在 Tool Layer**，Agent 不直接操作 IO、网络、文件系统
- **Guardrails 是最后一道不可绕过的防线**，不依赖 System Prompt 是否提醒 Agent
- **幂等键由 Tool Layer 自动计算**，Agent 不感知幂等机制的存在
- **沙盒是统一执行入口**：进程内工具经 `Sandbox.invoke()`，子进程工具经 `Sandbox.run()`

## 事件类型 (15 种)

```
RunStarted → AgentThought → ToolCalled → ToolCompleted
                                        → ToolFailed
                                        → ToolTimeout
                                        → GuardrailTriggered
                                        → ConfirmationRequested → ConfirmationReceived
                           → ContextCompressed
                           → ContextCheckpointed (V0.5+)
                           → RunPaused → RunResumed
                           → RunCompleted / RunFailed
```

所有事件 Append-Only 存储在 SQLite，`PRIMARY KEY (run_id, seq)` 保证全局有序，`fold_events()` 纯函数可重建任意时刻状态快照。写入后自动通过 WebSocket 广播给前端。

## 项目结构

```
harness/
├── __init__.py                 # 公共导出
├── models/
│   ├── events.py               # 15 种事件 Payload + EventType 枚举 (Pydantic v2)
│   └── tools.py                # ToolDefinition + Guardrail + RetryPolicy 模型
├── storage/
│   └── event_store.py          # SQLite Append-Only Event Store (PK 冲突重试, on_append 回调)
├── tools/
│   ├── executor.py             # Tool Executor — 8 步执行流程 (幂等/Guardrails/确认/沙盒)
│   ├── guardrails.py           # GuardrailRunner + SchemaGuardrail
│   ├── idempotency.py          # 幂等键自动计算 (SHA256)
│   ├── sandbox.py              # Sandbox — invoke() 进程内 + run() 子进程
│   ├── retry.py                # RetryRunner — 指数退避 + jitter
│   ├── registry.py             # ToolRegistry — 动态注册/查询/移除工具
│   ├── browser_tool.py         # 浏览器自动化 (Playwright)
│   ├── http_request.py         # HTTP 客户端
│   ├── file_op.py              # 文件读写操作
│   ├── mcp_call.py             # MCP 工具入口
│   └── skill.py                # 多步技能包封装
├── core/
│   ├── fold.py                 # fold_events() → RunState 纯函数
│   ├── scheduler.py            # AgentLoopScheduler — think→act→observe 循环
│   ├── agent_kernel.py         # AgentKernel ABC + MockAgentKernel + LLMAgentKernel
│   ├── llm_client.py           # LLMClient 抽象 + MockLLMClient
│   └── system_prompt.py        # System Prompt 构建器 + 工具 Schema 格式化
└── api/
    ├── app.py                  # FastAPI 应用组装 (CORS, lifespan, router include)
    ├── deps.py                 # HarnessAPI 容器 + DI + start_run + WebSocket 广播
    ├── schemas.py              # 请求/响应 Pydantic 模型
    ├── routes.py               # 8 个 REST 端点 + 确认接口
    ├── ws.py                   # WebSocket 事件推送端点
    └── serve.py                # 生产入口 — 装配 Mock/LLM kernel + 工具

frontend/                       # React + Vite 前端
├── src/
│   ├── api/
│   │   ├── client.ts           # API 调用 + WebSocket 连接
│   │   └── schema.ts           # 从 OpenAPI 自动生成的 TypeScript 类型
│   ├── pages/
│   │   ├── RunList.tsx         # Run 列表 (创建 + 自动刷新)
│   │   └── RunDetail.tsx       # 详情页 (事件时间线 + pause/resume/confirm)
│   ├── components/
│   │   └── ConfirmDialog.tsx   # 操作员确认弹窗
│   └── App.tsx
├── public/openapi.json         # OpenAPI schema
└── package.json

scripts/
└── generate_openapi.py         # 离线导出 OpenAPI + TypeScript 类型

tests/
├── test_event_store.py         # L1 测试 (23 tests)
├── test_fold.py                # 事件流折叠测试 (24 tests)
├── test_tool_layer.py          # L2 测试 (44 tests)
├── test_scheduler.py           # L3 测试 (12 tests)
├── test_kernel.py              # L4 测试 (16 tests)
├── test_tools_v02.py           # V0.2 工具测试 (26 tests)
└── test_api.py                 # V0.3 API 测试 (14 tests)
```

## 开发进度

| 层级 | 组件 | 状态 | 测试 |
|------|------|------|------|
| L1 | Event Store 基础设施 | ✓ 完成 | 23 |
| L2 | Tool Layer 核心 | ✓ 完成 | 44 |
| L3 | Agent Loop Scheduler | ✓ 完成 | 12 |
| L4 | Agent Kernel 接口 | ✓ 完成 | 16 |
| V0.2 | 工具层 (Registry / browser / http / file / MCP / SKILL) | ✓ 完成 | 26 |
| V0.3 | 可观测性 (REST + WebSocket + 前端 + DI 重构) | ✓ 完成 | 14 |
| V0.4 | Guardrails + 确认流程完善 | 待开始 | — |
| V0.5 | 长流程稳定性 | 待开始 | — |

**总计: 161 项测试，全部通过。**

## 快速开始

### 安装

```bash
pip install -e .
# 前端依赖（如需开发前端）
cd frontend && npm install
```

### 命令行跑一个 Mock Agent

```python
import asyncio
from harness import (
    AgentLoopScheduler, EventStore, EventType, ExecutionStatus,
    MockAgentKernel, RetryPolicy, SideEffect, ThinkResult,
    ToolDefinition, ToolExecutor,
)

async def main():
    async with EventStore(":memory:") as store:
        tool_def = ToolDefinition(
            name="greet", description="Greet someone",
            idempotency_key_fields=["name"], side_effects=[],
            timeout_ms=5000, retry_policy=RetryPolicy(),
        )

        kernel = MockAgentKernel([
            ThinkResult(thought="Say hello", tool_name="greet",
                        tool_input={"name": "World"}),
            ThinkResult(thought="Done", tool_name=None),
        ])

        executor = ToolExecutor(store)
        tool_fns = {"greet": lambda i: f"Hello, {i['name']}!"}

        scheduler = AgentLoopScheduler(
            store, executor, kernel, [tool_def], tool_fns,
        )

        state = await scheduler.run("run-1", "Greet the user")
        print(state.status)       # completed

asyncio.run(main())
```

### 启动 API 服务 + 前端

```bash
# 终端 1: 启动后端 (Mock 模式 — 配了 3 轮 echo 工具调用)
uvicorn harness.api.serve:app --reload --port 8000

# 终端 2: 启动前端
cd frontend && npm run dev   # http://localhost:3000

# 创建 run 观察事件自动流转
curl -X POST http://localhost:8000/api/v1/runs \
  -H "Content-Type: application/json" \
  -d '{"intent":"echo count to 3"}'
```

打开 `http://localhost:3000` → 点击 Run ID 进入详情 → F12 控制台观察 WebSocket 实时推送：

```
[WS] #1 RunStarted {intent: "echo count to 3"}
[WS] #2 AgentThought {thought: "Step 1: echo hello"}
[WS] #3 ToolCalled {tool_name: "echo"}
[WS] #4 ToolCompleted {tool_name: "echo"}
...
```

## API 端点

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v1/runs` | 列举所有 Run（分页） |
| POST | `/api/v1/runs` | 创建新 Run（自动拉起 Agent 循环） |
| GET | `/api/v1/runs/{run_id}` | 获取 Run 状态快照 |
| GET | `/api/v1/runs/{run_id}/events` | 获取事件流（分页） |
| POST | `/api/v1/runs/{run_id}/pause` | 暂停 Run |
| POST | `/api/v1/runs/{run_id}/resume` | 恢复 Run |
| POST | `/api/v1/runs/{run_id}/confirm` | 提交操作员确认决策 |
| DELETE | `/api/v1/runs/{run_id}` | 删除 Run |
| WS | `/api/v1/runs/{run_id}/events` | 实时事件流推送 |

## 里程碑

| 阶段 | 目标 | 状态 |
|------|------|------|
| **MVP** | `AgentLoopScheduler` + 自动事件写入 + 3 个基础工具 | ✓ 完成 |
| **V0.2** | 工具层完善：browser / http / file_op / MCP / SKILL | ✓ 完成 |
| **V0.3** | 可观测性：REST API + WebSocket + React 前端 | ✓ 完成 |
| **V0.4** | Guardrails + 确认流程完善 | 待开始 |
| **V0.5** | 长流程稳定性：自动压缩 + 断点续传 | 待开始 |
| **V1.0** | 生产就绪：PostgreSQL + 分布式 Worker + 分层记忆 | 待开始 |

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
