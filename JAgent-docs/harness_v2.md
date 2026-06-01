# Harness — Agent-First 任务执行引擎架构方案 v2.1

> 内部设计文档 · 2025
> v2.1 基于架构评审意见更新：明确受信边界，修正事件写入模型，重新设计确认流程

---

## 目录

1. [背景与核心设计转变](#1-背景与核心设计转变)
2. [设计目标](#2-设计目标)
3. [总体架构](#3-总体架构)
4. [Stateful Agent 设计](#4-stateful-agent-设计)
5. [Tool Layer 设计](#5-tool-layer-设计)
6. [事件存储 Event Store](#6-事件存储-event-store)
7. [确定性与可追溯性保证](#7-确定性与可追溯性保证)
8. [技术栈与部署](#8-技术栈与部署)
9. [里程碑规划](#9-里程碑规划)
10. [风险与缓解](#10-风险与缓解)
11. [评审检查点](#11-评审检查点)

---

## 1. 背景与核心设计转变

### 1.1 v1.0 的根本局限

v1.0 采用五层 Planner-Compiler-Executor 架构，本质是以 DAG/状态机为一等公民的 Workflow Engine。

> ⚠️ **核心问题**：执行图必须在执行前完全确定。Planner 生成图，Compiler 编译图，Executor 沿图的边走——Agent 是图的囚徒，灵活性只能靠 RePlan hack 找回。

具体三个痛点：

- **图先于世界存在**：计划在执行前必须完整确定，但复杂任务的执行结果本身会改变对下一步的认知
- **Compiler 层是伪需求**：将 Plan DSL 编译为 FSM 的复杂度，解决的是自己制造的问题
- **确定性与可追溯性被错误捆绑到图上**：这两个目标完全可以通过工具契约 + 事件流实现，不需要图先于执行存在

### 1.2 新的基础假设

**Agent 拥有决策权，系统拥有强制权。**

决策权归 Agent（选什么工具、怎么推理、是否规划），强制权归系统（必须写事件、必须过 Guardrails、必须等确认）。两者边界清晰是架构自洽的前提。

| 原五层 | v2.0 对应 |
|--------|-----------|
| Planner | `make_plan()` 工具，可选调用 |
| Compiler | 消失，计划是数据不是编译产物 |
| Executor Loop | Agent 自身的 think → act → observe 循环 |
| Event Store | 基础设施强制写入，Agent 无法绕过 |
| Sandbox | Tool Layer 的沙盒执行环境 |

---

## 2. 设计目标

| 目标 | 定义 | 实现机制 | 验收标准 |
|------|------|----------|----------|
| 工具幂等性 | 相同输入 + 相同幂等键 → 相同副作用 | Tool Layer 根据契约自动计算幂等键并校验 | 重复调用不产生多余副作用 |
| 事件流完整性 | 每次 think-act-observe 产生不可变事件 | 系统自动写入，Agent 无法干预，序列号严格递增 | 可基于事件流折叠出任意时刻状态 |
| 行为约束 | Agent 不会超出授权范围行动 | Tool Layer Guardrails（硬约束）+ System Prompt（软约束） | 危险操作必须经过人工确认才可执行 |
| 长流程稳定 | 超长任务不因上下文溢出失败 | MVP: LLM 原生上下文；V0.5+: 基础设施自动压缩 | MVP 支持 128K token 任务不中断 |

> 💡 **设计原则**：确定性不来自图的约束，而来自两个正交保证——工具的幂等性（相同输入相同副作用）和事件流的完整性（每步可追溯可重放）。

---

## 3. 总体架构

### 3.1 架构概览

```
┌─────────────────────────────────────────────────┐
│  Interface Layer                                 │
│     REST API / WebSocket / CLI                   │
├─────────────────────────────────────────────────┤
│  Agent Loop Scheduler        ← 受信组件          │
│     控制 think/act/observe 节奏                  │
│     负责确认等待时挂起/恢复 Agent 循环            │
├─────────────────────────────────────────────────┤
│  Stateful Agent (LLM Kernel) ← 非受信组件        │
│     think → act → observe 循环                   │
│     持有当前上下文窗口（Context Window）          │
│     System Prompt 引导决策行为                   │
├────────────────┬────────────────────────────────┤
│  Planning Tools│  Execution Tools    ← 非受信    │
│  make_plan()   │  browser()   http_request()    │
│  revise_plan() │  run_code()  file_op()         │
│                │  mcp_call()                    │
├────────────────┴────────────────────────────────┤
│  Tool Layer Infrastructure   ← 受信组件          │
│  幂等键自动计算 │ Guardrails │ 沙盒隔离 │ 超时重试│
├─────────────────────────────────────────────────┤
│  Context Manager             ← 受信组件          │
│     自动监控上下文长度，接近阈值时触发压缩         │
│     Agent 无感知（V0.5+）                        │
├─────────────────────────────────────────────────┤
│  Event Store (append-only)   ← 受信组件          │
│     系统自动写入，Agent 无法绕过或伪造             │
│     run_id + seq → 不可变事件流                  │
└─────────────────────────────────────────────────┘
```

### 3.2 受信边界

架构的核心设计原则是明确区分**受信组件**和**非受信组件**：

| 受信组件 | 职责 | 为什么必须受信 |
|----------|------|----------------|
| Event Store | 强制写入每个 think/act/observe 的产物 | Agent 可能遗漏或伪造，破坏可追溯性 |
| Tool Layer | 强制幂等校验、Guardrails、挂起确认 | Agent 的 LLM 推理是概率性的，不可靠 |
| Context Manager | 自动压缩，Agent 无感知 | Agent 无法准确判断何时该压缩 |
| Agent Loop Scheduler | 控制循环节奏，管理挂起/恢复 | 确认等待、错误熔断等需要外部控制 |

| 非受信组件 | 职责 | 约束方式 |
|------------|------|----------|
| Agent Kernel (LLM) | 推理、决策、工具选择 | 所有输出经受信组件校验后才生效 |
| 工具实现 | 执行业务逻辑 | 沙盒隔离 + Guardrails 前置检查 |

### 3.3 核心约束

> **约束 1**：所有实际副作用必须发生在 Tool Layer。Agent 不直接操作 IO、网络、文件系统。

> **约束 2**：幂等键由 Tool Layer 根据工具契约自动计算，Agent 不感知幂等机制的存在。

> **约束 3**：每次 think-act-observe 循环后，系统自动向 Event Store 写入对应事件，不依赖 Agent 主动触发。

> ⚠️ **约束 4**：危险操作的拦截由 Tool Layer Guardrails 负责，**与 System Prompt 是否提醒 Agent 无关**。Guardrails 是最后一道不可绕过的防线。

---

## 4. Stateful Agent 设计

### 4.1 think → act → observe 循环

Agent Loop Scheduler 控制循环的执行节奏，每轮循环由系统驱动而非 Agent 自驱：

```
Agent Loop Scheduler 驱动：

① THINK
   将上下文窗口 + System Prompt + 工具定义送入 LLM
   LLM 输出 thought + tool_call
   ↓
   系统自动写入 AgentThought 事件（Agent 无法干预）

② ACT
   Tool Layer 接收 tool_call
   - 自动计算幂等键
   - 执行 Guardrails 前置检查
   - 通过则在沙盒中执行
   系统自动写入 ToolCalled / GuardrailTriggered 事件

③ OBSERVE
   工具返回结果，追加到 Agent 上下文
   系统自动写入 ToolCompleted / ToolFailed 事件

④ SCHEDULE
   Scheduler 判断：继续循环 / 任务完成 / 等待确认 / 熔断
```

**"Stateful" 的含义**：Agent 的逻辑状态（推理历史、工具调用结果）持久化在 Event Store 中。Worker 崩溃时，新 Worker 从 Event Store 恢复上下文，接续执行。Agent 状态不绑定到特定进程。

### 4.2 System Prompt 行为约束

System Prompt 是**引导 Agent 做出正确决策的软约束**，不是安全防线（安全防线是 Guardrails）。

**① 身份与目标声明**

明确 Agent 的角色、当前任务目标、允许操作的资源范围。

**② 工具使用规范**

- 步骤超过 3 步的复杂任务，建议先调用 `make_plan()` 规划，再按计划执行
- 工具调用失败超过 N 次后，不再重试，调用 `fail_with_reason()` 终止任务
- 不允许自行推断已完成的步骤，应从 `get_run_events()` 读取实际执行记录

**③ 异常处理规范**

- 遇到意外情况（工具输出与预期不符）：先记录观察结果，再决定继续还是修改计划
- 遇到无法处理的错误：调用 `fail_with_reason()` 终止任务，不要无限重试

**④ 输出格式要求**

工具调用走 structured output（JSON Schema 约束），确保参数可被解析。

### 4.3 `make_plan()` 的正确使用方式

`make_plan()` 的输出是**临时草稿，不是执行契约**：

- 计划生成后仅作为 Agent 当前轮次的参考，**不长期驻留上下文**
- Agent 偏离计划时，直接执行新的工具调用，无需通知系统
- 需要重新规划时，再次调用 `make_plan()`，旧计划自然失效
- 如果 Agent 能通过内部推理（CoT）完成任务分解，可以完全不调用 `make_plan()`

计划的价值在于结构化复杂任务的分解，而不是约束执行路径。

### 4.4 上下文管理

**MVP 阶段**：使用 LLM 原生上下文窗口。单次 Run 控制在 ≤ 30 轮工具调用，超出时在产品层拆分任务。

**演进路径**：

| 阶段 | 机制 | Agent 感知 |
|------|------|-----------|
| MVP | 原生上下文，控制任务粒度 | 有感知，需主动拆分 |
| V0.5 | Context Manager 自动压缩（滚动摘要） | **无感知**，基础设施行为 |
| V1.0 | 分层记忆：Working / Episodic / Semantic | 无感知 |
| V1.0+ | 向量化检索，按需注入相关片段 | 无感知 |

> V0.5 之后，上下文压缩是 Context Manager（受信组件）的自动行为，由系统监控 token 使用量，在接近阈值时触发，不占用 Agent 的决策轮次。

---

## 5. Tool Layer 设计

### 5.1 统一工具契约

所有工具——MCP 工具、SKILL（预定义技能包）、浏览器自动化、代码执行——均遵循同一套契约：

```typescript
interface ToolDefinition {
  name: string                       // 全局唯一工具标识符（必须）
  description: string                // Agent 可读的能力描述（必须）
  input_schema: JSONSchema           // 输入参数声明（必须）
  output_schema: JSONSchema          // 输出结构声明（必须）
  idempotency_key_fields: string[]   // Tool Layer 用此字段集合计算幂等键（必须）
  side_effects: SideEffect[]         // 副作用类型：write / delete / external（必须）
  timeout_ms: number                 // 单次调用超时上限（必须）
  retry_policy: RetryPolicy          // 重试策略：次数、退避、可重试错误类型（必须）
  guardrails?: Guardrail[]           // 前置检查列表，失败则拒绝执行（可选）
  requires_confirmation?: boolean    // 是否需要人工确认才可执行（可选）
}
```

**SKILL 说明**：SKILL 是预定义的多步技能包，对外表现为单个工具（有 `name`、`input_schema`、`output_schema`），内部可以包含多步操作。Agent 调用 SKILL 和调用普通工具的方式完全相同，无需区分。

### 5.2 幂等性保证

**幂等键由 Tool Layer 自动计算，Agent 不感知：**

```
幂等键 = hash(tool_name + canonicalize(input[idempotency_key_fields]))
```

- Agent 只提供工具调用的输入参数，Tool Layer 自动提取 `idempotency_key_fields` 中声明的字段，计算幂等键
- 同一个 Run 内，相同幂等键的工具调用，第二次调用直接返回第一次的结果，不重复执行
- Agent 甚至不需要知道幂等键的存在

**Tool Layer 执行流程：**

```
接收 tool_call (tool_name, input)
  │
  ├─ 1. Schema 校验（SchemaGuardrail）
  ├─ 2. 自动计算幂等键
  ├─ 3. 查询 Event Store：此幂等键是否已有 ToolCompleted 事件？
  │      ├─ 是 → 直接返回缓存结果（无副作用）
  │      └─ 否 → 继续
  ├─ 4. 执行 Guardrails 前置检查
  │      ├─ 通过 → 继续
  │      └─ 失败 → 写入 GuardrailTriggered 事件，拒绝执行
  ├─ 5. requires_confirmation 检查
  │      ├─ false → 继续
  │      └─ true → 写入 ConfirmationRequested，触发挂起流程（见 5.3）
  └─ 6. 在沙盒中执行工具，写入 ToolCompleted / ToolFailed 事件
```

**重放安全分级：**

| 工具类型 | 幂等处理 |
|----------|----------|
| 只读查询 | 天然幂等，`idempotency_key_fields` 可为空 |
| 本地写操作 | 必须声明 `idempotency_key_fields`，Tool Layer 查重 |
| 外部系统写入 | 需外部系统支持幂等 API；不支持则标记 `requires_confirmation: true` |

### 5.3 人工确认流程（挂起/恢复机制）

人工确认**不是 Agent 调用的工具**，而是系统的挂起/恢复机制：

```
① Agent 调用危险工具（requires_confirmation: true）
         │
         ▼
② Tool Layer 拦截
   写入 ConfirmationRequested 事件
   通知 Agent Loop Scheduler
         │
         ▼
③ Agent 循环挂起（非终止，保留完整上下文）
   通过 WebSocket / Webhook 向操作员发送确认请求
         │
         ▼
④ 操作员通过外部接口查看详情并决策
   系统写入 ConfirmationReceived 事件（confirmed: true/false）
         │
         ├─ confirmed: false → 写入 ToolFailed，Agent 循环恢复，处理拒绝结果
         │
         └─ confirmed: true  → Agent 循环恢复
                               Tool Layer 重新执行原工具调用
                               此时 Guardrail 识别已确认，放行
                               写入 ToolCompleted 事件
```

整个确认过程完整记录在事件流中，可追溯可重放。Agent 在恢复后通过事件流感知确认结果，不依赖任何内存状态。

### 5.4 Guardrails 前置检查框架

Guardrails 在 Tool Layer 实现，与 Agent 推理逻辑完全解耦。**即使 System Prompt 没有提醒 Agent，Guardrails 仍然生效。**

| Guardrail 类型 | 检查内容 |
|----------------|----------|
| `SchemaGuardrail` | 输入参数是否符合 JSON Schema（第一道，最轻量） |
| `ScopeGuardrail` | 操作目标是否在授权范围内（如：只允许操作指定目录） |
| `RateLimitGuardrail` | 单位时间内同类工具调用次数是否超限 |
| `DestructiveOpGuardrail` | 是否为不可逆操作，是则强制触发确认流程 |
| `DependencyGuardrail` | 前置步骤是否已完成（通过 Event Store 查询） |

### 5.5 工具分类

#### 规划工具（Planning Tools，可选使用）

| 工具 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `make_plan` | `intent, context` | `Plan`（步骤列表） | LLM 生成，JSON Schema 约束输出 |
| `revise_plan` | `plan, observation` | `Plan` | 基于执行结果修订 |
| `check_plan` | `plan` | `ValidationResult` | 校验工具名和参数合法性 |

规划工具的输出是**临时草稿**，不是执行契约，不长期驻留上下文。

#### 执行工具（Execution Tools）

| 工具 | 说明 |
|------|------|
| `browser` | 浏览器自动化（Playwright 封装） |
| `http_request` | HTTP 调用 |
| `run_code` | 沙盒代码执行 |
| `file_op` | 文件读写操作 |
| `mcp_call` | MCP 工具 / SKILL 统一调用入口 |

#### 控制工具（Control Tools）

| 工具 | 说明 | 受信 |
|------|------|------|
| `fail_with_reason` | 主动终止任务并记录原因 | 否（Agent 调用） |
| `get_run_events` | 读取当前 Run 的事件流 | 否（Agent 调用） |
| `get_run_state` | 折叠事件流得到当前状态快照 | 否（Agent 调用） |

> 注意：事件写入、上下文压缩、确认触发均由受信组件自动完成，不在工具列表中。

---

## 6. 事件存储 Event Store

### 6.1 设计原则

- **Append-Only**：物理禁止 UPDATE 和 DELETE
- **系统强制写入**：每个 think/act/observe 的产物由 Agent Loop Scheduler 和 Tool Layer 自动写入，不依赖 Agent 主动调用
- **全局有序**：复合主键 `(run_id, sequence_number)`，seq 严格递增不可跳过
- **可折叠**：任意时刻状态 = `fold(events[0..t])`，无需维护独立状态表

### 6.2 事件类型

| 事件类型 | 写入方 | 触发时机 | 关键字段 |
|----------|--------|----------|----------|
| `RunStarted` | Scheduler | 任务启动 | `run_id, intent, context_snapshot` |
| `AgentThought` | Scheduler | LLM 输出 thought 后自动写入 | `thought, tool_choice, token_count` |
| `ToolCalled` | Tool Layer | 工具调用发起时自动写入 | `tool_name, input, idempotency_key` |
| `ToolCompleted` | Tool Layer | 工具执行成功时自动写入 | `tool_name, output, duration_ms` |
| `ToolFailed` | Tool Layer | 工具执行失败时自动写入 | `tool_name, error, retryable` |
| `ToolTimeout` | Tool Layer | 工具执行超时时自动写入 | `tool_name, timeout_ms` |
| `GuardrailTriggered` | Tool Layer | 前置检查未通过时自动写入 | `tool_name, guardrail_id, reason` |
| `ConfirmationRequested` | Tool Layer | 危险操作被拦截时自动写入 | `tool_name, input, risk_level` |
| `ConfirmationReceived` | 外部接口 | 操作员提交决策 | `confirmed, operator_id` |
| `ContextCompressed` | Context Manager | 自动压缩触发时写入 | `original_tokens, compressed_tokens, summary_ref` |
| `RunPaused` | Scheduler | 暂停执行 | `reason` |
| `RunResumed` | Scheduler | 恢复执行 | `resume_from_seq` |
| `RunCompleted` | Scheduler | LLM 输出停止信号后自动写入 | `result_summary` |
| `RunFailed` | Scheduler / Tool Layer | 熔断或 `fail_with_reason` | `final_error, event_count` |

### 6.3 Schema

**MVP：SQLite**

```sql
CREATE TABLE events (
  run_id          TEXT    NOT NULL,
  seq             INTEGER NOT NULL,
  event_type      TEXT    NOT NULL,
  payload         JSON    NOT NULL,
  idempotency_key TEXT,
  created_at      REAL    NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE UNIQUE INDEX idx_idem
  ON events(run_id, event_type, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

**生产：PostgreSQL**

- `JSONB` 类型存储 payload，支持 GIN 索引加速 payload 字段查询
- 分区表按 `run_id` hash 分区，支持水平扩展
- 物化视图 `run_state` 异步投影，加速状态查询
- WAL 复制保证高可用

---

## 7. 确定性与可追溯性保证

### 7.1 确定性的边界定义

> ⚠️ **重要**：v2.1 明确承认 Agent 推理过程（thought）的不确定性，不对其做确定性承诺。

**MVP 阶段的确定性边界**：相同事件流 → 相同工具调用序列 → 相同副作用

Agent 的 thought 文本在重放时可能因上下文截断或 LLM sampling 差异而不同，这是可接受的。

| 机制 | v1.0 实现 | v2.1 实现 |
|------|-----------|-----------|
| 执行确定性 | FSM 状态转移为纯函数 | 工具幂等键 + Tool Layer 缓存重放 |
| 行为约束 | Compiler 注入 Guardrails 到 FSM | Tool Layer Guardrails（硬）+ System Prompt（软） |
| 可追溯性 | 事件流折叠重建状态 | 事件流折叠重建状态（机制相同，但写入方从 Agent 改为系统） |
| 失败恢复 | 快照 + 状态机断点加载 | Event Store 折叠恢复上下文 + 最近 checkpoint 加速 |

### 7.2 重放安全性

给定同一个 `run_id` 的完整事件流：

1. 从 Event Store 读取所有事件，按 seq 排序
2. 依次折叠事件，恢复 Agent 上下文（工具调用结果通过幂等键从缓存返回）
3. 工具不会被重新执行，副作用不会重复产生
4. 调试时可在任意 seq 停止，检查该时刻的完整状态

### 7.3 断点续传

```
1. 读取最近的 ContextCheckpointed 事件，加载上下文快照
2. 从快照对应的 seq 之后读取增量事件
3. 将增量事件折叠进上下文（Agent "回忆"已发生的事情）
4. Scheduler 恢复 think → act → observe 循环
```

未记录 checkpoint 时：从 `RunStarted` 开始折叠全部事件。MVP 阶段 Context Manager 每 10 轮自动触发 checkpoint。

### 7.4 Agent 状态与 Worker 状态的分离

| | Agent 逻辑状态 | Worker 运行时状态 |
|--|----------------|-------------------|
| **内容** | 推理历史、工具调用结果、任务进度 | LLM 连接、沙盒进程、浏览器实例 |
| **持久化** | Event Store（永久） | Worker 内存（临时） |
| **崩溃行为** | 不丢失，可从 Event Store 恢复 | 丢弃，新 Worker 重建 |
| **扩展方式** | 随 Event Store 水平扩展 | Worker 无状态水平扩展 |

Worker 崩溃后，新 Worker 读取 Event Store 恢复 Agent 状态，重新建立 LLM 连接和沙盒。目标恢复时间 < 30 秒。

---

## 8. 技术栈与部署

### 8.1 技术选型

| 组件 | MVP | 生产 | 理由 |
|------|-----|------|------|
| Agent 运行时 | Python asyncio | Python asyncio | 原生协程，工具调用天然异步 |
| LLM 调用 | OpenAI / DeepSeek SDK | OpenAI / DeepSeek SDK | 异步调用，structured output |
| 接口层 | FastAPI | FastAPI + K8s Ingress | 原生异步，Pydantic 类型安全 |
| Event Store | SQLite | PostgreSQL + JSONB | MVP 零配置；生产支持分区和复制 |
| 任务队列 | asyncio.Queue | Redis Streams | MVP 单进程；生产分布式调度 |
| 沙盒执行 | subprocess（进程隔离） | gVisor 容器 | MVP 简单；生产强隔离 |
| 浏览器工具 | Playwright (async) | Playwright (async) | 异步原生，会话隔离性强 |
| MCP 集成 | mcp Python SDK | mcp Python SDK | 官方 SDK，统一 Tool 接口 |

### 8.2 Worker 内部结构

```
Worker Process (Python asyncio)
  │
  ├─ Agent Loop Scheduler           ← 受信，控制循环节奏
  │   ├─ 驱动 think/act/observe
  │   ├─ 监听 ConfirmationRequested → 挂起循环
  │   ├─ 监听 ConfirmationReceived  → 恢复循环
  │   └─ 自动写入 AgentThought / RunCompleted / RunFailed
  │
  ├─ Agent Instance                 ← 非受信，LLM 推理
  │   ├─ Context Window（当前上下文）
  │   └─ Tool Registry（工具定义，运行时动态加载）
  │
  ├─ Tool Executor                  ← 受信，强制执行契约
  │   ├─ Idempotency Checker（自动计算 + 查重）
  │   ├─ Guardrail Runner（前置检查）
  │   └─ Sandbox Runner（副作用隔离执行）
  │
  ├─ Context Manager                ← 受信，自动压缩（V0.5+）
  │   └─ 监控 token 使用，接近阈值时触发滚动摘要
  │
  └─ Sandbox Pool
      ├─ Browser Instance A（Playwright，独立会话）
      └─ Browser Instance B（Playwright，独立会话）
```

---

## 9. 里程碑规划

| 阶段 | 周期 | 目标 | 交付物 |
|------|------|------|--------|
| **MVP** | 3 周 | Agent 核心跑通 | `StatefulAgent` + `AgentLoopScheduler` + 自动事件写入 + 3 个基础工具 + SQLite Event Store |
| **V0.2** | 2 周 | 工具层完善 | `browser()` + `http()` + `run_code()` + 幂等键自动计算 + Retry 策略 |
| **V0.3** | 2 周 | 可观测性 | 事件流 API + Run 详情 UI + 工具调用 trace 可视化 |
| **V0.4** | 2 周 | Guardrails + 确认流程 | 前置检查框架 + 挂起/恢复机制 + 操作员确认 UI |
| **V0.5** | 2 周 | 长流程稳定性 | Context Manager 自动压缩 + 滚动摘要 + 断点续传 |
| **V1.0** | 3 周 | 生产就绪 | 分层记忆 + 分布式 Worker + 权限 + 监控报警 |

> ✅ **MVP 验收标准**：`AgentLoopScheduler` 驱动一个 `StatefulAgent`，完成自然语言任务，全程事件由系统自动写入（不依赖 Agent 主动触发），幂等键由 Tool Layer 自动计算，工具重试无副作用。

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| LLM 决策漂移 | 高 | Tool Layer Guardrails 作为最终防线；System Prompt 引导正确决策；每步事件追踪，漂移可被检测 |
| 幂等键碰撞 | 中 | Tool Layer 自动计算，规则固定；碰撞时拒绝而非覆盖；唯一约束兜底 |
| 上下文溢出（MVP） | 中 | MVP 阶段控制任务粒度 ≤ 30 轮；V0.5 由 Context Manager 自动处理 |
| Worker 崩溃 | 中 | Agent 状态持久化在 Event Store；新 Worker 恢复目标 < 30 秒 |
| 工具副作用泄漏 | 高 | 工具契约强制声明 `side_effects`；沙盒隔离；Run 结束强制清理 |
| LLM 输出解析失败 | 中 | Structured output（JSON Schema 约束）；失败自动重试一次后熔断 |
| 并发写入事件冲突 | 中 | DB 级唯一约束 `(run_id, seq)`；写入冲突则读取最新 seq 重试 |
| LLM 调用成本规模化 | 中 | 控制每 Run 工具调用轮次；V0.5 引入上下文压缩减少 token 消耗；V1.0 评估缓存策略 |

---

## 11. 评审检查点

- [ ] 所有实际副作用是否发生在 Tool Layer，Agent 是否不直接操作 IO？
- [ ] 幂等键是否由 Tool Layer 根据 `idempotency_key_fields` 自动计算，Agent 是否不感知？
- [ ] 危险操作工具是否设置 `requires_confirmation: true`，触发的是挂起/恢复机制而非 Agent 的工具调用？
- [ ] 每个 think/act/observe 的事件写入是否由系统自动完成，不依赖 Agent 主动触发？
- [ ] Event Store 是否为 Append-Only，是否有物理约束防止 UPDATE/DELETE？
- [ ] Guardrails 是否为最后一道不可绕过的防线，不依赖 System Prompt 是否提醒了 Agent？
- [ ] Agent Loop Scheduler 是否独立于 Agent Kernel，能够在确认等待时挂起/恢复循环？
- [ ] Worker 崩溃后，Agent 状态能否从 Event Store 恢复，恢复时间是否 < 30 秒？
- [ ] 工具注册表是否支持运行时动态加载，新增工具不需要修改 Agent 核心？
- [ ] MCP 工具和 SKILL 是否均通过 `mcp_call()` 入口，工具契约格式是否一致？
- [ ] MVP 阶段上下文上限（≤ 30 轮）是否在产品层有明确的任务拆分策略？

---

*Harness v2.1 · 基于架构评审意见更新*
*核心修正：明确受信边界 / 事件写入改为系统强制 / 幂等键自动计算 / 确认流程改为挂起恢复机制*

*后续演进：分层记忆（V1.0）→ 向量化检索（V1.0+）→ 多 Agent 协作（V2.0）*
