# Harness v2.1

**Agent-First 任务执行引擎** — Agent 拥有决策权，系统拥有强制权。

## 核心范式

与传统 Workflow Engine 不同，Harness 不以 DAG/状态机为一等公民。Agent 自主决策（think → act → observe），系统强制约束（事件写入、Guardrails、幂等校验）。

| 概念 | 说明 |
|------|------|
| **受信边界** | Event Store、Tool Layer、Context Manager、Scheduler 是受信组件；Agent Kernel (LLM) 和工具实现是非受信组件 |
| **系统强制写入** | 所有 think/act/observe 事件由系统自动写入 Event Store，Agent 无法绕过 |
| **Tool Layer 自治** | 幂等键自动计算、Guardrails 前置检查、危险操作挂起确认，均不依赖 Agent 配合 |
| **挂起恢复机制** | 人工确认不是 Agent 的工具，而是系统级挂起/恢复流程 |
| **状态分离** | Agent 逻辑状态持久化在 Event Store；Worker 运行时状态可丢弃重建 |

## 架构概览

```
┌─────────────────────────────────────────────┐
│  Interface Layer — REST API / WebSocket     │
├─────────────────────────────────────────────┤
│  Agent Loop Scheduler   ← 受信              │
│  控制 think/act/observe 节奏，挂起/恢复     │
├─────────────────────────────────────────────┤
│  Stateful Agent (LLM)    ← 非受信           │
│  think → act → observe 循环                 │
├──────────────┬──────────────────────────────┤
│  Tools       │  Tool Layer      ← 受信      │
│  browser()   │  幂等键 · Guardrails          │
│  http()      │  沙盒隔离 · 超时重试          │
│  run_code()  │  确认挂起                     │
├──────────────┴──────────────────────────────┤
│  Event Store (append-only)   ← 受信         │
│  run_id + seq → 不可变事件流                 │
└─────────────────────────────────────────────┘
```

## 设计原则

- **确定性不来自图的约束**，而来自工具幂等性（相同输入相同副作用）+ 事件流完整性（每步可追溯可重放）
- **所有实际副作用发生在 Tool Layer**，Agent 不直接操作 IO、网络、文件系统
- **Guardrails 是最后一道不可绕过的防线**，不依赖 System Prompt 是否提醒 Agent
- **幂等键由 Tool Layer 自动计算**，Agent 不感知幂等机制的存在

## 事件类型 (14 种)

```
RunStarted → AgentThought → ToolCalled → ToolCompleted
                                       → ToolFailed
                                       → ToolTimeout
                                       → GuardrailTriggered
                                       → ConfirmationRequested → ConfirmationReceived
                          → ContextCompressed
                          → RunPaused → RunResumed
                          → RunCompleted / RunFailed
```

所有事件 Append-Only 存储在 SQLite，`PRIMARY KEY (run_id, seq)` 保证全局有序，`fold_events()` 纯函数可重建任意时刻状态快照。

## 项目结构

```
harness/
  models/events.py     # 14 种事件 Payload + Event 模型 (Pydantic v2)
  storage/event_store.py  # SQLite Append-Only Event Store
  core/fold.py         # 事件流折叠引擎 (fold_events → RunState)
```

## 快速开始

```bash
pip install -e .
```

```python
import asyncio
from harness import EventStore, EventType

async def main():
    async with EventStore(":memory:") as store:
        await store.append_event(
            "run-1",
            EventType.RUN_STARTED,
            {"intent": "搜索 opencode 的 GitHub 仓库", "context_snapshot": {}}
        )
        events = await store.get_events("run-1")
        print(events[0].seq)  # 1

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
| 沙盒执行 | subprocess |
| 浏览器工具 | Playwright (async) |

## License

MIT
