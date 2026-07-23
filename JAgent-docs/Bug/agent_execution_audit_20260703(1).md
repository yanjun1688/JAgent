# JAgent Agent 执行深度审计报告

> **审计日期**: 2026-07-03
> **审计范围**: Agent Loop (L3) / Planner-Executor (V0.7) / Context Manager / LLM Client / 前端 ChatDrawer
> **审计人**: AgentX (deepseek-v4-pro)
> **重点**: Agent 多轮对话体验 & 执行鲁棒性

---

## 执行摘要

JAgent 的架构设计方向正确——事件溯源、受信/非受信分离、Plan-Execute-Revise 模式都是经过验证的工程范式。但在**多轮对话**这个"好用的必要条件"上存在根本性缺失：**当前系统没有多轮对话概念**。每次用户输入创建一个全新的、独立的 Run，前后两次输入之间共享零上下文。这是导致"不好用"的核心根因。

此外，V0.7 Planner-Executor 路径存在多个已知但未完全修复的协议缺口（变量解析、状态传播、确认死循环），Planner Fallback 路径（串行 AgentLoopScheduler）在多轮场景下又面临 LLM 上下文膨胀和反馈失效问题。

下面按 优点 / 缺点 / 根因 → 优化方向 展开。

---

## 一、架构优点

### 1.1 事件溯源（Event Sourcing）+ Append-Only 设计 ✅

```
所有状态变更 → Event → Event Store (Append-Only)
任意时刻状态 = fold_events(event_stream)  ← 纯函数，确定性
```

| 优点 | 代码证据 |
|------|----------|
| 状态可审计、可重放 | `fold_events()` [harness/core/fold.py:67-273] 从事件流折叠出 RunState，无副作用 |
| 断点续传 | `ContextManager.try_checkpoint()` 每 N 轮写入 checkpoint [harness/core/context_manager.py:167] |
| 幂等安全 | `IdempotencyKeyGenerator` 自动计算，Tool Layer 写入前查重 [harness/tools/idempotency.py] |

### 1.2 受信/非受信边界清晰 ✅

| 组件 | 受信? | 职责 | 关键约束 |
|------|-------|------|----------|
| `ToolExecutor` | ✅ | 8-step 执行管线 | Schema→Idem→Guardrail→Confirm→Sandbox [harness/tools/executor.py:98-290] |
| `GuardrailRunner` | ✅ | 前置安全校验 | 不依赖 Agent 配合，强制拦截 [harness/tools/guardrails.py:255-305] |
| `DagExecutor` | ✅ | 拓扑排序 + 并行执行 | 变量解析在受信侧完成 [harness/core/dag_executor.py] |
| `Planner` | ❌ | LLM 生成 Plan | 输出经 `PlanGuardrail` 校验后执行 [harness/core/planner.py:184-376] |
| `AgentKernel` | ❌ | LLM 决策 | 输出仅为"建议"，系统决定是否执行 |

### 1.3 上下文压缩（ContextManager）完备 ✅

- 双层阈值：80% 正常压缩 / 90% 紧急压缩 [harness/core/context_manager.py:85-123]
- 压缩输出为结构化的 `EpisodeSummary`（key_decisions, tools_used, key_findings, errors）[harness/models/events.py:123-131]
- Plan 边界对齐：压缩时不破坏当前 Plan 的完整性 [harness/core/context_manager.py:98-112]
- 压缩后自动注入 System Prompt 作为"Previous context summary" [harness/core/agent_kernel.py:146-158]

### 1.4 监控反馈（RunMonitor）设计合理 ✅

- 实时事件驱动（EventStore.on_append 回调）[harness/monitoring/run_monitor.py:70]
- 异常检测维度：连续失败、重复调用、token 超量 [harness/monitoring/run_monitor.py:82-230]
- 结构化 Feedback：含 category/tool/error_type/suggestion/expires_at_seq [harness/models/events.py:157-178]
- 支持 Operator 手动反馈注入 [harness/api/routes.py:188-234]

### 1.5 降级回退机制 ✅

当 Planner 生成 Plan 全失败后，自动降级到串行 `AgentLoopScheduler` [harness/core/scheduler/plan.py:308-320]：
```python
# scheduler/plan.py:308
plan = await self.planner.plan(intent, state, feedback=feedback_text)
if plan is not None:
    return plan
# → Fallback to AgentLoopScheduler
```

---

## 二、核心缺陷

### 🔴 P0: 多轮对话完全缺失

**这是"不好用"的根本原因。**

当前代码中 `ChatDrawer.handleSubmit()` 的处理流程 [frontend/src/components/ChatDrawer.tsx:116-142]：

```typescript
async function handleSubmit() {
  const text = input.trim()
  setLastUserMessage(text)
  // 每次提交创建全新 Run
  const { run_id } = await createRun(text)
  setActiveRunId(run_id)
  // WebSocket 订阅这个 Run 的事件流
  // Run 完成后显示 finalAnswer
}
```

然后 `createRun` [harness/api/routes.py:88-99]：

```python
run_id = str(uuid.uuid4())[:8]  # ← 全新 Run ID，与之前的 Run 零关联
await api.store.append_event(run_id, EventType.RUN_STARTED, ...)
await api.start_run(run_id, body.intent)
```

**后果**：

| 用户行为 | 实际效果 | 期望效果 |
|----------|----------|----------|
| "帮我查一下东京天气" → "用中文总结" | Run#1 查天气完成。Run#2 收到 `intent="用中文总结"` → **不知道要总结什么**，因为没有 Run#1 的上下文 | Run#2 知道 Run#1 的输出，正确总结 |
| "今天有什么新闻？" → "详细说第一条" | Run#2 完全不知道"第一条"是什么 | 保留对话历史，能引用前一轮结果 |
| 连续追问 3 轮 | 3 个完全独立的 Run，Agent 每次都从零开始 | 一个连续的对话线程 |

**根因**：架构中只有 `Run`（单次任务执行）的概念，没有 `Session`/`Conversation`（多轮对话线程）的概念。`Run` 是执行单元，不是对话单元。

---

### 🔴 P1: Planner-Executor 路径有多个已知协议缺口

上一轮审查 `planner_executor_gaps.md` [JAgent-docs/reports/planner_executor_gaps.md] 识别了 6 个协议缺口，但**修复状态不明**：

| 缺口 | 描述 | 影响 | 在代码中是否已修复? |
|------|------|------|---------------------|
| **A: 变量解析** | `upstream_outputs()` key 命名不一致 (`s1_result` vs `s1`) | `$s1.body.url` 永远不被解析 | `dag_vars.py` 已独立出来且 key 使用裸 `step_id`，**推测已修复** |
| **B: 状态传播** | `CONFIRMATION_NEEDED` 在 DAG 路径被折叠为 `error` | 触发无限 revise 循环 | `StepResult` 已有 `CONFIRMATION_NEEDED` 状态 [harness/core/dag_types.py:10-14]，**推测已修复** |
| **C: 监控控制** | Monitor 只输出文本反馈，无强制控制能力 | 无限循环中 Monitor 无法熔断 | `RunCommandType` **未实现**（搜索为 0 hits） |
| **D: Guardrail 顺序** | Guardrail 在变量解析后的顺序有历史 bug | 变量未解析 → SchemaGuardrail 误拦 | 当前顺序正确（先解析后执行） |
| **E: Revise 上下文** | `build_dag_status_text` 缺少 step ID 保留/输出值列表 | Planner 重复生成已完成的步骤 | 代码中 `completed_step_ids` 已传递给 `guardrail.validate` [harness/core/planner.py:311] |
| **F: 监控 DAG 失效** | Monitor 未监听 `DAG_STEP_FAILED` 事件 | DAG 路径下 `_consecutive_failures` 始终为 0 | **已在代码中修复** [harness/monitoring/run_monitor.py:88] |

**关键确认**：Monitor 已监听 `DAG_STEP_FAILED`（`run_monitor.py:88` 三事件合并行），但 `RunCommand` 硬控制机制**完全未实现**——Monitor 仍然只能"建议"，不能"强制"。

---

### 🟡 P2: 串行 Fallback 路径在多轮场景下的问题

当 Planner 失败时降级到 `AgentLoopScheduler` + `_FallbackKernel`，这个路径：

1. **使用纯文本指令格式**（THOUGHT/TOOL/ARGS/ANSWER/`<STOP>`），而非 OpenAI Function Calling [harness/core/scheduler/fallback_kernel.py:30-71]
2. **无 OpenAI tools API 的结构化 tool_calls**，靠正则解析 LLM 输出 [harness/core/agent_kernel.py:19-65]
3. **解析脆弱**：`_ARGS_GREEDY_RE` 在工具输出包含 JSON 时容易错位

```python
# agent_kernel.py:32-42
_ARGS_GREEDY_RE = re.compile(r"ARGS:\s*(\{.*\})", re.DOTALL)
def _parse_segment(segment: str) -> tuple[str | None, dict[str, Any]]:
    seg = segment.strip()
    name_end = seg.find("\n")
    tool_name = seg[:name_end].strip() if name_end >= 0 else seg.strip()
    args_match = _ARGS_GREEDY_RE.search(seg)
    # ...
```

**多轮场景下这个路径的问题更严重**：没有 tool_calls 的结构化输出意味着 LLM 更容易偏离指令格式（尤其是上下文中已经有大量工具调用结果后）。

---

### 🟡 P3: think() 的上下文窗口仅保留最近 5 轮

在 `LLMAgentKernel.think()` [harness/core/agent_kernel.py:132-171]：

```python
window = 5  # 默认只取最近 5 个 thought + 5 个 result
timeline: list[tuple[str, Any]] = []
for t in state.thought_history[-window:]:
    timeline.append(("thought", t))
for tr in state.tool_results[-window:]:
    timeline.append(("result", tr))
```

**问题**：这是一个硬编码的滑动窗口，在压缩发生前始终只传最近 5 轮。对于需要引用早期结果的复杂任务，LLM 会在第 6 轮"忘记"第 1 轮的输出。`ContextManager` 提供压缩后在 `state.summary` 中有摘要，但摘要的保真度远低于原始结果。

---

### 🟡 P4: 前端无对话历史持久化

| 缺失能力 | 影响 |
|----------|------|
| 无 Session ID 概念 | 用户切换页面后无法恢复对话 |
| 无客户端对话历史存储 | 刷新页面后所有历史 Run 需要手动查找 |
| 无对话线程列表 UI | 用户看不到"我之前的对话"，只能看到 Run 列表 |
| ChatDrawer 强制 1:1 绑定 Run | 一个聊天窗口 = 一个 Run，不能在同一窗口中继续对话 |

`RunList.tsx` 有历史列表，但它是 Run 级别的列表——不是对话级别的。用户无法在一个"对话"中看到"我发起了 3 个连续 Run"。

---

### 🟡 P5: System Prompt 在多轮场景下缺乏"持久指令"

`get_prompt(AgentPhase.SERIAL_THINK, intent=intent, tool_list=tool_list)` [harness/core/system_prompt.py:139-166] 每次 think 都重新构建 System Prompt，但：

- 没有"角色记忆"（persistent instructions across runs）
- 没有"对话级 System Prompt"（一次对话中保持不变的系统指令）
- 第 N 轮的 System Prompt 和第 1 轮完全一致——没有"你已经完成了 X，现在继续 Y"的渐进式指令

---

### 🟢 P6: 几个较小的但影响体验的问题

| 问题 | 位置 | 影响 |
|------|------|------|
| Fallback 路径 `_generate_stop_summary` 仅在串行降级时调用 | [agent_kernel.py:79-98] | V0.7 Planner 路径有自己的 `_generate_answer` |
| `AgentThoughtPayload.tool_calls` 字段只在串行路径被填充为工具名列表 | [loop.py:85] | Planner 路径中始终为 `None`（写入的是 `plan` 字符串） |
| 前端 `ChatDrawer` 的输入框在 Run running 时 disabled | [ChatDrawer.tsx:241] | 用户必须等 Run 完成才能发下一条，无"中断并追加"的能力 |
| 确认卡片的 `risk_level` 前端类型读取有问题 | [ChatDrawer.tsx:82-85] | `pc.payload?.risk_level` 应为 `pc.risk_level`（非嵌套） |

---

## 三、根因分析

### 3.1 为什么没有多轮对话？

1. **架构哲学**："Run" 是被设计为"任务执行单元"而非"对话线程"的。从 `events.py` 可以看到，整个事件模型围绕 `run_id` 构建——`RunStarted → AgentThought → ToolCalled → ... → RunCompleted`。没有 `ConversationStarted` / `MessageAdded` 这样对话级事件。

2. **前端成本低优先**：`ChatDrawer` 是在 V0.3 阶段作为"快速演示"加的——一个输入框 + 一个 Run 显示。没有演进为真正的聊天 UI（多轮消息、历史管理、对话树）。

3. **后端无 Session 概念**：`EventStore` 只有 `run_id` 维度的查询——`get_events(run_id)`、`list_runs()`。无法跨 Run 查询"某个对话的所有 Run"。

### 3.2 为什么 Planner-Executor 路径有协议缺口？

1. **迭代速度 vs 协议稳定性**：V0.7 通过 5 个 Phase 快速迭代，每次 Phase 修复上一阶段发现的问题，但没有做完整的跨层协议形式化。

2. **审查盲区**：上一轮的 `architecture_issues.md` 是组件级审查（检查崩溃/数据损坏），`planner_executor_gaps.md` 是跨层审查（检查数据流/控制流协议）。前者先做，后者后做，协议缺口在组件级审查中不可见。

3. **测试覆盖率偏差**：341 项测试全通过，但没有针对"多轮对话"场景的端到端测试。所有测试都是单 Run 的。

---

## 四、优化方向与路线图

### 4.1 🔴 Phase 1: 实现多轮对话（最关键）

这是需求最强烈、改善最大的方向。

#### 4.1.1 后端：引入 Conversation / Session 概念

```
新增概念层级:
  Conversation (多轮对话)
    ├─ Run #1 (第 1 轮任务执行)
    ├─ Run #2 (第 2 轮，可引用 Run #1 的上下文)
    └─ Run #N

新增事件:
  ConversationStarted    — 对话创建
  ConversationMessage    — 用户消息（关联到对话但每个消息触发一个 Run）
  ConversationEnded      — 对话结束
```

**关键设计决策**：每个 Run 仍然是独立的执行单元，但在 `RunStarted.intent` 的构建阶段注入对话历史摘要。

```python
# 伪代码: create_run 增强版
async def create_run_with_context(conversation_id: str, message: str):
    history = await get_conversation_summary(conversation_id)
    intent = f"Previous conversation:\n{history}\n\nNew request: {message}"
    return await create_run(intent)
```

**变更范围**：

| 文件 | 变更 |
|------|------|
| 新增 `harness/models/conversation.py` | Conversation / Message 数据模型 |
| `harness/storage/event_store.py` | 新增 `conversations` 表 / 查询方法 |
| `harness/api/routes.py` | 新增 `POST /api/v1/conversations` / `POST /api/v1/conversations/{id}/messages` |
| `harness/api/deps.py` | `start_run()` 支持注入对话历史 |
| `harness/core/system_prompt.py` | 新增 `CONVERSATION_CONTEXT` prompt phase |
| `frontend/src/components/ChatDrawer.tsx` | 重写为真正的多轮聊天 UI |

#### 4.1.2 前端：ChatDrawer 重写为 ConversationDrawer

```
当前 (ChatDrawer):
  ┌─────────────────┐
  │ Run abc12345     │ ← 仅显示一个 Run
  │                  │
  │ [用户消息]       │
  │ [Agent 思考]     │
  │ [最终回答]       │
  ├─────────────────┤
  │ [输入框]  [Send] │ ← 提交后创建新 Run，清空当前
  └─────────────────┘

目标 (ConversationDrawer):
  ┌─────────────────┐
  │ 对话: 帮我分析XX │ ← 对话标题
  ├─────────────────┤
  │ 👤 帮我查天气   │ ← 第 1 轮
  │ 🤖 东京今天25°C │
  │ 👤 用中文总结   │ ← 第 2 轮 (能引用上一轮上下文!)
  │ 🤖 根据之前的查 │
  │    询结果...    │
  │ 👤 那明天呢？   │ ← 第 3 轮
  │ 🤖 (查询中...)  │
  ├─────────────────┤
  │ [输入框]  [Send] │
  └─────────────────┘
```

**关键交互变化**：

- 每个对话有一个 `conversationId`
- 用户连续发消息在同一对话中
- 每个消息触发一个 Run（后台执行）
- Agent 回答自动关联回对话
- 对话历史可折叠、可搜索

### 4.2 🟡 Phase 2: 修复 Planner-Executor 协议缺口

#### 4.2.1 实现 RunMonitor 硬控制（缺口 C）

当前 Monitor 只能写"建议"文本，无法强制执行。需要新增 `RunCommand` 事件：

```python
# 新增事件类型
EventType.RUN_COMMAND = "RunCommand"

class RunCommandPayload(BaseModel):
    command: Literal["hard_abort", "soft_abort", "pause", "skip_tool", "lower_parallel"]
    reason: str
    affected_tool: str | None = None
```

插入点：`PlanningExecutorScheduler._plan_execute_revise_loop` 的每次循环开始处检查是否有待执行的 command。

#### 4.2.2 修复 Fallback 路径使用 OpenAI tools API

当前 `_FallbackKernel` [harness/core/scheduler/fallback_kernel.py] 使用文本指令格式（TOOL:/ARGS: 正则解析），应改为使用 OpenAI tools API（和 `LLMAgentKernel` 一致）：

```python
# fallback_kernel.py 应改为:
response = await self.client.chat(messages, tools=build_tool_schemas(tool_defs))
# 然后复用 LLMClient 的 tool_calls 解析逻辑
```

### 4.3 🟡 Phase 3: 提升上下文管理

#### 4.3.1 动态窗口大小

将硬编码的 `window = 5` 改为基于 token 估算的动态窗口 [harness/core/agent_kernel.py:132]：

```python
# 替代硬编码 5
max_context_tokens = 8000  # 为 conversation 保留的空间
window = self._compute_dynamic_window(state, max_context_tokens)
```

#### 4.3.2 工具结果智能截断

当前 `tool_results` 中的 `output` 是完整的原始结果，可能很大。在传给 LLM 前应按工具类型策略截断：

- `http_request` → 只保留前 2000 chars body + status code
- `browser` → 只保留前 1000 chars text content
- `file_op` → 只保留前 500 chars 内容

### 4.4 🟢 Phase 4: 前端体验优化

| 优化项 | 优先级 | 说明 |
|--------|--------|------|
| Run 运行时允许输入下一条消息（排队执行） | 高 | 当前必须等 Run 完成才能发下一条 |
| 对话历史侧边栏 | 中 | 类似 ChatGPT 的对话列表 |
| Agent 思考过程折叠动画 | 中 | 当前是静态展开的 |
| 工具调用实时状态卡片 | 中 | 显示"正在搜索..."而非仅显示日志 |
| Markdown 渲染最终回答 | 低 | 当前是纯文本输出 |

### 4.5 🟢 Phase 5: 持久化对话记忆

```python
# 概念: 对话级向量记忆
class ConversationMemory:
    """跨 Run 的持久化记忆"""
    conversation_id: str
    key_facts: list[str]       # "用户是日本人，需要日语回复"
    tool_results_cache: dict   # 避免重复查询
    preferences: dict          # "用户喜欢简短回答"
```

---

## 五、优先级矩阵

| 优先级 | 方向 | 改善幅度 | 实现成本 | 风险 |
|--------|------|----------|----------|------|
| 🔴 P0 | 多轮对话 | ★★★★★ | 高（需后端+前端全栈改造） | 中（架构变更大） |
| 🟡 P1 | 修复 Planner-Executor 缺口 | ★★★★☆ | 中 | 低（改动局部） |
| 🟡 P2 | Fallback 路径使用 tools API | ★★★☆☆ | 低 | 低 |
| 🟡 P3 | 动态上下文窗口 | ★★★☆☆ | 低 | 低 |
| 🟢 P4 | 前端体验优化 | ★★★☆☆ | 中 | 低 |
| 🟢 P5 | 对话记忆 | ★★☆☆☆ | 高 | 中 |

---

## 六、建议的下一步

### 立即执行

1. **修复 P0: 实现最小可行多轮对话**（后端先做）
   - 新增 `Conversation` 模型 + `conversations` 表
   - `create_run` 改为接受可选 `conversation_id`
   - 如果有 `conversation_id`，在 System Prompt 中注入前几轮的摘要
   - 成本估算：约 3-5 个文件修改 + 1-2 个新文件

2. **确认 Planner-Executor 缺口的修复状态**
   - 对照 `planner_executor_gaps.md` 逐项检查代码
   - 跑一轮完整功能测试确认

### 短期执行

3. 将 `_FallbackKernel` 改为使用 OpenAI tools API
4. 将硬编码 `window = 5` 改为动态 token 估算

### 中期规划

5. 前端 ChatDrawer 重写为 ConversationDrawer
6. RunMonitor 硬控制机制

---

## 七、附录

### A. 审查覆盖的文件清单

| 文件 | 功用 | 审查重点 |
|------|------|----------|
| `harness/core/agent_kernel.py` | LLMAgentKernel + MockAgentKernel | 多轮上下文窗口 |
| `harness/core/scheduler/loop.py` | AgentLoopScheduler 串行循环 | think→act→observe 流程 |
| `harness/core/scheduler/plan.py` | PlanningExecutorScheduler | Plan→Execute→Revise 流程 |
| `harness/core/scheduler/base.py` | BaseScheduler | 生命周期管理 |
| `harness/core/scheduler/fallback_kernel.py` | Fallback 降级 Kerenl | 解析脆弱性 |
| `harness/core/context_manager.py` | ContextManager | 压缩/checkpoint |
| `harness/core/system_prompt.py` | System Prompt 注册表 | 各 phase prompt |
| `harness/core/llm_client.py` | LLMClient | OpenAI/DeepSeek 调用 |
| `harness/core/fold.py` | fold_events | 事件折叠 |
| `harness/core/planner.py` | Planner + PlanGuardrail | Plan 生成与校验 |
| `harness/core/dag_executor.py` | DagExecutor | 并行执行 |
| `harness/core/dag_types.py` | StepResult 类型 | 状态传播 |
| `harness/core/dag_vars.py` | 变量解析 | $ref 解析 |
| `harness/tools/executor.py` | ToolExecutor | 8-step 执行 |
| `harness/tools/guardrails.py` | GuardrailRunner | 安全校验 |
| `harness/tools/retry.py` | RetryRunner | 重试策略 |
| `harness/tools/semantic.py` | SemanticEvaluator | SOFT_ERROR 判定 |
| `harness/monitoring/run_monitor.py` | RunMonitor | 异常检测 |
| `harness/models/events.py` | 事件模型 | payload 一致性 |
| `harness/models/tools.py` | ToolDefinition | 工具契约 |
| `harness/api/routes.py` | REST API | Run CRUD |
| `harness/api/ws.py` | WebSocket | 实时事件推送 |
| `harness/api/deps.py` | HarnessAPI | 依赖容器 |
| `harness/api/serve.py` | 生产入口 | 组件装配 |
| `frontend/src/components/ChatDrawer.tsx` | 聊天 UI | 多轮交互 |
| `frontend/src/pages/OpsChatView.tsx` | Ops 视图 | 布局 |

### B. 引用的已知技术债务

- `TODO_v2.1.md` 底部 Known Technical Debt:
  - `fold.py`: `tool_calls` 和 `feedbacks` 未在压缩时修剪
  - `event_store.py`: `_seq_locks` TTL 淘汰
  - `guardrails.py`: `RateLimitGuardrail._call_history` 类级字典无清理

### C. 上一轮审查报告

- `holistic_code_review_20260611.md` — 全量 Code Review
- `planner_executor_gaps.md` — Planner-Executor 协议缺口分析
- `architecture_issues.md` — 架构问题修复记录
- `confirmation_timeout_race.md` — 确认超时竞态

---

*本报告基于 26 个源代码文件的全文审查生成。所有断言均有代码位置引用支持。*
