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
┌─────────────────────────────────────────────┐
│  Interface Layer — REST API / WebSocket     │
├─────────────────────────────────────────────┤
│  Agent Loop Scheduler    ← 受信             │
│  驱动 think/act/observe，自动事件写入        │
│  挂起/恢复控制，熔断保护                     │
├─────────────────────────────────────────────┤
│  Agent Kernel (LLM)       ← 非受信          │
│  think → 选择工具 → 推理决策                 │
├──────────────┬──────────────────────────────┤
│  Tools       │  Tool Layer       ← 受信     │
│  browser()   │  幂等键 · Guardrails          │
│  http()      │  Sandbox 统一执行入口         │
│  run_code()  │  超时重试 · 确认挂起          │
├──────────────┴──────────────────────────────┤
│  Event Store (append-only)   ← 受信         │
│  run_id + seq → 不可变事件流                 │
│  PK 冲突自动重试 · 幂等键唯一约束            │
└─────────────────────────────────────────────┘
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

所有事件 Append-Only 存储在 SQLite，`PRIMARY KEY (run_id, seq)` 保证全局有序，`fold_events()` 纯函数可重建任意时刻状态快照。

## 项目结构

```
harness/
├── __init__.py                 # 公共导出
├── models/
│   ├── events.py               # 15 种事件 Payload + EventType 枚举 (Pydantic v2)
│   └── tools.py                # ToolDefinition + Guardrail + RetryPolicy 模型
├── storage/
│   └── event_store.py          # SQLite Append-Only Event Store (PK 冲突重试)
├── tools/
│   ├── executor.py             # Tool Executor — 8 步执行流程 (幂等/Guardrails/确认/沙盒)
│   ├── guardrails.py           # GuardrailRunner + SchemaGuardrail
│   ├── idempotency.py          # 幂等键自动计算 (SHA256)
│   ├── sandbox.py              # Sandbox — invoke() 进程内 + run() 子进程
│   └── retry.py                # RetryRunner — 指数退避 + jitter
└── core/
    ├── fold.py                 # fold_events() → RunState 纯函数
    ├── scheduler.py            # AgentLoopScheduler — think→act→observe 循环
    ├── agent_kernel.py         # AgentKernel ABC + MockAgentKernel + LLMAgentKernel
    ├── llm_client.py           # LLMClient 抽象 + MockLLMClient
    └── system_prompt.py        # System Prompt 构建器 + 工具 Schema 格式化

tests/
├── test_event_store.py         # L1 测试 (23 tests)
├── test_fold.py                # 事件流折叠测试 (24 tests)
├── test_tool_layer.py          # L2 测试 (44 tests)
├── test_scheduler.py           # L3 测试 (10 tests)
└── test_kernel.py              # L4 测试 (16 tests)
```

## 开发进度

| 层级 | 组件 | 状态 | 测试 |
|------|------|------|------|
| L1 | Event Store 基础设施 | ✓ 完成 | 23 |
| L2 | Tool Layer 核心 | ✓ 完成 | 44 |
| L3 | Agent Loop Scheduler | ✓ 完成 | 10 |
| L4 | Agent Kernel 接口 | ✓ 完成 | 16 |
| L5 | 工具注册与实现 | 待开始 | — |
| L6 | 接口层 (API/WebSocket) | 待开始 | — |
| L7 | 前端可观测性 | 待开始 | — |

## 快速开始

```bash
pip install -e .
```

```python
import asyncio
from harness import (
    AgentLoopScheduler, EventStore, EventType, ExecutionStatus,
    MockAgentKernel, RetryPolicy, SideEffect, ThinkResult,
    ToolDefinition, ToolExecutor,
)

async def main():
    async with EventStore(":memory:") as store:
        # 定义工具
        tool_def = ToolDefinition(
            name="greet", description="Greet someone",
            idempotency_key_fields=["name"], side_effects=[],
            timeout_ms=5000, retry_policy=RetryPolicy(),
        )

        # 模拟 Agent 决策
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
        print(state.tool_results) # [ToolResult(status='completed', output='Hello, World!')]

asyncio.run(main())
```

## 里程碑

| 阶段 | 目标 |
|------|------|
| **MVP** | `AgentLoopScheduler` + 自动事件写入 + 3 个基础工具 |
| **V0.2** | 工具层完善：browser / http / run_code / 幂等 / 重试 |
| **V0.3** | 可观测性：事件流 API + WebSocket + 前端 |
| **V0.4** | Guardrails + 确认流程完善 |
| **V0.5** | 长流程稳定性：自动压缩 + 断点续传 |
| **V1.0** | 生产就绪：PostgreSQL + 分布式 Worker + 分层记忆 |

## 技术栈

| 组件 | MVP |
|------|-----|
| Agent 运行时 | Python asyncio |
| LLM 调用 | OpenAI / DeepSeek SDK |
| 接口层 | FastAPI |
| Event Store | SQLite |
| 沙盒执行 | subprocess / asyncio coroutine |
| 浏览器工具 | Playwright (async) |

## License

MIT
