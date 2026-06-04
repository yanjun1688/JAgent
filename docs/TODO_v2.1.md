# Harness v2.1 — 实现路线图

> 基于 `harness_v2.1.md` 架构方向生成
> 已完成里程碑：MVP → V0.2 → V0.3 → V0.4 → V0.4+ → V0.5 → V0.5+ → V0.6

---

## 已完成里程碑总览

| 里程碑 | 核心交付 | 状态 |
|--------|----------|------|
| MVP | Event Store + Tool Layer + Scheduler + Agent Kernel 基础循环 | ✅ |
| V0.2 | ToolRegistry + browser / http_request / file_op / mcp_call / SKILL | ✅ |
| V0.3 | FastAPI REST+WS 后端 + React 前端（Run 列表/详情/确认 UI） | ✅ |
| V0.4 | ScopeGuardrail / RateLimitGuardrail / DestructiveOpGuardrail / DependencyGuardrail + 确认 UI 完善 | ✅ |
| V0.4+ | Orchestrator 动态编排 + PlanGuardrail + 步骤级安全继承 | ✅ |
| V0.5 | Context Manager（自动压缩/滚动摘要/Checkpoint）+ 断点续传 + 100 轮压力测试 | ✅ |
| V0.5+ | EpisodeSummary 结构化摘要 + 紧急压缩 + 239 项测试全通过 | ✅ |
| V0.6 | RunMonitor + FeedbackInjected + Scheduler 反馈注入 + 261 项测试全通过 | ✅ |

当前基线：**261 项测试全通过**（239 历史 + 22 监控反馈）。

---

## V0.5+ — 记忆压缩优化（P1）

**前置依赖**: V0.5 完成
**目标**: 将当前纯文本摘要升级为结构化摘要，支持紧急压缩

### 设计要点

当前 `ContextCompressedPayload.summary_ref` 类型为 `str`（纯文本），V0.5+ 改为结构化 `EpisodeSummary`：

```python
class EpisodeSummary(BaseModel):
    episode_range: tuple[int, int]   # 覆盖的 seq 范围
    original_tokens: int
    compressed_tokens: int
    key_decisions: list[str]         # Agent 做出的关键决策
    tools_used: list[str]            # 使用了哪些工具
    key_findings: list[str]          # 发现的重要信息
    errors_encountered: list[str]    # 遇到的错误
    current_plan: str | None         # 当时的计划
    original_event_refs: list[int]   # 原始事件的 seq 列表
```

LLM 摘要生成需输出 JSON（通过 `response_format` 或 JSON mode），无 LLM 时降级为纯文本。

### 任务清单

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 8.6 | `EpisodeSummary` Pydantic Model 定义 | `harness/models/events.py` | 字段完整，支持 JSON Schema 序列化 | 0.5d |
| 8.7 | ContextManager 摘要改为结构化输出 | `harness/core/context_manager.py` | LLM 输出 JSON 结构降级为纯文本；已有测试不破坏 | 1d |
| 8.8 | 紧急压缩策略 | ContextManager 新增 `select_compression_window()` | 超过阈值时压缩最旧的 50% 事件，保留最近 3 轮 | 1d |
| 8.9 | 测试更新 | `tests/test_context_manager.py` | 结构化摘要类型检查 + 紧急压缩触发验证 | 1d |

### 验收检查清单

- [x] `EpisodeSummary` 结构完整，折叠后 `state.summary` 为结构化字段而非纯文本
- [x] 有 LLM 时摘要输出 JSON，无 LLM 时降级纯文本
- [x] 紧急压缩：超 80% 阈值时压缩旧 50% 事件，保留最近 3 轮
- [x] 已有 V0.5 测试不受影响

---

## V0.6 — 监控与反馈系统（P0）

**状态**: ✅ 已完成

**前置依赖**: V0.5 完成
**目标**: 独立受信组件 `RunMonitor`，实时监听 Event Store，通过 System Prompt 注入反馈

### 设计要点

- **不是 Agent 调用的 Tool**，而是独立运行在 Scheduler 内部协程的受信组件
- 监控端通过 `EventStore.on_append` 回调实时接收事件，无需轮询
- 反馈通过 `FeedbackInjected` 事件写入 Event Store，Scheduler 在 THINK 前读取并注入 AgentKernel
- Agent 不感知反馈机制，反馈像"环境信息"一样自动出现在感知中
- MVP 范围：异常检测（连续失败）+ Token 超限预警；**成本追踪推迟到 V1.0**

### 反馈生命周期

- **简单可扩展**：每次只注入最新的 N 条反馈（默认 5 条）
- 通过 `RunMonitor.get_active_feedbacks()` 一个方法封装筛选逻辑
- 今后扩展（按优先级筛选/过期 seq/保留未读）只改这一个方法，调用方不变

### 断路器关系

- Scheduler 已有 `max_consecutive_failures=5` 熔断，Monitor 在连续 3 次失败时注入反馈，目的是**给 Agent 提前预警、自我纠正的机会**
- 两者互补：反馈让 Agent 自愈，断路器兜底终止

### 反馈注入方式（问题 1 - 方案 A）

`AgentKernel.think()` 新增可选参数 `feedback: str | None = None`，Scheduler 在调用前拉取 `FeedbackInjected` 事件组装为字符串传入。改动最小、不破坏已有代码。

### 架构

```
Event Store append
    ↓ (on_append 回调)
RunMonitor.on_event(event)
    ├─ token 消耗统计（复用 ContextManager 估算）
    └─ 异常检测（连续失败、Guardrail 触发率）
    ↓
检测到异常 → 写入 FeedbackInjected 事件
    ↓
Scheduler THINK 前 → 拉取 FeedbackInjected
    → 组装为字符串 → 传入 AgentKernel.think(feedback=...)
```

### 新增事件类型

| 事件类型 | 写入方 | 关键字段 |
|----------|--------|----------|
| `FeedbackInjected` | RunMonitor | `feedback_text: str, priority: str = "medium"` |

### 新增/修改文件

| 文件 | 职责 |
|------|------|
| `harness/monitoring/run_monitor.py` | `RunMonitor` 类，订阅 Event Store，分析实时指标 |
| `harness/monitoring/__init__.py` | 模块初始化 |
| `harness/core/scheduler.py` | `AgentKernel.think()` 新增 `feedback` 参数；Scheduler 拉取反馈并传入 |

### 任务清单

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 9.1 | `FeedbackInjected` 事件类型定义 | `harness/models/events.py` | 枚举 + Payload Model + PAYLOAD_MODEL_MAP + fold 支持 | 0.5d |
| 9.2 | `RunMonitor` 核心实现 | `harness/monitoring/run_monitor.py` | 订阅 `on_append`；连续 3 次 `ToolFailed` 注入高优先级反馈；token 超 80% 注入预警 | 2d |
| 9.3 | `AgentKernel.think()` 新增 `feedback` 参数 | `harness/core/scheduler.py` | `think(intent, tool_defs, state, feedback=None)`；向后兼容 | 0.5d |
| 9.4 | `LLMAgentKernel` 注入反馈至 System Prompt | `harness/core/agent_kernel.py` | `think()` 收到 `feedback` 后追加一条 `{"role":"system"}` 消息（位置：基础 Prompt 之后、摘要之前），Agent 可见 | 0.5d |
| 9.5 | Scheduler 拉取反馈并传入 Kernel | `harness/core/scheduler.py` | THINK 前从 `state.feedbacks[-5:]`（折叠状态）取最新 N 条反馈 → 传入 `think(feedback=...)` | 1d |
| 9.6 | 监控挂载方式 | `harness/monitoring/run_monitor.py` | `RunMonitor.attach(store)` 注册 `on_append` 回调 | 0.5d |
| 9.7 | 测试 | `tests/test_monitoring.py` | 事件监听、异常检测触发反馈、Scheduler 拉取、Kernel 注入 System Prompt、已有测试不受影响 | 1.5d |

### 验收检查清单

- [x] `FeedbackInjected` 事件类型定义完整（EventType 枚举 + Payload Model + PAYLOAD_MODEL_MAP + fold）
- [x] `RunMonitor` 通过 `on_append` 回调实时接收事件，不依赖 Agent 配合
- [x] 连续 3 次 `ToolFailed` 触发高优先级反馈注入
- [x] `AgentKernel.think()` 新增 `feedback` 参数，默认 `None`，向后兼容
- [x] `LLMAgentKernel.think()` 将 feedback 注入 System Prompt 消息段，Agent 感知
- [x] Scheduler THINK 前从 `state.feedbacks[-5:]`（折叠状态）取最新 N 条反馈传入 AgentKernel
- [x] 反馈通过 EventStore → fold → state.feedbacks 路径，不经过内存缓冲
- [x] Agent 不感知反馈机制，不回调反馈相关工具
- [x] 已有 261 项测试不受影响

---

## V1.0 — 生产就绪（P1/P2）

**前置依赖**: V0.6 完成
**目标**: 多租户隔离 + 鉴权 + 语义记忆 + 业务适配

### 架构

```
┌─────────────────────────────────────────┐
│  业务适配层（Business Adapter）          │  ← P2
│  ├─ 业务领域定义（领域模型、术语表）      │
│  ├─ 业务规则引擎（什么状态下做什么）      │
│  └─ 输出格式化                          │
├─────────────────────────────────────────┤
│  多租户隔离层（Multi-tenancy）           │  ← P1
│  ├─ 用户角色与权限（RBAC / ToolACL）     │
│  ├─ 用户记忆隔离（每用户独立语义记忆）    │
│  └─ 资源配额（token 预算、并发限制）      │
├─────────────────────────────────────────┤
│  监控与反馈系统（V0.6）                 │
│  现有架构（MVP→V0.5）                   │
└─────────────────────────────────────────┘
```

### 9.1 — 分层语义记忆（P1）

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 10.1 | Semantic Memory 抽象接口 | `harness/memory/semantic.py` | 支持 `store(memory)` / `search(query, user_id, top_k, filters)` | 1d |
| 10.2 | 内存向量存储实现（MVP） | `harness/memory/in_memory.py` | 基于 numpy 或 sklearn 的向量检索原型 | 1d |
| 10.3 | 用户记忆隔离 | 所有记忆操作强制带 `user_id` | 用户 A 的偏好不能影响用户 B | 1d |
| 10.4 | 记忆注入 Working Memory | Scheduler 在 THINK 前检索语义记忆 | 用户画像作为 System Prompt 段注入 | 1d |

### 9.2 — 用户角色与鉴权（P1）

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 11.1 | `ToolACL` 权限模型 | `harness/auth/acl.py` | 角色-工具映射；`can_invoke()` / `can_confirm()` | 1.5d |
| 11.2 | ACL 集成到 Executor | Executor 在 Guardrails 后、ToolCalled 前检查 | 拒绝时写入 `GuardrailTriggered(guardrail_id="acl_denied")` | 1d |
| 11.3 | `user_id` 贯穿调用链 | API → Scheduler → Executor → Store | API 接收 `user_id`，Scheduler 透传给 ACL | 1d |
| 11.4 | 确认权限控制 | 确认接口检查用户角色 | critical 级别仅 admin 可确认 | 0.5d |
| 11.5 | 测试 | `tests/test_acl.py` | 角色拦截、角色放行、确认权限 | 1d |

### 9.3 — 业务兼容（P2）

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 12.1 | 业务 SKILL 示例（电商订单） | `harness/tools/skills/order_skill.py` | 对外单工具，内部多步业务逻辑 | 1.5d |
| 12.2 | 业务适配器框架 | `harness/adapter/` 模块 | 业务输入 → 内部格式 → 业务输出 | 1d |

---

## 总结优先级

| 方向 | 优先级 | 预计投入 | 关键决策 |
|------|--------|----------|----------|
| 监控 + 反馈 | **P0** — 现在就能做 | 5d | 不通过 Tool，通过 System Prompt 注入反馈；使用 on_append 回调 |
| 记忆压缩优化 | **P1** — V0.5+ | 3.5d | 改为结构化 `EpisodeSummary`；无 LLM 降级纯文本 |
| 语义记忆 + 鉴权 | **P1** — V1.0 Phase 1 | 7d | 记忆按 `user_id` 隔离；权限在 Tool Layer 前拦截 |
| 业务兼容 | **P2** — V1.0 Phase 2 | 2.5d | 通过 SKILL + 适配器封装业务流程 |

---

*基于 `harness_v2.1.md` 架构方向生成*
*分层推进，禁止跨层，每层交付物需验收后方可进入下一层*
