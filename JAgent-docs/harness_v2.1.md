 我来帮你系统性地梳理这些问题。基于你的 Harness v2.2 架构（已完成 MVP→V0.5），你的这些想法都是合理的演进方向，但需要放在正确的架构位置上。让我逐一分析：

---

## 1. Agent 上面做有限状态机（FSM）—— 已放弃

v1.0 的教训已说明——**不要让 Agent 成为图的囚徒**。v2.2 中 FSM 概念不再需要：

- **Skill** 机制可封装多步业务逻辑和状态转移
- **Orchestrator** 支持多步骤编排和失败终止
- **Guardrail** 框架（DependencyGuardrail）可做前置条件检查

业务流的约束已由现有组件覆盖，无需新增 FSM 层。

---

## 2. 监控机制 + 反馈机制——可以，且应该放在 Tool Layer

**结论：监控和反馈都是"受信组件"，可以并行运行，但不适合做成 Agent 调用的 Tool**

### 架构位置

```
┌─────────────────────────────────────────┐
│  监控与反馈系统（受信组件，并行运行）     │  ← 你的插入点
├─────────────────────────────────────────┤
│  Event Store 监听层                     │
│  ├─ 实时事件流分析（token 消耗、延迟）   │
│  ├─ 异常检测（连续失败、Guardrail 触发率）│
│  └─ 成本追踪（每 Run LLM 调用成本）      │
├─────────────────────────────────────────┤
│  反馈注入层                             │
│  ├─ 检测到异常 → 自动写入 System Prompt  │
│  ├─ 检测到高成本 → 提示 Agent 精简推理   │
│  └─ 检测到循环 → 强制插入 break 提示     │
└─────────────────────────────────────────┘
              ↓
┌─────────────────────────────────────────┐
│  Agent Loop Scheduler（已有）            │
│  （监控反馈通过 System Prompt 或         │
│   上下文注入影响 Agent，Agent 不直接感知）│
└─────────────────────────────────────────┘
```

### 为什么不做成 Tool？

| 做成 Tool 的问题 | 作为受信组件的优势 |
|------------------|-------------------|
| Agent 可能忘记调用监控工具 | 系统自动订阅 Event Store，不依赖 Agent |
| 监控有延迟（需要一轮 think→act） | 事件产生即触发，真正的实时 |
| Agent 可能伪造监控结果 | 监控在受信边界外，Agent 无法干预 |
| 反馈需要 Agent 主动消费 | 通过 System Prompt 自动注入，Agent 被动接收 |

### 具体实现建议

**监控端（独立进程/线程，监听 Event Store）：**

```python
# harness/monitoring/run_monitor.py
class RunMonitor:
    """受信组件，订阅 Event Store，实时分析 Run 健康度"""
    
    async def on_event(self, event: Event):
        # 实时指标
        if event.event_type == EventType.AGENT_THOUGHT:
            self.token_counter.add(event.payload.token_count)
        
        # 异常检测
        if event.event_type == EventType.TOOL_FAILED:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                await self.inject_feedback(
                    run_id=event.run_id,
                    feedback="注意：已连续 3 次工具调用失败，建议检查输入参数或终止任务",
                    priority="high"
                )
        
        # 成本告警
        cost = self.estimate_cost(event)
        if cost > self.budget_threshold * 0.8:
            await self.inject_feedback(
                run_id=event.run_id,
                feedback="成本预警：已消耗预算 80%，建议精简后续步骤",
                priority="medium"
            )
    
    async def inject_feedback(self, run_id: str, feedback: str, priority: str):
        """将反馈写入 Run 的上下文（通过 Event Store 特殊事件或 System Prompt 更新）"""
        # 方式1：写入 FeedbackInjected 事件，Scheduler 下轮 THINK 前读取并加入 System Prompt
        # 方式2：直接修改 Scheduler 的 dynamic_system_prompt（如果 Scheduler 支持热更新）
```

**反馈注入机制：**

```python
# 在 Scheduler 的 THINK 步骤前插入
async def think(self):
    # 拉取动态反馈（来自 Monitor）
    feedback_events = await self.store.get_feedback_since(
        run_id=self.run_id, 
        after_seq=self.last_feedback_seq
    )
    
    # 构建临时 System Prompt 片段
    feedback_prompt = self._build_feedback_prompt(feedback_events)
    
    # 送入 LLM（作为 system prompt 的最后一段，优先级最高）
    messages = [
        {"role": "system", "content": base_system_prompt + feedback_prompt},
        ... # 历史上下文
    ]
    
    response = await self.llm.chat(messages, tools=...)
```

**关键设计：反馈是"环境信息"不是"工具结果"**

Agent 不需要调用 `check_monitor()`，监控信息像天气一样自动出现在它的感知中。

---

## 3. 记忆系统压缩策略——V0.5 的正确打开方式

你 V0.5 的任务清单（Context Manager + 滚动摘要 + Checkpoint）方向正确，但需要明确**分层记忆的架构**：

```
┌─────────────────────────────────────────┐
│  上下文窗口（Working Memory）            │  ← Agent 直接感知，V0.5 管理
│  ├─ 当前任务目标（intent）               │
│  ├─ 最近 N 轮 think/act/observe（热数据）│
│  └─ 动态反馈（来自 Monitor）             │
│  [由 Context Manager 自动压缩维护]        │
├─────────────────────────────────────────┤
│  事件存储（Episodic Memory）             │  ← 完整历史，V0.5 Checkpoint 加速
│  ├─ 全部事件流（永久保留）               │
│  ├─ Checkpoint 快照（每 20 轮一个）      │
│  └─ 压缩摘要（滚动生成，替代原始事件）    │
│  [由 Context Manager 异步生成]           │
├─────────────────────────────────────────┤
│  语义记忆（Semantic Memory）V1.0+        │  ← 向量数据库
│  ├─ 用户偏好（"用户喜欢用表格总结"）      │
│  ├─ 领域知识（"这个业务的审批规则是..."）  │
│  └─ 成功模式（"上次类似任务的最佳实践"）  │
│  [按需检索注入 Working Memory]           │
└─────────────────────────────────────────┘
```

### V0.5 压缩策略的具体建议

**滚动摘要的生成时机：**

| 触发条件 | 操作 | Agent 感知 |
|----------|------|------------|
| 每 10 轮工具调用 | 生成摘要事件，替换前 10 轮的原始事件 | 无感知，上下文突然变短 |
| Token 使用量 > 80% 阈值 | 紧急压缩：保留最近 3 轮 + 摘要 | 无感知 |
| Checkpoint 触发 | 写入 `ContextCheckpointed` + 生成摘要 | 无感知 |

**摘要内容结构（不是简单文本总结）：**

```python
class EpisodeSummary(BaseModel):
    """滚动摘要的数据结构，保留可检索的关键信息"""
    episode_range: tuple[int, int]  # 覆盖的 seq 范围
    original_tokens: int
    compressed_tokens: int
    
    # 结构化摘要（比纯文本更利于后续检索）
    key_decisions: list[str]        # Agent 做出的关键决策
    tools_used: list[str]           # 使用了哪些工具
    key_findings: list[str]         # 发现的重要信息
    errors_encountered: list[str]   # 遇到的错误
    current_plan: str | None        # 当时的计划（如果有）
    
    # 保留原始事件的引用，支持"展开"查看细节
    original_event_refs: list[int]  # 原始事件的 seq 列表
```

**Context Manager 的职责边界：**

```python
class ContextManager:
    """受信组件，Agent 无感知"""
    
    async def maybe_compress(self, run_id: str, current_events: list[Event]):
        total_tokens = self.estimate_tokens(current_events)
        
        if total_tokens > self.threshold * 0.8:
            # 1. 选择压缩范围（ oldest 的 50% 事件）
            to_compress = self.select_compression_window(current_events)
            
            # 2. 生成摘要（调用 LLM 或规则摘要）
            summary = await self.summarize(to_compress)
            
            # 3. 写入 ContextCompressed 事件
            await self.store.append_event(run_id, ContextCompressedEvent(
                original_tokens=sum(e.tokens for e in to_compress),
                compressed_tokens=summary.tokens,
                summary=summary.dict()
            ))
            
            # 4. 写入 Checkpoint（可选，取决于策略）
            await self.store.append_event(run_id, ContextCheckpointedEvent(
                checkpoint_seq=to_compress[-1].seq,
                snapshot_ref=summary.ref,
                token_count=summary.tokens
            ))
            
            # 5. 从当前上下文中移除被压缩的事件（逻辑移除，物理仍在 Event Store）
            # 这步由 Scheduler 消费 ContextCompressed 事件后执行
```

---

## 4. 业务兼容、用户角色记忆、鉴权——V1.0 的正确架构

这些问题不能"插入"到现有层，需要**新增横切层**：

```
┌─────────────────────────────────────────┐
│  业务适配层（Business Adapter）          │  ← 新增，V1.0
│  ├─ 业务领域定义（领域模型、术语表）      │
│  ├─ 业务规则引擎（什么状态下做什么）      │
│  └─ 输出格式化（符合业务系统的数据格式）  │
├─────────────────────────────────────────┤
│  多租户隔离层（Multi-tenancy）           │  ← 新增，V1.0
│  ├─ 用户角色与权限（RBAC / ToolACL）     │
│  ├─ 数据隔离（Event Store 多租户）       │
│  │  ├─ 表加 `tenant_id` 列，查询自动过滤  │
│  │  ├─ 拆分 Reader/Writer 接口：         │
│  │  │  ├─ Writer：完整 EventStore（受信组件）│
│  │  │  └─ Reader：只读 ScopedEventStore   │
│  │  └─ API 层注入当前租户上下文           │
│  ├─ 用户记忆隔离（每用户独立语义记忆）    │
│  └─ 资源配额（token 预算、并发限制）      │
├─────────────────────────────────────────┤
│  你的现有架构（MVP→V0.5）               │
│  ├─ Interface Layer                     │
│  ├─ Agent Loop Scheduler                │
│  ├─ Stateful Agent                      │
│  ├─ Tool Layer                          │
│  ├─ Context Manager                     │
│  └─ Event Store                         │
└─────────────────────────────────────────┘
```

### 用户角色记忆

**不是放在 Agent 的上下文里，而是作为检索前置：**

```python
# 在 Scheduler THINK 之前，从语义记忆中检索用户相关记忆
async def prepare_context(self, run_id: str, user_id: str, intent: str):
    # 1. 基础上下文（事件流折叠）
    base_context = fold_events(await self.store.get_events(run_id))
    
    # 2. 检索用户相关记忆（语义层）
    user_memories = await self.semantic_memory.search(
        query=intent,
        user_id=user_id,           # 隔离！只能搜到这个用户的记忆
        top_k=5,
        filters={"type": "preference"}  # 可以是 preference / knowledge / pattern
    )
    
    # 3. 注入 System Prompt（作为"用户画像"段）
    user_profile = self._format_memories(user_memories)
    
    return base_context, user_profile
```

**记忆类型示例：**

| 记忆类型 | 内容 | 更新时机 |
|----------|------|----------|
| 偏好 | "用户喜欢简洁回答，不要代码块" | 用户显式反馈或隐式行为（跳过长回答） |
| 知识 | "用户是医生，熟悉医学术语" | 用户资料或对话中推断 |
| 历史模式 | "上次类似任务用了 browser→file_op 组合" | 成功任务后自动提取 |
| 禁忌 | "用户拒绝过直接发邮件" | 用户显式拒绝后记录 |

### 用户鉴权

**在 Tool Layer 之前加一层权限门：**

```python
# harness/auth/acl.py
class ToolACL:
    """权限控制：什么角色能用什么工具"""
    
    def can_invoke(self, user_role: str, tool_name: str, input: dict) -> bool:
        # 角色-工具映射
        if user_role == "guest":
            return tool_name in {"browser", "http_request", "get_run_events"}
        
        if user_role == "operator":
            # 可以调用更多，但 file_op 只能读特定目录
            if tool_name == "file_op":
                return input.get("path", "").startswith("/safe/")
            return True
        
        if user_role == "admin":
            return True  # 全权限
        
        return False
    
    def can_confirm(self, user_role: str, confirmation: ConfirmationRequested) -> bool:
        # 谁有权确认危险操作
        if confirmation.risk_level == "critical":
            return user_role == "admin"
        return user_role in {"operator", "admin"}
```

**鉴权检查的位置：**

```
Agent 调用 tool
    ↓
Scheduler 接收 tool_call
    ↓
【新增】ACL 检查：用户角色是否允许？
    ├─ 拒绝 → 写入 GuardrailTriggered(guardrail_id="acl_denied")
    ↓
Tool Layer 正常处理
```

### 业务兼容

**通过 SKILL + 业务适配器实现：**

```python
# 业务 SKILL 示例：电商订单处理
class OrderSkill(Skill):
    """对外是单个工具，内部是多步业务逻辑"""
    
    def define(self) -> ToolDefinition:
        return ToolDefinition(
            name="process_order",
            description="处理电商订单（内部封装业务规则）",
            input_schema={"order_id": "string", "action": "enum[confirm, cancel, refund]"},
            # ... 内部调用多个底层工具，但 Agent 只看到这一个
        )
    
    async def execute(self, input: dict):
        # 内部业务规则（FSM 可以在这里）
        order = await self.call_tool("query_db", {"table": "orders", "id": input["order_id"]})
        
        if order.status == "paid" and input["action"] == "refund":
            # 业务规则：已付款才能退款
            await self.call_tool("initiate_refund", {"order_id": order.id})
            await self.call_tool("send_notification", {"user_id": order.user_id, "msg": "退款已发起"})
            return {"status": "refund_initiated"}
        
        # ...
```

---

## 5. 声明式事件前置条件（`depends_on`）——V0.6+ 强化

### 概念

工具可以声明"我需要哪些事件已经发生"，系统在 `GuardrailRunner` 中自动校验——**不依赖 Agent 配合，不写在工具代码里**。

### 模型

```python
class DependencyConstraint(BaseModel):
    event_type: str                    # 必须存在的事件类型（匹配 EventType.value）
    payload_filter: dict[str, Any]     # 可选：过滤 payload 字段
    message: str = ""                  # 检查失败时的提示

class ToolDefinition(BaseModel):
    ...
    depends_on: list[DependencyConstraint] = []
```

### 执行链路

```
GuardrailRunner.run()
  ├─ SchemaGuardrail                     ✅ 硬编码
  ├─ _auto_check_depends_on()            ✅ 新增，自动运行
  │    └─ DependencyGuardrail.check()
  │         └─ 读 tool_def.depends_on
  │              └─ 扫描 Event Store 匹配 (event_type + payload_filter)
  └─ tool_def.guardrails[]               ✅ 已有路径
```

- **两条路径并存**：`depends_on`（推荐，类型安全）和 `config["required_events"]`（向后兼容）
- `depends_on` 非空时，`required_events` 被忽略

### 设计原则

1. **声明式**：工具注册时一次性声明依赖，零运行时检查代码
2. **基于事件溯源**：唯一可信状态 = 事件流，不维护独立状态表
3. **受信组件强制**：`GuardrailRunner` 自动执行，Agent 无法绕过/感知
4. **与编排工具正交**：`orchestrate` 的 `depends_on=[RunStarted]` 只在入口检查一次，内部步骤由 Orchestrator 代码保证顺序

| 你的想法 | 建议优先级 | 实现位置 | 关键决策 |
|----------|-----------|----------|----------|
| 监控 + 反馈 | P0（现在就能做） | 独立受信组件，监听 Event Store | 不通过 Tool，通过 System Prompt 注入反馈 |
| 记忆压缩优化 | P1（V0.5+） | Context Manager | 改为结构化摘要（EpisodeSummary），优于当前纯文本摘要 |
| 用户角色/鉴权 | P1（V1.0） | 新增 Multi-tenancy 层 | 语义记忆按用户隔离，权限在 Tool Layer 前拦截 |
| 业务兼容 | P2（V1.0） | 业务 SKILL + 适配器 | 封装业务规则为 SKILL，Agent 无感知 |

**现在就能开始做的（V0.5 已完成基础上）：**

1. **监控组件原型**：写一个独立脚本通过 Event Store `on_append` 回调或独立连接监听，计算实时指标
2. **反馈注入实验**：在 Scheduler 的 THINK 前注入一条反馈，观察 Agent 行为变化

这些可以并行推进，不影响 V0.5 的已有功能。

---

## 6. Planner-Executor + DAG 执行引擎（V0.7）

### 架构动机

当前 `AgentLoopScheduler` 的串行 think→act→observe 循环有三个核心瓶颈：

1. **LLM 认知负荷过重**: 每轮 think 既要规划步骤序列，又要选择具体工具和参数
2. **串行执行**: LLM 输出的多个工具逐一 `await`，无法利用 asyncio 并行能力
3. **多轮失忆**: 进度状态完全依赖 LLM 上下文中的事件流折叠结果，压缩或截断后会幻觉

### 架构变更

```
旧循环:                             新循环:
                                    ┌─────────────┐
                                    │  Planner     │ ← LLM 生成 JSON Plan
                                    │  (非受信)     │   含 DAG 依赖关系
                                    └──────┬──────┘
                                           ↓ Plan
                                    ┌─────────────┐
think → act(串行) → observe        │  DagExecutor │ ← 拓扑排序，同层并行
  ↑ 每轮 1 个 think                │  (受信)      │   完整 Tool Layer 安全
  ↑ LLM 既要规划又要执行            └──────┬──────┘
                                           ↓ Results
                                    ┌─────────────┐
                                    │  Planner     │ ← Revise: 继续/修/终止
                                    │  (非受信)     │   系统注入 DAG 状态摘要
                                    └─────────────┘
```

### 受信边界

| 组件 | 受信 | 职责 | 约束方式 |
|------|------|------|----------|
| Planner | ❌ 非受信 | 调 LLM 生成/修订 JSON Plan | Plan 经受信组件校验后才执行 |
| DagExecutor | ✅ 受信 | 拓扑执行、并行调度、上游结果摘要化 | 完整 Tool Layer 流程 |
| PlanGuardrail | ✅ 受信 | Schema 校验、依赖无环、危险组合检测 | 独立于 Planner，不可绕过 |

### 核心数据流

```
Planner.plan(intent, state)
  → LLM 返回 JSON Plan
  → PlanGuardrail 校验（工具存在 / schema / 无环 / 危险组合）
  → 写入 PlanCreated 事件

DagExecutor.execute(run_id, plan)
  → topological_sort() 分层 [层0, 层1, ...]
  → 逐层 asyncio.gather() 并行执行
  → 每步写 DagStepStarted / DagStepCompleted / DagStepFailed
  → 上游 output 通过 upstream_selectors 提取摘要后合并到下游 input
  → 写入 PlanCompleted / PlanFailed

Planner.revise(plan, results, system_state)
  → 系统注入不可压缩 DAG 状态摘要
  → LLM 决定: 继续（修订剩余步骤）/ 完成（写 RunCompleted）/ 终止（写 RunFailed）
```

### DAG 拓扑执行

```
         s1    s2             ← 层0: 并行
          \   /
           s3                 ← 层1: 等 s1+s2 完成
          /
         s4                   ← 层2: 等 s3 完成

分层: [[s1, s2], [s3], [s4]]
每层内 gather() 并行执行
```

### 风险管理对照

| 风险 | 缓解方案 | 实现位置 |
|------|----------|----------|
| Plan 解析格式异常 | PlanParser 自动重试 2 次 + 降级旧串行 | `planner.py` |
| 上游结果膨胀 | `upstream_selectors` 字段路径提取 + 截断 500 chars | `dag_executor.py` |
| Revise 时 LLM 失忆 | 受信组件注入 `【系统状态 - 不可折叠】` 块 | `dag_executor.py` → planner |
| 动态条件分支 | `DagPlan.dynamic: true` 退化为逐层串行 | `scheduler.py` |
| 危险组合漏检 | PlanGuardrail 检测 `dangerous_with` | `planner.py` |
| 并行超限 | PlanGuardrail 检测 `max_parallel` | `planner.py` |
| 事件 fold 规则 | 按白名单分级（不可 fold / 摘要化 / 可跳过） | `fold.py` |