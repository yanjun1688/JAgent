# Harness v2.1 — 多轮对话开发实施计划

> **版本**: v1.0
> **基于**: PRD_v1.0.md / agent_execution_audit_20260703(1).md / ARCHITECTURE_v2.1.md
> **当前基线**: 341 项测试全通过
> **实施原则**: 分层推进，后端→前端，每层交付验收后方可进入下一层

---

## 目录

- [1. 概述](#1-概述)
- [2. Phase 1 — 最小可行多轮对话（后端）](#2-phase-1--最小可行多轮对话后端)
  - [2.1 架构变更](#21-架构变更)
  - [2.2 数据模型](#22-数据模型)
  - [2.3 事件扩展](#23-事件扩展)
  - [2.4 API 端点](#24-api-端点)
  - [2.5 对话上下文注入](#25-对话上下文注入)
  - [2.6 任务拆解](#26-任务拆解)
  - [2.7 前后端联调契约](#27-前后端联调契约)
  - [2.8 验收检查清单](#28-验收检查清单)
- [3. Phase 2 — 执行控制 + Fallback 优化](#3-phase-2--执行控制--fallback-优化)
  - [3.1 RunMonitor 硬控制](#31-runmonitor-硬控制)
  - [3.2 Fallback Kernel tools API 改造](#32-fallback-kernel-tools-api-改造)
  - [3.3 任务拆解](#33-任务拆解)
- [4. Phase 3 — 上下文管理优化](#4-phase-3--上下文管理优化)
  - [4.1 动态上下文窗口](#41-动态上下文窗口)
  - [4.2 工具结果智能截断](#42-工具结果智能截断)
  - [4.3 任务拆解](#43-任务拆解)
- [5. Phase 4 — 前端对话式 UI 重写](#5-phase-4--前端对话式-ui-重写)
  - [5.1 架构变更](#51-架构变更)
  - [5.2 组件结构](#52-组件结构)
  - [5.3 数据流](#53-数据流)
  - [5.4 任务拆解](#54-任务拆解)
  - [5.5 后端配合变更](#55-后端配合变更)
  - [5.6 前后端联调契约（补充）](#56-前后端联调契约补充)
- [6. 测试策略](#6-测试策略)
- [7. 发布顺序与回退方案](#7-发布顺序与回退方案)

---

## 1. 概述

### 核心问题

当前系统没有多轮对话概念。每次用户输入创建一个全新的、独立的 Run，前后两次输入之间共享零上下文。`Run` 是执行单元，不是对话单元。

### 解决方案概览

```
新增概念层级:

  Conversation (多轮对话线程)
    ├─ user_id (对话所属用户)
    ├─ title (自动生成)
    ├─ created_at / updated_at
    ├─ status: active | archived
    │
    ├─ Message #1 → Run #1 (第 1 轮执行)
    │   ├─ intent: "帮我查天气"
    │   └─ result: "东京今天 25°C"
    │
    ├─ Message #2 → Run #2 (第 2 轮，引用 Run #1 上下文)
    │   ├─ intent: "用中文总结" (系统注入前一轮摘要后实际为 "Previous: 东京25°C\nNew: 用中文总结")
    │   └─ result: "根据之前的查询结果：东京 25°C..."
    │
    └─ Message #N → Run #N
```

### 总体实施路线

| Phase | 内容 | 涉及 | 前置依赖 | 预计 |
|-------|------|------|----------|------|
| **Phase 1** | 后端多轮对话基础设施 | 后端 | 无 | 2 周 |
| **Phase 2** | 执行控制 + Fallback 优化 | 后端 | Phase 1 完成 | 2 周 |
| **Phase 3** | 上下文管理优化 | 后端 | Phase 1 完成 | 1 周 |
| **Phase 4** | 前端对话式 UI | 前端 | Phase 1-3 完成 | 3 周 |

---

## 2. Phase 1 — 最小可行多轮对话（后端）

**核心目标**: 后端引入 Conversation 概念，现有前端 ChatDrawer 仍可正常工作（仅增加对话级 API）。

### 2.1 架构变更

```
变更前:
  POST /api/v1/runs
    → create_run(intent)
    → Run 独立执行，与前后零关联

变更后:
  POST /api/v1/conversations/{id}/messages
    → 1. 查找/创建 Conversation
    → 2. 折叠对话历史（从 Conversation 事件流）
    → 3. 构建带上下文的 intent
    → 4. create_run_with_context(intent, conversation_id)
    → 5. Run 执行，关联 conversation_id
    → 6. 写入 ConversationMessage 事件

  POST /api/v1/runs (向后兼容)
    → 无 conversation_id 时行为不变
    → 有 conversation_id 时注入上下文
```

**关键设计决策**：

1. **Conversation 存储在 Event Store 中**：Conversation 本身也是一个事件流，使用 `conversation_id` 作为查询维度
2. **Conversation 索引表**：新增独立的 `conversations` SQLite 表，用于快速查询对话列表（标题/时间/消息数）
3. **存量兼容**：现有 `POST /api/v1/runs` 不传 `conversation_id` 时行为完全不变；`conversation_id` 字段 Optional

### 2.2 数据模型

#### 新增 `harness/models/conversation.py`

```python
# harness/models/conversation.py
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum

class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"

class Conversation(BaseModel):
    """对话模型 - 后端索引，非事件溯源"""
    conversation_id: str
    user_id: str = "default"
    title: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: float
    updated_at: float
    message_count: int = 0

class ConversationDetail(BaseModel):
    """对话详情 - 包含消息列表"""
    conversation: Conversation
    messages: list["ConversationMessageItem"]

class ConversationMessageItem(BaseModel):
    """对话中的单条消息"""
    seq: int
    run_id: str
    role: str  # "user" | "assistant"
    content: str
    created_at: float
    status: str  # run 的执行状态

class CreateConversationRequest(BaseModel):
    title: Optional[str] = None

class SendMessageRequest(BaseModel):
    message: str

class ConversationListResponse(BaseModel):
    conversations: list[Conversation]
    total: int
```

### 2.3 事件扩展

#### `harness/models/events.py` 新增事件类型

| 事件类型 | 写入方 | 关键 payload 字段 |
|----------|--------|-------------------|
| `ConversationStarted` | API | `conversation_id, title, user_id` |
| `ConversationMessage` | API | `conversation_id, run_id, role, content` |
| `ConversationEnded` | API | `conversation_id, summary` |

```python
# 在 EventType 枚举中新增
CONVERSATION_STARTED = "ConversationStarted"
CONVERSATION_MESSAGE = "ConversationMessage"
CONVERSATION_ENDED = "ConversationEnded"

# Payload 模型
class ConversationStartedPayload(BaseModel):
    conversation_id: str
    title: str
    user_id: str = "default"

class ConversationMessagePayload(BaseModel):
    conversation_id: str
    run_id: str
    role: str  # "user" | "assistant"
    content: str

class ConversationEndedPayload(BaseModel):
    conversation_id: str
    summary: str = ""
```

**注意事项**：
- `ConversationMessage` 的 `run_id` 关联到具体的执行 Run
- `role="assistant"` 的消息在 Run 完成时（`RunCompleted`/`RunFailed`）写入
- `conversation_id` 需要写入 `events` 表的新增列，或通过 payload 中的字段查询

#### Event Store 表结构变更

```sql
-- conversations 索引表（新增）
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);

-- events 表增加 conversation_id 列（可选索引）
ALTER TABLE events ADD COLUMN conversation_id TEXT;
CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id);
```

### 2.4 API 端点

#### 新增端点

| 方法 | 路径 | 请求体 | 响应 | 说明 |
|------|------|--------|------|------|
| `POST` | `/api/v1/conversations` | `{"title": "..."}` | `{"conversation_id", "title", "created_at"}` | 创建新对话 |
| `GET` | `/api/v1/conversations` | — | `{"conversations": [...], "total": N}` | 获取对话列表（按 updated_at 降序） |
| `GET` | `/api/v1/conversations/{id}` | — | `{"conversation": {...}, "messages": [...]}` | 获取对话详情 + 消息列表 |
| `DELETE` | `/api/v1/conversations/{id}` | — | `{"success": true}` | 删除对话（软删除：标记为 archived） |
| `POST` | `/api/v1/conversations/{id}/messages` | `{"message": "..."}` | `{"run_id", "conversation_id", "seq"}` | 发送消息，创建 Run 执行 |
| `PATCH` | `/api/v1/conversations/{id}` | `{"title": "..."}` | `{"success": true}` | 更新对话标题 |

#### 修改已有点

| 方法 | 路径 | 变更 |
|------|------|------|
| `POST` | `/api/v1/runs` | 请求体新增可选字段 `conversation_id: str` |
| `GET` | `/api/v1/runs/{run_id}` | 响应新增字段 `conversation_id: str | None` |

### 2.5 对话上下文注入

#### 核心机制

在 `create_run_with_context()` 中，从 Conversation 事件流折叠出对话摘要，注入到新 Run 的 System Prompt：

```python
async def _build_conversation_context(store: EventStore, conversation_id: str) -> str:
    """从 Conversation 事件流构建上下文摘要"""
    events = await store.get_events(conversation_id)
    messages = []
    for e in events:
        if e.event_type == EventType.CONVERSATION_MESSAGE:
            p = e.payload
            messages.append(f"[{p.role}] {p.content[:500]}")
        elif e.event_type == EventType.CONVERSATION_STARTED:
            pass  # 跳过创建事件

    # 只取最近 N 轮（防止 token 超限）
    recent = messages[-6:]  # 最多最近 3 轮对话（3 user + 3 assistant）
    return "\n".join(recent)
```

#### System Prompt 变更

在 `harness/core/system_prompt.py` 新增 `AgentPhase.CONVERSATION_CONTEXT`：

```python
# system_prompt.py 新增
_CONVERSATION_CONTEXT_PROMPT = """
Previous conversation:
{conversation_summary}

Current request: {intent}
"""
```

注入时机：在 `PlanningExecutorScheduler.run()` / `AgentLoopScheduler.run()` 创建 Run 后，首次 THINK 前注入。

**注入方式**：`conversation_summary` 作为 `intent` 的前缀组合，不新增 `think()` 参数：

```python
# 原 intent = "帮我用中文总结"
# 有对话上下文时：
intent = f"""Previous conversation:
👤 帮我查东京天气
🤖 东京今天 25°C，晴

Current request: 帮我用中文总结"""
```

这样做的好处：**不修改 Scheduler/Kernel 的 think() 接口**，零侵入。Conversation 上下文对执行引擎是完全透明的。

### 2.6 任务拆解

#### 后端任务

| # | 任务 | 文件 | 交付物 | 负责人 |
|---|------|------|--------|--------|
| B1.1 | `Conversation` 数据模型定义 | `harness/models/conversation.py` **新文件** | Pydantic Model：`Conversation`, `ConversationDetail`, `ConversationMessageItem`, 请求/响应模型 | 后端 |
| B1.2 | 事件类型扩展 | `harness/models/events.py` | `EventType.CONVERSATION_STARTED/MESSAGE/ENDED` + 3 个 Payload Model + `PAYLOAD_MODEL_MAP` 注册 | 后端 |
| B1.3 | Event Store 表结构变更 | `harness/storage/event_store.py` | `conversations` 表 + `events.conversation_id` 列 + 索引 + 迁移逻辑 | 后端 |
| B1.4 | Event Store 对话查询方法 | `harness/storage/event_store.py` | `upsert_conversation()` / `list_conversations()` / `get_conversation()` / `delete_conversation()` | 后端 |
| B1.5 | Event Store 跨 Run 事件查询 | `harness/storage/event_store.py` | `get_events_by_conversation()` 按 conversation_id 过滤 | 后端 |
| B1.6 | 对话上下文构建函数 | `harness/models/conversation.py` | `_build_conversation_context()` 从事件流折叠摘要 | 后端 |
| B1.7 | 对话 API 路由 | `harness/api/routes.py` | 6 个 REST 端点（CRUD + send message） | 后端 |
| B1.8 | `POST /api/v1/runs` 支持 conversation_id | `harness/api/routes.py` + `schemas.py` | 接收可选 `conversation_id`，创建 Run 时关联对话 | 后端 |
| B1.9 | send_message 核心流程 | `harness/api/routes.py` | `_handle_send_message()`: 构建上下文 → create_run → 写 `ConversationMessage` | 后端 |
| B1.10 | Run 完成后写 assistant 消息 | `harness/core/scheduler.py` | 在 `_run_loop` finally 处，有 `conversation_id` 时写 `ConversationMessage(role=assistant)` | 后端 |
| B1.11 | fold 兼容对话事件 | `harness/core/fold.py` | `CONVERSATION_*` 事件在 fold 中透传或跳过（非 Run 级事件） | 后端 |
| B1.12 | 测试 | `tests/test_conversation.py` **新文件** | 对话 CRUD + send_message + 上下文注入 + 存量兼容 | 后端 |

#### 前端任务（Phase 1 最小适配）

Phase 1 前端不改 ChatDrawer 核心 UI，仅做 API 适配：

| # | 任务 | 文件 | 交付物 | 负责人 |
|---|------|------|--------|--------|
| F1.1 | 新增对话 API 调用函数 | `frontend/src/api/` | `createConversation()`, `listConversations()`, `getConversation()`, `sendMessage()` | 前端 |
| F1.2 | `createRun` 支持 `conversation_id` | `frontend/src/api/` | 修改 `createRun()` 接受可选参数 | 前端 |

### 2.7 前后端联调契约

#### WebSocket 消息扩展

当前 WebSocket 推送 `run_id` 维度的事件。Phase 1 新增 `conversation_id` 维度推送：

```typescript
// WebSocket 消息新增 fields
interface WsEvent {
  run_id: string;
  conversation_id?: string;  // 新增
  event_type: string;
  seq: number;
  payload: any;
}
```

#### 新增 API 契约验证用例

| 场景 | 请求 | 期望响应 |
|------|------|---------|
| 创建对话 | `POST /api/v1/conversations {}` | `201 {"conversation_id": "c_xxx", "title": "New conversation"}` |
| 发送消息 | `POST /api/v1/conversations/{id}/messages {"message":"hello"}` | `200 {"run_id": "r_xxx", "conversation_id": "c_xxx", "seq": 1}` |
| 第 2 条消息含上下文 | 同上，同一 conversation | `intent` 中包含前一轮摘要 |
| 存量兼容 | `POST /api/v1/runs {"intent":"hi"}` | 行为不变，`conversation_id` 为 null |

### 2.8 验收检查清单

- [ ] `Conversation` 模型定义完整，字段全
- [ ] 3 个对话事件类型定义完整（Enum + Payload + PAYLOAD_MODEL_MAP）
- [ ] `conversations` 表创建正确，`events.conversation_id` 列可空
- [ ] `POST /api/v1/conversations` → 201 + `ConversationStarted` 事件写入
- [ ] `POST /api/v1/conversations/{id}/messages` → 创建 Run + 写入 `ConversationMessage`
- [ ] 同一对话中第 2 条消息的 intent 包含前一轮摘要
- [ ] Run 完成时自动写入 `ConversationMessage(role=assistant)`
- [ ] `GET /api/v1/conversations` 按 `updated_at` 降序
- [ ] `GET /api/v1/conversations/{id}` 返回完整消息列表
- [ ] `POST /api/v1/runs` 无 `conversation_id` 时行为完全不变
- [ ] `DELETE /api/v1/conversations/{id}` 软删除（标记 archived）
- [ ] 对话上下文注入不超过最近 3 轮（6 条消息）
- [ ] 存量 341 项测试不受影响
- [ ] 新增测试 ≥ 20 项

---

## 3. Phase 2 — 执行控制 + Fallback 优化

### 3.1 RunMonitor 硬控制

#### 设计原理

当前 Monitor 只能写文本反馈，不能强制执行。在多轮对话场景下，异常 Run 如不熔断会浪费用户时间。新增 `RunCommand` 事件 + `Scheduler` 侧检查点。

#### 新增事件

```python
# events.py 新增
EventType.RUN_COMMAND = "RunCommand"

class RunCommandPayload(BaseModel):
    command: Literal["hard_abort", "soft_abort", "pause", "skip_tool", "lower_parallel"]
    reason: str
    affected_tool: str | None = None
    issued_by: str = "monitor"  # "monitor" | "operator"
```

#### 执行流程

```
RunMonitor 检测异常
    ↓
写入 RunCommand 事件
    ↓
Scheduler._check_pending_commands()   ← 每次循环开始时调用
    ├─ hard_abort → 写 RunFailed, 终止
    ├─ soft_abort → 等待当前工具完成 → 写 RunFailed
    ├─ pause → 写 RunPaused, 暂停
    └─ skip_tool → 跳过当前工具并继续
```

#### 注入点

`PlanningExecutorScheduler._plan_execute_revise_loop()` 和 `AgentLoopScheduler._run_loop()` 的每次迭代开始处：

```python
async def _check_pending_commands(self, run_id: str) -> str | None:
    """检查是否有待执行命令，返回命令类型或 None"""
    events = await self.store.get_events(run_id)
    for e in reversed(events):
        if e.event_type == EventType.RUN_COMMAND:
            cmd = e.payload.command
            # 只处理尚未处理的命令（通过已处理的事件号记录）
            if e.seq > self._last_processed_command_seq:
                self._last_processed_command_seq = e.seq
                return cmd
    return None
```

#### Monitor 自动熔断触发条件

| 熔断条件 | 阈值 | 命令 | 说明 |
|----------|------|------|------|
| 连续工具调用失败 | ≥5 次 | `hard_abort` | 断路器兜底 |
| Token 消耗超限 | ≥预算 120% | `soft_abort` | 防止无限消耗 |
| 执行时间超限 | ≥配置 max_duration | `hard_abort` | 超时自动终止 |
| 循环检测触发 | 连续 6 次重复签名 | `hard_abort` | 循环检测补充（之前只写反馈，现在加熔断） |

### 3.2 Fallback Kernel tools API 改造

#### 当前问题

`_FallbackKernel` [harness/core/scheduler/fallback_kernel.py] 使用纯文本指令格式：

```
THOUGHT: ...
TOOL: http_request
ARGS: {"url": "..."}
```

→ 正则解析，脆弱

#### 改造目标

复用 `LLMAgentKernel` 的 tools API 路径：

```python
# fallback_kernel.py 改造后
class FallbackKernel:
    def __init__(self, llm_client, tool_defs):
        self.client = llm_client
        self.tool_schemas = build_tool_schemas(tool_defs)  # 复用现有函数

    async def think(self, intent, state, feedback=None):
        messages = self._build_messages(intent, state, feedback)
        response = await self.client.chat(
            messages=messages,
            tools=self.tool_schemas,    # ← 使用 tools API
            tool_choice="auto"
        )
        # 直接读取 response.tool_calls（复用 agent_kernel 的解析逻辑）
        return self._parse_response(response)
```

#### 关键点

- `FallbackKernel` 改为调用 `LLMClient.chat(tools=...)`，与 `LLMAgentKernel` 一致
- 移除 `_ARGS_GREEDY_RE` 等脆弱正则
- `_parse_response` 复用 `agent_kernel.py` 中的 `_parse_tool_calls` 逻辑
- 向后兼容：`FallbackKernel` 接口不变（`think(intent, state, feedback)`）

### 3.3 任务拆解

#### 后端任务

| # | 任务 | 文件 | 交付物 | 负责人 |
|---|------|------|--------|--------|
| B2.1 | `RunCommand` 事件类型 + Payload | `harness/models/events.py` | 枚举 + Payload Model + fold 支持 | 后端 |
| B2.2 | Scheduler 命令检查机制 | `harness/core/scheduler.py` (BaseScheduler) | `_check_pending_commands()` + `_last_processed_command_seq` 字段 | 后端 |
| B2.3 | 5 种命令的处理逻辑 | `harness/core/scheduler.py` | hard_abort / soft_abort / pause / skip_tool / lower_parallel 在 `_plan_execute_revise_loop` 和 `_run_loop` 中的响应 | 后端 |
| B2.4 | Monitor 自动熔断增强 | `harness/monitoring/run_monitor.py` | 连续 5 次失败 → hard_abort；token 超 120% → soft_abort | 后端 |
| B2.5 | `FallbackKernel` tools API 改造 | `harness/core/scheduler/fallback_kernel.py` | 使用 tools API + 移除正则解析 | 后端 |
| B2.6 | `_parse_response` 复用 | `harness/core/agent_kernel.py` | 提取 `_parse_tool_calls` 为独立函数，FallbackKernel 复用 | 后端 |
| B2.7 | 测试 | `tests/test_commands.py` + `tests/test_fallback.py` | 5 种命令执行 + FallbackKernel tools API + 存量测试 | 后端 |

---

## 4. Phase 3 — 上下文管理优化

### 4.1 动态上下文窗口

#### 当前问题

`LLMAgentKernel.think()` 硬编码 `window = 5`，始终只传最近 5 轮。

#### 改造方案

```python
# agent_kernel.py
class LLMAgentKernel:
    def __init__(self, ...):
        self.max_context_tokens = 8000  # 对话保留预算

    def _compute_dynamic_window(self, state, max_tokens) -> int:
        """基于 token 估算计算能保留多少轮"""
        tokens_per_thought = 200  # 平均每轮 thought 的 token 数
        tokens_per_result = 500   # 平均每轮 tool result 的 token 数（已截断后）
        round_cost = tokens_per_thought + tokens_per_result
        return max(1, max_tokens // round_cost)

    async def think(self, intent, tool_defs, state, feedback=None):
        window = self._compute_dynamic_window(state, self.max_context_tokens)
        timeline = []
        for t in state.thought_history[-window:]:
            timeline.append(("thought", t))
        for tr in state.tool_results[-window:]:
            timeline.append(("result", tr))
        # ...
```

#### 自适应行为

| 场景 | 估算窗口 | 说明 |
|------|----------|------|
| 简单问答 | 20+ 轮 | 每轮 token 少 |
| 工具密集型 | 5-8 轮 | 工具结果占 token 多 |
| 大文件读取 | 2-3 轮 | 单轮 token 巨大 |

### 4.2 工具结果智能截断

#### 当前问题

`tool_results.output` 是完整原始结果，传给 LLM 时可能非常大。

#### 改造方案

新增 `truncate_tool_output()` 函数，按工具类型策略截断：

```python
# harness/core/agent_kernel.py 或新文件 harness/core/truncation.py

TRUNCATION_RULES = {
    "http_request": {"max_chars": 2000, "keep_fields": ["status_code", "body"]},
    "browser": {"max_chars": 1000, "keep_fields": ["text_content"]},
    "file_op": {"max_chars": 500, "keep_fields": ["content"]},
    "search": {"max_chars": 1000, "keep_fields": ["results"]},
    "mcp_call": {"max_chars": 1500, "keep_fields": None},
    "__default__": {"max_chars": 1000, "keep_fields": None},
}

def truncate_tool_output(tool_name: str, output: dict, tool_result_type: str) -> dict:
    """按工具类型截断输出，返回截断后的结果"""
    if tool_result_type != ToolResultType.SUCCESS:
        return {"error": str(output)[:500]}
    
    rules = TRUNCATION_RULES.get(tool_name, TRUNCATION_RULES["__default__"])
    truncated = {}
    
    if rules["keep_fields"]:
        for field in rules["keep_fields"]:
            if field in output:
                val = str(output[field])
                if len(val) > rules["max_chars"]:
                    truncated[field] = val[:rules["max_chars"]] + "..."
                else:
                    truncated[field] = val
    else:
        content = str(output)
        if len(content) > rules["max_chars"]:
            truncated["_truncated"] = content[:rules["max_chars"]] + "..."
        else:
            truncated["_content"] = content
    
    return truncated
```

注入点：在 `LLMAgentKernel.think()` 构建 `timeline` 时，对 `tool_results` 先截断再传入。

### 4.3 任务拆解

| # | 任务 | 文件 | 交付物 | 负责人 |
|---|------|------|--------|--------|
| B3.1 | 动态窗口计算 | `harness/core/agent_kernel.py` | `_compute_dynamic_window()` + 替换硬编码 `window = 5` | 后端 |
| B3.2 | 工具结果截断函数 | `harness/core/truncation.py` **新文件** | `truncate_tool_output()` + 截断规则表 | 后端 |
| B3.3 | think() 集成截断 | `harness/core/agent_kernel.py` | 构建 timeline 时对 tool_results 进行截断 | 后端 |
| B3.4 | 测试 | `tests/test_agent_kernel.py` | 动态窗口边界 + 各工具截断规则 + 超大结果截断 | 后端 |

---

## 5. Phase 4 — 前端对话式 UI 重写

### 5.1 架构变更

#### 当前架构

```
ChatDrawer (单个组件)
  ├─ 绑定一个 Run (1:1)
  ├─ 输入框在 Run 运行时 disabled
  └─ 每次提交创建新的 Run 并替换显示
```

#### 目标架构

```
ConversationView (容器)
  ├─ ConversationSidebar (对话列表)
  │   ├─ 搜索框
  │   └─ 对话条目列表 (标题 + 摘要 + 时间)
  │
  └─ ConversationDrawer (聊天区域)
      ├─ 对话标题栏 (标题 + 操作按钮)
      ├─ 消息列表
      │   ├─ UserMessage (用户消息泡)
      │   ├─ AssistantMessage (Agent 回答)
      │   │   ├─ ThinkingPanel (思考过程, 可折叠)
      │   │   ├─ ToolCallCard (工具调用, 实时状态)
      │   │   ├─ ConfirmationCard (确认卡片)
      │   │   └─ FinalAnswer (最终回答, Markdown 渲染)
      │   └─ PendingMessage (排队中的消息)
      ├─ 排队队列指示器 ("还有 N 条消息等待执行")
      └─ 输入框 (始终可用, 支持回车发送)
```

### 5.2 组件结构

```
frontend/src/components/
├── ConversationView.tsx          ← 容器: 侧边栏 + 聊天区
├── ConversationSidebar.tsx       ← 对话列表
├── ConversationDrawer.tsx        ← 聊天区 (替代 ChatDrawer)
├── MessageBubble.tsx             ← 消息气泡 (用户/Agent)
├── ThinkingPanel.tsx             ← Agent 思考 (可折叠)
├── ToolCallCard.tsx              ← 工具调用卡片 (实时状态)
├── ConfirmationCard.tsx          ← 确认卡片
├── FinalAnswer.tsx               ← Markdown 最终回答
└── PendingIndicator.tsx          ← 排队指示器

frontend/src/pages/
└── OpsChatView.tsx               ← 布局页 (引用 ConversationView)
```

### 5.3 数据流

```
用户输入 "用中文总结"
    ↓
ConversationDrawer.handleSubmit()
    ↓
sendMessage(conversation_id, "用中文总结")
    ↓
POST /api/v1/conversations/{id}/messages
    ↓
后端: 构建上下文 → create_run_with_context
    ↓ 返回 {run_id, conversation_id, seq}
    ↓
前端订阅 WebSocket /api/v1/runs/{run_id}/events
    ↓
消息实时追加到当前对话的消息列表
    ↓
Run 完成 → 自动写 ConversationMessage(role=assistant)
    ↓
前端通过 WS 收到 RunCompleted → 更新消息气泡状态

同时:
用户可立即输入下一条（排队）
    ↓
消息显示为 "排队中..." 状态
    ↓
当前 Run 完成 → 自动发送排队中的下一条
```

### 5.4 任务拆解

#### 前端任务

| # | 任务 | 文件 | 交付物 | 负责人 |
|---|------|------|--------|--------|
| F4.1 | `MessageBubble.tsx` | 新建 | 通用消息气泡：用户/Agent 样式区分 | 前端 |
| F4.2 | `ThinkingPanel.tsx` | 新建 | Agent 思考过程组件，可折叠展开，动画 | 前端 |
| F4.3 | `ToolCallCard.tsx` | 新建 | 工具调用实时状态卡片（搜索中.../完成/失败） | 前端 |
| F4.4 | `ConfirmationCard.tsx` | 新建 | 确认卡片（从 ChatDrawer 抽取） | 前端 |
| F4.5 | `FinalAnswer.tsx` | 新建 | Markdown 渲染最终回答 | 前端 |
| F4.6 | `ConversationDrawer.tsx` | 新建 | 聊天区主体：消息列表 + 输入框 + 排队指示器 | 前端 |
| F4.7 | `ConversationSidebar.tsx` | 新建 | 对话侧边栏：搜索 + 列表 + 新建按钮 | 前端 |
| F4.8 | `ConversationView.tsx` | 新建 | 容器：侧边栏 (可折叠) + 聊天区 | 前端 |
| F4.9 | `PendingIndicator.tsx` | 新建 | 排队消息指示器 | 前端 |
| F4.10 | API 层：对话操作函数 | `frontend/src/api/` | `createConversation`, `listConversations`, `getConversation`, `sendMessage`, `deleteConversation` | 前端 |
| F4.11 | 排队输入逻辑 | `ConversationDrawer.tsx` | 输入框始终可用 → 排队队列 → 自动发送 | 前端 |
| F4.12 | WebSocket 多 Run 订阅 | 前端 WS hook | 同时订阅活跃 Run 的事件流 | 前端 |
| F4.13 | OpsChatView 布局适配 | `frontend/src/pages/OpsChatView.tsx` | 引入 `ConversationView` 替代 `ChatDrawer` | 前端 |
| F4.14 | 对话历史持久化 | local storage | 刷新后恢复上次对话 | 前端 |
| F4.15 | 测试 | 前端测试文件 | 组件渲染 + API 调用 + WS 事件处理 | 前端 |

### 5.5 后端配合变更

| # | 任务 | 文件 | 交付物 |
|---|------|------|--------|
| B4.1 | WebSocket 支持 `conversation_id` 订阅 | `harness/api/ws.py` | 客户端可订阅 `conversation_id` 维度的事件流 |
| B4.2 | `GET /api/v1/conversations/{id}/events` | `harness/api/routes.py` | 获取对话的事件流（合并其中所有 Run 的事件） |

### 5.6 前后端联调契约（补充）

#### 排队输入 API

前端不需要额外的排队 API。排队纯前端逻辑：

```typescript
// 前端排队队列
interface QueuedMessage {
  text: string;
  status: "queued" | "sending" | "sent";
}

// 核心流程
const queue = ref<QueuedMessage[]>([]);
const isExecuting = ref(false);

async function handleSubmit(text: string) {
  if (isExecuting.value) {
    queue.value.push({ text, status: "queued" });
    return;
  }
  await sendMessage(conversationId, text);
}

// 监听到 RunCompleted 后，发送队列中的下一条
watch(activeRunStatus, (status) => {
  if (status === "completed" || status === "failed") {
    isExecuting.value = false;
    if (queue.value.length > 0) {
      const next = queue.value.shift()!;
      sendMessage(conversationId, next.text);
    }
  }
});
```

#### WebSocket 多 Run 订阅

```typescript
// 当前: 一次只订阅一个 Run
// 目标: 自动转到当前活跃 Run
useRunWebSocket(activeRunId, (event) => {
  // 按 conversation_id 路由到对应对话
  if (event.conversation_id === currentConversationId) {
    messages.value.push(event);
  }
});
```

---

## 6. 测试策略

### 6.1 分层测试

| 测试类型 | 目标组件 | 新增用例数 | 关注点 |
|----------|----------|-----------|--------|
| **单元测试** | Conversation 模型、上下文构建、截断函数 | 15+ | 纯函数，无 I/O |
| **集成测试** | Event Store 对话操作、API 端点、Run 关联 | 20+ | 事件写入/查询/上下文注入 |
| **端到端测试** | send_message → Run 执行 → assistant 消息写入 → 多轮上下文正确 | 5+ | 完整对话环 |
| **回归测试** | 存量 341 项测试 | 0 | 不破坏现有功能 |

### 6.2 关键测试场景

#### Phase 1 测试

```
test_conversation_crud:
  - 创建对话 → ConversationStarted 事件写入
  - 对话中发送消息 → RunStarted + ConversationMessage 写入
  - 同一对话第二条消息包含前一条摘要
  - 对话列表排序正确
  - 删除对话软标记
  - 无 conversation_id 的 Run 行为不变

test_conversation_context_injection:
  - 第 N 条消息的 intent 包含前 N-1 轮的摘要
  - 对话超过 3 轮时只注入最近 3 轮
  - Run 完成时 ConversationMessage(role=assistant) 写入

test_backward_compatibility:
  - POST /api/v1/runs 无 conversation_id → RunStarted 无关联
  - 已有 341 项测试全部通过
```

#### Phase 2 测试

```
test_run_commands:
  - hard_abort → Run 立即终止
  - soft_abort → 当前工具完整执行后终止
  - pause → Run 切换到 PAUSED
  - skip_tool → 跳过当前工具继续
  - 不存在的命令 → 被忽略

test_fallback_kernel_tools_api:
  - FallbackKernel 使用 tools API 返回结构化响应
  - 移除正则解析后解析不再脆弱
  - 与 LLMAgentKernel 行为一致
```

#### Phase 3 测试

```
test_dynamic_window:
  - 成本估算函数返回合理窗口值
  - 工具结果少时窗口增大
  - 工具结果多时窗口减小
  - window 最小为 1

test_tool_truncation:
  - http_request body 超过 2000 chars 被截断
  - browser text_content 超过 1000 chars 被截断
  - 未知工具使用默认规则
  - SOFT_ERROR 时截断 error 字段
```

---

## 7. 发布顺序与回退方案

### 7.1 发布顺序

```
Phase 1 (后端) ──────────→ Phase 3 (后端) ──→ Phase 4 (前端)
                          ↘
                    Phase 2 (后端)

不能先做 Phase 4 再做 Phase 1，原因：
  Phase 4 的前端 ConversationDrawer 依赖 Phase 1 的对话 API
  Phase 3 是 Phase 4 的前置（上下文管理影响多轮体验）
```

### 7.2 推荐实施顺序

| 顺序 | Phase | 原因 |
|------|-------|------|
| 1 | Phase 1 (后端) | 基础设施，最小可行多轮对话 |
| 2 | Phase 2 (后端) | 执行控制，与 Phase 1 无依赖，可并行 |
| 3 | Phase 3 (后端) | 体验优化，为 Phase 4 做准备 |
| 4 | Phase 4 (前端) | 用户可见的完整体验，需后端就绪 |

### 7.3 回退方案

| 风险 | 回退动作 | 影响范围 |
|------|----------|----------|
| Phase 1 数据库变更导致存量数据损坏 | 回滚 `conversations` 表 + `ALTER TABLE` 迁移 | 仅 Phase 1 |
| Phase 2 命令检查增加 Scheduler 延迟 | 通过 feature flag `ENABLE_RUN_COMMANDS=false` 禁用 | 仅 Phase 2 |
| Phase 3 截断导致 LLM 信息不足 | 调高截断阈值或恢复为不截断 | 仅 Phase 3 |
| Phase 4 前端不稳定 | 保留 `ChatDrawer` 组件，通过 feature flag 切换 | 仅 Phase 4 |

### 7.4 Feature Flag 设计

```python
# harness/config.py
class FeatureFlags:
    enable_conversations: bool = True     # Phase 1
    enable_run_commands: bool = True      # Phase 2
    enable_dynamic_window: bool = True    # Phase 3
    enable_tool_truncation: bool = True   # Phase 3
```

---

*本计划基于 PRD_v1.0.md 需求、ARCHITECTURE_v2.1.md 架构约束和 agent_execution_audit_20260703(1).md 审计发现编写。*
*任务拆解以"可独立交付、可独立验证"为粒度，每个任务完成后均可合并到主线。*
