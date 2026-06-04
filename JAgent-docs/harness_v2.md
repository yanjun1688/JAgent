# Harness — Agent-First 任务执行引擎架构方案 v2.2

> 内部设计文档 · 2025
> v2.1 基于架构评审意见更新：明确受信边界，修正事件写入模型，重新设计确认流程
> v2.2 基于 L1 工程落地修正：tool_call_id 链路追踪、confirmation_id 确认关联、幂等缓存策略、确认重入逻辑、事件写入时机

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

**沙盒执行层说明**：Sandbox 是受信的统���工具执行入口，提供两条路径：
- `Sandbox.invoke(fn, input, timeout_ms)`：进程内执行（`http_request`、`file_op` 等），由 Sandbox 统一管理超时和异常
- `Sandbox.run(command, timeout_ms, cwd)`：子进程隔离执行（`run_code`、`browser` 等），进程级沙盒

Executor 的步骤 7 始终经由 Sandbox 执行，不直接调用 `tool_fn`。两条路径共享同一 Sandbox 抽象，未来增强隔离（如限制 fd、内存配额）在 Sandbox 层统一施加。

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

**ToolRegistry 说明**：`ToolRegistry`（`harness/tools/registry.py`）是工具的中央注册表，支持动态注册/查询/移除。Scheduler 通过 `registry.list_tool_defs()` 和 `registry.list_tool_fns()` 获取工具列表，`build_llm_schemas()` 自动生成 LLM 兼容的函数定义。新增工具只需注册到 Registry，无需修改 Scheduler 或 Kernel。

**SKILL 说明**：SKILL 是预定义的多步技能包，对外表现为单个工具（有 `name`、`input_schema`、`output_schema`），内部可以包含多步操作。Agent 调用 SKILL 和调用普通工具的方式完全相同，无需区分。SKILL 通过 `Skill` 类（`harness/tools/skill.py`）定义，支持步骤编排和外部工具依赖注入。

### 5.2 幂等性保证

**幂等键由 Tool Layer 自动计算，Agent 不感知：**

```
幂等键 = hash(tool_name + canonicalize(input[idempotency_key_fields]))
```

- Agent 只提供工具调用的输入参数，Tool Layer 自动提取 `idempotency_key_fields` 中声明的字段，计算幂等键
- 同一个 Run 内，相同幂等键 + 已有 `ToolCompleted` 事件 → 第二次调用直接返回第一次的结果，不重复执行
- Agent 甚至不需要知道幂等键的存在

**缓存命中规则（关键约束）：**

| 已有事件 | 行为 | 理由 |
|----------|------|------|
| `ToolCompleted`（同幂等键） | **跳过执行**，返回缓存结果 | 副作用已完成，无需重复 |
| `ToolFailed`（同幂等键） | **允许重试**，取决于 `retry_policy` | 失败可能因瞬时错误，有重试价值 |
| `ToolTimeout`（同幂等键） | **允许重试**，取决于 `retry_policy` | 超时可能因资源竞争，有重试价值 |
| 无同幂等键事件 | **继续执行流程** | 首次调用 |

**只读工具与幂等缓存：**

只读查询天然可重放，但不同参数应返回不同结果。因此：
- 只读工具也**必须声明 `idempotency_key_fields`**，将区分不同调用的关键字段纳入（如 `url`、`query`）
- 如果工具确实无参数（或所有参数都相同），`idempotency_key_fields` 可为空数组，此时所有调用共享同一个幂等键
- 如果工具调用**不参与幂等缓存**（如每次都需要实时拉取），传递 `idempotency_key=None`，Event Store 不对其建立唯一约束

**tool_call_id（调用链追踪）：**

每次工具调用由 Tool Layer 生成一个全局唯一的 `tool_call_id`（UUID 或 ULID），该 ID 贯穿整个工具调用生命周期：

```
ToolCalled.tool_call_id
  └─ ToolCompleted.tool_call_id   ← 同一 ID，形成 trace 链路
  └─ ToolFailed.tool_call_id
  └─ ToolTimeout.tool_call_id
  └─ GuardrailTriggered.tool_call_id
```

Agent 不感知 `tool_call_id` 的存在——它由 Tool Layer 在接收 `tool_call` 时自动生成，注入到所有相关事件中。

**Tool Layer 执行流程（修订版）：**

```
接收 tool_call (tool_name, input)
  │
  ├─ 0. 生成 tool_call_id（UUID/ULID）
  ├─ 2. 自动计算幂等键（若 input 含 idempotency_key 且不为 None）
  ├─ 1+4. Schema + Guardrails 前置检查（GuardrailRunner 合并执行）
  │      SchemaGuardrail 内置在 GuardrailRunner 首位，守卫无效输入
  │      ├─ 通过 → 继续
  │      └─ 失败 → 写入 GuardrailTriggered 事件，拒绝执行
  ├─ 3. 查询 Event Store：此幂等键是否已有 ToolCompleted 事件？
  │      ├─ 是 → 直接返回缓存结果（不写入 ToolCalled，无副作用）
  │      └─ 否 → 继续
  ├─ 5. requires_confirmation 检查
  │      ├─ false → 继续
  │      ├─ 已有 ConfirmationReceived(confirmed=true) 对应本幂等键 → 跳过，继续
  │      └─ true 且无已确认记录 → 写入 ConfirmationRequested，触发挂起流程（见 5.3）
  ├─ 6. 写入 ToolCalled 事件 ← 此时才写入，确认即将实际执行
  └─ 7. 经 Sandbox 执行工具，写入 ToolCompleted / ToolFailed / ToolTimeout 事件
```

**ToolCalled 写入时机说明：**

`ToolCalled` 事件只在**确认即将实际执行**时才写入（步骤 6），而非在工具调用发起时写入。这确保了：
- 幂等键命中时（步骤 3 缓存返回），不会产生孤立的 `ToolCalled` 事件
- Guardrails 拦截时（步骤 1+4），不会产生后续无对应完成事件的 `ToolCalled`
- 事件流中每条 `ToolCalled` 必定有对应的 `ToolCompleted` / `ToolFailed` / `ToolTimeout`

**步骤顺序说明（v2.2 修正）：**

- Guardrails（1+4）在缓存查重（3）之前执行：无效输入在最早可行点被拦截（fail-fast），避免先做 I/O 再发现参数非法；动态 Guardrail（RateLimitGuardrail）在 V0.4 实现时也在此步骤生效
- 幂等键计算（2）在最前（schema 校验之后）：后续步骤均依赖幂等键

**重放安全分级：**

| 工具类型 | 幂等处理 |
|----------|----------|
| 只读查询 | 天然幂等，`idempotency_key_fields` 应包含区分不同请求的关键字段 |
| 本地写操作 | 必须声明 `idempotency_key_fields`，Tool Layer 查重 |
| 外部系统写入 | 需外部系统支持幂等 API；不支持则标记 `requires_confirmation: true` |

### 5.3 人工确认流程（挂起/恢复机制）

人工确认**不是 Agent 调用的工具**，而是系统的挂起/恢复机制：

```
① Agent 调用危险工具（requires_confirmation: true）
          │
          ▼
② Tool Layer 拦截
   生成 confirmation_id（UUID/ULID）
    写入 ConfirmationRequested 事件（含 confirmation_id + tool_call_id + idempotency_key + 原 tool_call 的 input）
          │
          ▼
③ Agent Loop Scheduler 检测到 ConfirmationRequested
   写入 RunPaused 事件（reason: "waiting_confirmation"）
   Agent 循环挂起（非终止，保留完整上下文）
   通过 WebSocket / Webhook 向操作员发送确认请求
          │
          ▼
④ 操作员通过外部接口查看详情并决策
   系统写入 ConfirmationReceived 事件（confirmation_id, confirmed: true/false, operator_id）
   写入 RunResumed 事件（resume_from_seq: 最近事件的 seq）
          │
          ├─ confirmed: false → Tool Layer 写入 ToolFailed (error: "operator_denied")
          │                      Agent 循环恢复，感知拒绝结果
          │
          └─ confirmed: true  → Agent 循环恢复
                                Tool Layer 重新执行原工具调用
                                此时步骤 5 检测到 ConfirmationReceived(confirmed=true)
                                跳过 requires_confirmation 检查，直接进入执行
                                写入 ToolCalled → ToolCompleted 事件
```

**重入避免机制：**

确认通过后 Tool Layer 重新执行原工具调用，会再次走过完整的执行流程（5.2 节）。此时：
- 步骤 3（幂等键查重）：如果 `ConfirmationRequested` 和 `ConfirmationReceived` 事件带有相同的 `idempotency_key`，则不会重复确认（因为步骤 5 会先检查）
- 步骤 5（requires_confirmation 检查）：Tool Layer 检测到该幂等键已有 `ConfirmationReceived(confirmed=true)` 事件 → 跳过 `requires_confirmation` 检查 → 继续步骤 6 执行

**confirmation_id 关联机制：**

- `ConfirmationRequestedPayload.confirmation_id` 由 Tool Layer 在拦截时生成
- `ConfirmationReceivedPayload.confirmation_id` 由外部接口写入时引用相同的值
- fold_events() 通过 `confirmation_id` 匹配，从 `pending_confirmations` 中移除已解决的确认项

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
| `orchestrate` | 多步动态编排：Agent 提交步骤序列，Harness 逐步骤安全执行（见 §5.6） |

#### 控制工具（Control Tools）

| 工具 | 说明 | 受信 |
|------|------|------|
| `fail_with_reason` | 主动终止任务并记录原因 | 否（Agent 调用） |
| `get_run_events` | 读取当前 Run 的事件流 | 否（Agent 调用） |
| `get_run_state` | 折叠事件流得到当前状态快照 | 否（Agent 调用） |

> 注意：事件写入、上下文压缩、确认触发均由受信组件自动完成，不在工具列表中。

### 5.6 Dynamic Orchestration（动态编排）

#### 5.6.1 核心概念

动态编排允许 Agent 在**单次 tool_call** 中提交多步工具序列，由 Harness（`Orchestrator` 受信组件）逐步骤安全执行。这是对"Agent 提议，Harness 执行"模式在序列层面的扩展：

```
传统模式（单步）:
  Agent thinks → 调 1 个 tool → observe → Agent thinks → 调 1 个 tool → ...

动态编排（多步）:
  Agent thinks → 调 orchestrate([stepA, stepB, stepC])
    → Harness 逐步骤执行:
        stepA → Tool Layer(Guardrails+幂等+确认) → StepCompleted
        stepB → Tool Layer(Guardrails+幂等+确认) → StepCompleted
        stepC → Tool Layer(Guardrails+幂等+确认) → StepCompleted
    → 聚合结果返回 Agent
  Agent observes → thinks next
```

**核心原则**：
- **Agent 决策，Harness 执行**：Agent 决定"做什么"（步骤序列），Harness 决定"怎么做"（何时执行、是否安全、如何容错）
- **原子可见性**：编排过程 Agent 不可见中间结果，只看到最终聚合结果
- **失败即终止**：任一步失败停止编排，Agent 下轮 think 中自行修复

#### 5.6.2 Orchestrator 组件

`Orchestrator`（`harness/core/orchestrator.py`）是编排的执行引擎，职责：

```
Orchestrator.execute(input):
  ├─ 1. 校验计划（PlanGuardrail）
  │   ├─ 步数 ≤ max_steps（默认 10）
  │   ├─ 每步 tool_name 存在于 ToolRegistry
  │   └─ 每步 input 通过该工具的 SchemaGuardrail
  │
  ├─ 2. 写入 OrchestrationStarted 事件
  │
  ├─ 3. 逐步骤执行
  │   for i, step in enumerate(steps):
  │     result = ToolExecutor.execute(
  │       run_id, step.tool, step.input,
  │       tool_def, tool_fn
  │     )
  │     match result.status:
  │       COMPLETED | IDEMPOTENCY_HIT → 写入 StepCompleted
  │       CONFIRMATION_NEEDED → 挂起等待 → 恢复后重试
  │       FAILED | TIMEOUT | GUARDRAIL_BLOCKED → 写入 StepFailed → 终止
  │
  ├─ 4. 写入 OrchestrationCompleted / OrchestrationFailed
  │
  └─ 5. 返回聚合结果
```

#### 5.6.3 `orchestrate` 工具契约

```typescript
ToolDefinition {
  name: "orchestrate",
  description: "Execute multiple tool calls in sequence under harness control",
  input_schema: {
    type: "object",
    properties: {
      intent: { type: "string", description: "编排目标" },
      steps: {
        type: "array",
        items: {
          type: "object",
          properties: {
            tool: { type: "string" },
            input: { type: "object" },
            description: { type: "string" }
          },
          required: ["tool", "input"]
        }
      }
    },
    required: ["intent", "steps"]
  },
  idempotency_key_fields: ["intent", "steps"],
  side_effects: [EXTERNAL],
  timeout_ms: 600000,          // 10 min（编排可能包含多个 I/O 操作）
  retry_policy: { max_retries: 0 },  // 编排整体不重试，重试在步骤级别
  requires_confirmation: false
}
```

`orchestrate` 作为普通工具注册到 `ToolRegistry`，Scheduler **不需要**感知编排的存在。Scheduler 的 think→act→observe 循环不变，`orchestrate` 只是 Agent 可选调用的众多工具之一。

#### 5.6.4 安全的层级结构

编排保留了 Harness 的每一层安全控制：

| 层级 | 保障 | 机制 |
|------|------|------|
| 计划级 | 步数限制 | `PlanGuardrail`：max_steps（默认 10） |
| 计划级 | 工具可用性 | `PlanGuardrail`：确认每步 tool 已注册 |
| 计划级 | 输入合法性 | `PlanGuardrail`：每步输入预校验 Schema |
| 步骤级 | 完整 Tool Layer | 每步独立经过 SchemaGuardrail → 幂等键 → Guardrails → Sandbox 执行 |
| 步骤级 | 人工确认 | `requires_confirmation=true` 的工具触发完整挂起/恢复流程 |
| 循环级 | Scheduler 熔断 | 步骤失败计入 `consecutive_failures`，触发熔断时整个 Run 终止 |
| Run 级 | Pause/Cancel | 暂停/取消从 Scheduler 级联到 Orchestrator |

#### 5.6.5 与已有机制的互补

| 机制 | 关系 |
|------|------|
| `make_plan()` | `make_plan` 产生文本计划供 Agent 参考；`orchestrate` 直接执行结构化步骤。Agent 可先 `make_plan` 规划再 `orchestrate` 执行 |
| SKILL | SKILL 是开发者预编码的多步技能包；`orchestrate` 是 Agent 动态组装的多步序列。两者都通过 Tool Layer 执行 |
| 单个工具调用 | `orchestrate` 不会绕过或替代单工具调用。Agent 根据任务复杂度自行选择：1 步用普通调用，3+ 步用 `orchestrate` |

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
| `RunStarted` | Scheduler | 任务启动 | `intent, context_snapshot` |
| `AgentThought` | Scheduler | LLM 输出 thought 后自动写入 | `thought, tool_choice, token_count` |
| `ToolCalled` | Tool Layer | 通过全部前置检查、确认即将实际执行时写入 | `tool_call_id, tool_name, input, idempotency_key` |
| `ToolCompleted` | Tool Layer | 工具执行成功时自动写入 | `tool_call_id, tool_name, output, duration_ms` |
| `ToolFailed` | Tool Layer | 工具执行失败时自动写入 | `tool_call_id, tool_name, error, retryable` |
| `ToolTimeout` | Tool Layer | 工具执行超时时自动写入 | `tool_call_id, tool_name, timeout_ms` |
| `GuardrailTriggered` | Tool Layer | 前置检查未通过时自动写入 | `tool_call_id, tool_name, guardrail_id, reason` |
| `ConfirmationRequested` | Tool Layer | 危险操作被拦截时自动写入 | `confirmation_id, tool_call_id, tool_name, input, idempotency_key, risk_level` |
| `ConfirmationReceived` | 外部接口 | 操作员提交决策 | `confirmation_id, confirmed, operator_id` |
| `ContextCompressed` | Context Manager | 自动压缩触发时写入（V0.5+） | `original_tokens, compressed_tokens, summary_ref`（V0.5+ 改为 `EpisodeSummary \| str` 结构化输出） |
| `ContextCheckpointed` | Context Manager / 任意受信组件 | 定期快照写入（V0.5+）；L2 已定义完整 Payload 和 fold 处理 | `checkpoint_seq, snapshot_ref, token_count` |
| `RunPaused` | Scheduler | 暂停执行（如等待确认） | `reason` |
| `RunResumed` | Scheduler | 恢复执行（确认完成后） | `resume_from_seq` |
| `RunCompleted` | Scheduler | LLM 输出停止信号后自动写入 | `result_summary` |
| `RunFailed` | Scheduler / Tool Layer | 熔断或 `fail_with_reason` | `final_error, event_count` |
| `OrchestrationStarted` | Orchestrator | 编排开始时写入 | `plan_id, intent, steps_summary` |
| `StepCompleted` | Orchestrator | 编排中某一步执行成功 | `plan_id, step_index, tool_call_id, output` |
| `StepFailed` | Orchestrator | 编排中某一步执行失败 | `plan_id, step_index, tool_call_id, error` |
| `OrchestrationCompleted` | Orchestrator | 全部步骤完成 | `plan_id, completed_steps, summary` |
| `FeedbackInjected` | RunMonitor | 监控组件检测到异常时自动注入反馈（V0.6） | `feedback_text, priority` |
| `OrchestrationFailed` | Orchestrator | 编排因步骤失败终止 | `plan_id, completed_steps, final_error` |

> **tool_call_id 链路**：每次工具调用由 Tool Layer 生成全局唯一的 `tool_call_id`，`ToolCalled`、`ToolCompleted`、`ToolFailed`、`ToolTimeout`、`GuardrailTriggered` 共享同一 ID，通过该 ID 可重建完整的工具调用 trace 链表。

> **confirmation_id 关联**：`ConfirmationRequested` 和 `ConfirmationReceived` 通过 `confirmation_id` 关联。该 ID 由 Tool Layer 在拦截时生成，外部接口提交决策时引用。

### 6.3 Schema

**MVP：SQLite**

```sql
CREATE TABLE events (
  run_id          TEXT    NOT NULL,
  seq             INTEGER NOT NULL,
  event_type      TEXT    NOT NULL,
  payload         JSON    NOT NULL,   -- TEXT column, stored as JSON string
  idempotency_key TEXT,
  created_at      REAL    NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE UNIQUE INDEX idx_idem
  ON events(run_id, event_type, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

**Python 侧 Payload 校验（Pydantic v2）：**

事件存储层使用原始 `dict` 存储 payload（便于 JSON 往返），写入时通过 `PAYLOAD_MODEL_MAP` 进行 Pydantic 校验：

```python
# Event 模型
class Event(BaseModel):
    run_id: str
    seq: int
    event_type: EventType
    payload: dict[str, Any]        # 原始 dict，JSON 往返
    idempotency_key: str | None = None
    created_at: float

# Payload 注册表
PAYLOAD_MODEL_MAP: dict[EventType, type[BaseModel]] = {
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.AGENT_THOUGHT: AgentThoughtPayload,
    # ... 全部 15 种事件类型
}
```

**seq 生成（MVP 限制）：**

```python
next_seq = await get_latest_seq(run_id) + 1  # MAX(seq) + 1
```

> ⚠️ MVP 阶段 seq 计算非原子操作。并发写入同一 `run_id` 时可能冲突，DB 层 `PRIMARY KEY (run_id, seq)` 作为最后一道防线。生产环境应使用 `RETURNING` 子句或自增序列表。

**生产：PostgreSQL**

- `JSONB` 类型存储 payload，支持 GIN 索引加速 payload 字段查询
- 分区表按 `run_id` hash 分区，支持水平扩展
- 物化视图 `run_state` 异步投影，加速状态查询
- WAL 复制保证高可用

### 6.4 事件流折叠（fold_events）

`fold_events()` 是纯函数，接收按 seq 排序的事件列表，输出 `RunState` 快照：

```python
def fold_events(events: list[Event]) -> RunState:
    """Pure function: fold a sorted event stream into a RunState snapshot."""
```

**RunState 数据结构：**

```python
@dataclass
class RunState:
    run_id: str
    status: RunStatus           # RUNNING | PAUSED | COMPLETED | FAILED
    seq: int                    # 当前最高 seq
    intent: str                 # 任务意图
    context_snapshot: dict      # 启动时的上下文快照
    thought_history: list[AgentThoughtPayload]  # 推理历史
    latest_thought: AgentThoughtPayload | None
    tool_calls: list[ToolCalledPayload]         # tool_call 追踪链
    tool_results: list[ToolResult]              # 工具结果（completed/failed/timeout/guardrail_blocked）
    last_error: str | None
    summary: EpisodeSummary | str | None    # V0.5+ 结构化摘要，无 LLM 时降级纯文本
    keep_recent_count: int = 0              # 紧急压缩时保留最近 3 轮详情
    pause_reason: str | None
    pending_confirmations: list[ConfirmationRequestedPayload]  # 未解决的确认请求
    last_checkpoint_seq: int | None   # 最近 ContextCheckpointed 的 seq（V0.5+）
    feedbacks: list[FeedbackInjectedPayload]  # V0.6 监控反馈列表，折叠 `FeedbackInjected` 事件
```

**fold 逻辑要点：**

- `ConfirmationRequested` 将确认项加入 `pending_confirmations`
- `ConfirmationReceived` 通过 `confirmation_id` 匹配并移除对应确认项（`confirmed=false` 同样移除，表示确认流程已终结）
- `ContextCheckpointed` 更新 `last_checkpoint_seq`，供断点续传定位恢复起点
- `RunPaused` / `RunResumed` 控制 `status` 切换
- `ToolCalled` 追加到 `tool_calls` 列表，`ToolCompleted`/`ToolFailed`/`ToolTimeout`/`GuardrailTriggered` 追加到 `tool_results`
- 折叠是**无副作用的纯函数**：相同事件流总是产生相同的 `RunState`

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
1. 读取最近的 ContextCheckpointed 事件（V0.5+），加载上下文快照
2. 从快照对应的 seq 之后读取增量事件
3. 将增量事件折叠进上下文（Agent "回忆"已发生的事情）
4. Scheduler 恢复 think → act → observe 循环
```

未记录 checkpoint 时：从 `RunStarted` 开始折叠全部事件。

> **ContextCheckpointed vs ContextCompressed**：
> - `ContextCheckpointed`（V0.5+）：断点续传时保存的上下文快照事件，记录 `snapshot_ref` 和 `checkpoint_seq`。Scheduler 定期写入，用于快速恢复。
> - `ContextCompressed`（V0.5+）：上下文自动压缩事件，记录 `original_tokens` 和 `compressed_tokens`。Context Manager 在接近 token 阈值时触发。
> - MVP 阶段这两个事件均不实现。上下文管理直接使用 LLM 原生窗口，任务粒度控制在 ≤ 30 轮。

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
  │   └─ 通过 Scheduler 接收 tool_defs（`list[ToolDefinition]` 参数注入）
  │
  ├─ Tool Registry                  ← 受信，工具生命周期管理
  │   ├─ 集中注册/查询/移除工具定义和实现
  │   ├─ build_llm_schemas() 自动生成 LLM 兼容的 function calling 定义
  │   └─ Scheduler 通过 registry.list_tool_defs() / list_tool_fns() 获取
  │
  ├─ Tool Executor                  ← 受信，强制执行契约
  │   ├─ Idempotency Checker（自动计算 + 查重）
  │   ├─ Guardrail Runner（前置检查）
  │   └─ 委托 Sandbox 执行（见下）
  │
  ├─ Sandbox                        ← 受信，统一执行入口
  │   ├─ invoke(fn, input, timeout_ms)  ← 进程内工具（http_request, file_op）
  │   └─ run(command, timeout_ms, cwd)  ← 子进程工具（run_code, browser）
  │
  ├─ Context Manager                ← 受信，自动压缩（V0.5+）
  │   └─ 监控 token 使用，接近阈值时触发滚动摘要
  │
  └─ Sandbox Pool
      ├─ Browser Instance A（Playwright，独立会话）
      └─ Browser Instance B（Playwright，独立会话）
```

---

## 8.3 接口层设计（V0.3+）

### 8.3.0 依赖注入模式

HarnessAPI 通过 FastAPI `Depends()` 注入所有端点（实现见 `harness/api/deps.py`）：

```python
# harness/api/deps.py
class HarnessAPI:
    """Wraps core dependencies injected into every endpoint."""
    def __init__(self, store: EventStore, executor=None):
        self.store = store
        self.executor = executor
        self._schedulers: dict[str, AgentLoopScheduler] = {}
        self._ws_clients: dict[str, list[WebSocket]] = {}

_hapi: HarnessAPI | None = None

def get_hapi() -> HarnessAPI:
    """FastAPI dependency — injects HarnessAPI into endpoints."""
    if _hapi is None:
        raise RuntimeError("HarnessAPI not initialized.")
    return _hapi

# Endpoints receive via Depends():
@app.get("/api/v1/runs")
async def list_runs(api: HarnessAPI = Depends(get_hapi)):
    ...

# 测试通过 dependency_overrides 注入 mock 实例：
app.dependency_overrides[get_hapi] = lambda: test_api
```

- HTTP 和 WebSocket 端点统一使用 `Depends(get_hapi)`
- 生产环境通过 `configure_hapi(api)` 设置全局实例（`harness/api/deps.py:43`）
- 测试无需调用 `configure_hapi()`，直接覆盖依赖即可，避免全局状态污染
- 生命周期由外部管理（`store.initialize()` / `store.close()`），FastAPI lifespan 为空
- WebSocket 端点和 REST 端点共享同一 `HarnessAPI` 实例，通过 `broadcast_event()` 推送新事件（`harness/api/deps.py:58`）

### 8.3.1 REST API

| 端点 | 方法 | 用途 | 响应 |
|------|------|------|------|
| `/api/v1/runs` | GET | 列举所有 Run（分页） | `{ runs: RunSummary[], total: int }` |
| `/api/v1/runs` | POST | 创建新 Run | `{ run_id: str }` |
| `/api/v1/runs/{run_id}` | GET | 获取 Run 状态快照 | `RunDetailResponse`（含 status, intent, seq, event_count, last_error, summary, pause_reason, pending_confirmations） |
| `/api/v1/runs/{run_id}` | DELETE | 归档/删除 Run | `{ success: bool }` |
| `/api/v1/runs/{run_id}/events` | GET | 获取事件流（分页） | `{ events: Event[], total: int }` |
| `/api/v1/runs/{run_id}/pause` | POST | 暂停 Run | `{ success: bool }` |
| `/api/v1/runs/{run_id}/resume` | POST | 恢复 Run | `{ success: bool }` |
| `/api/v1/runs/{run_id}/confirm` | POST | 提交确认决策 | `{ success: bool }` |

### 8.3.2 WebSocket

| 端点 | 用途 |
|------|------|
| `WS /api/v1/runs/{run_id}/events` | 实时推送 Run 的新事件 |

- 连接时立即发送该 Run 的所有历史事件（按 seq 排序）
- 新事件产生时实时推送给所有连接的客户端
- 每个消息包含单个 `Event` JSON 对象

### 8.3.3 前端架构

```
frontend/
├── src/
│   ├── api/           # fetch 客户端 + WebSocket 连接（client.ts）
│   │   ├── client.ts  # API 调用封装 + connectEventStream()
│   │   └── schema.ts  # 从 OpenAPI schema 自动生成的 TypeScript 类型
│   ├── components/    # 可复用 UI 组件
│   │   └── ConfirmDialog.tsx
│   ├── pages/         # 页面组件
│   │   ├── RunList.tsx     # Run 列表（带创建输入 + 5s 自动刷新）
│   │   └── RunDetail.tsx   # 详情页：事件时间线 + pause/resume/delete + 确认操作
│   └── App.tsx
├── public/
│   └── openapi.json   # `python scripts/generate_openapi.py` 导出的 OpenAPI Schema
└── package.json

类型同步方式（scripts/generate_openapi.py 在项目根目录）：
1. 后端 Pydantic Model 变更后运行 `npm run generate-api`（调用 `py .../generate_openapi.py`）
2. `python scripts/generate_openapi.py` 离线从 `FastAPI app.openapi()` 导出 `openapi.json` → `frontend/public/openapi.json`
3. 同一脚本同时从 `components.schemas` 提取 TypeScript 接口 → `frontend/src/api/schema.ts`
4. `client.ts` 从 `schema.ts` 导入类型（import type { RunSummary, RunDetailResponse, EventResponse }），不手写重复定义
5. WebSocket 逻辑内联在 `client.ts` (connectEventStream) + `RunDetail.tsx`（useRef 管理连接、lastSeqRef 去重），无独立 hooks 目录
```

---

## 9. 里程碑规划

| 阶段 | 周期 | 目标 | 交付物 |
|------|------|------|--------|
| **MVP** | 3 周 | Agent 核心跑通 | `StatefulAgent` + `AgentLoopScheduler` + 自动事件写入 + 3 个基础工具 + SQLite Event Store | ✅ |
| **V0.2** | 2 周 | 工具层完善 | `ToolRegistry` + `browser()` + `http_request()` + `file_op()` + `mcp_call()` + `SKILL` + 幂等键全面声明 | ✅ |
| **V0.3** | 2 周 | 可观测性 | FastAPI 后端（REST + WebSocket）+ React 前端（Run 列表/详情/确认 UI）| ✅ |
| **V0.4** | 2 周 | Guardrails + 确认流程 | ScopeGuardrail + RateLimitGuardrail + DestructiveOpGuardrail + DependencyGuardrail + 操作员确认 UI + Guardrail 测试 32 项 | ✅ |
| **V0.4+** | 1 周 | Dynamic Orchestration | `Orchestrator` + `orchestrate` 工具 + PlanGuardrail + 5 种编排事件 + 16 项集成测试 | ✅ |
| **V0.5** | 2 周 | 长流程稳定性 | Context Manager 自动压缩 + 滚动摘要 + 断点续传 + 20 项集成测试 | ✅ |
| **V0.5+** | 1 周 | 记忆压缩优化 | EpisodeSummary 结构化摘要 + 紧急压缩（压缩旧 50% 保留近 3 轮）+ 9 项新增测试 | ✅ |
| **V0.6** | 2 周 | 监控与反馈 | RunMonitor（on_append 实时监听）+ FeedbackInjected 事件 + Scheduler System Prompt 注入 + 22 项测试 | ✅ |
| **V1.0** | 3 周 | 生产就绪 | 分层记忆 + 分布式 Worker + 权限 + 业务适配 | 🔜 |

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
- [x] Agent Loop Scheduler 是否独立于 Agent Kernel，能够在确认等待时挂起/恢复循环？
- [x] Worker 崩溃后，Agent 状态能否从 Event Store 恢复，恢复时间是否 < 30 秒？
- [x] V0.5 ContextManager 是否对 Agent 透明？Agent 是否可以感知压缩事件的事件写入？
- [x] V0.5 `state.summary` 是否被 AgentKernel 正确消费，实现上下文压缩效果？
- [ ] 工具注册表是否支持运行时动态加载，新增工具不需要修改 Agent 核心？
- [ ] MCP 工具和 SKILL 是否均通过 `mcp_call()` 入口，工具契约格式是否一致？
- [ ] MVP 阶段上下文上限（≤ 30 轮）是否在产品层有明确的任务拆分策略？
- [x]（V0.4+）`orchestrate` 工具的每步是否独立经过 Tool Layer 的 Guardrails/幂等键/确认检查？

- [x]（V0.4+）编排失败时是否立即终止并写入 `OrchestrationFailed`，不继续执行后续步骤？

- [x]（V0.4+）编排事件（OrchestrationStarted / StepCompleted / StepFailed / OrchestrationCompleted）是否完整记录在 Event Store？

---

*Harness v2.2 · 基于 L1 工程落地修正*
*核心修正：明确受信边界 / 事件写入改为系统强制 / 幂等键自动计算 / 确认流程改为挂起恢复机制 / tool_call_id 链路追踪 / confirmation_id 确认关联*
*v2.2 新增：幂等缓存策略细化 / 确认重入避免 / ToolCalled 写入时机 / ContextCheckpointed 与 ContextCompressed 区分 / Sandbox 统一执行入口（invoke + run）/ ContextCheckpointed 第 15 种事件类型 / append_event PK 冲突重试*

*后续演进：分层记忆（V1.0）→ 向量化检索（V1.0+）→ 多 Agent 协作（V2.0）*
