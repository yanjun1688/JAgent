# Agent Runtime 审计 Review — 2026-07-23

> **审计来源**: `data/logs/harness.log` (Run: `9f4c5fce`, 用户意图: "帮我下载昨天腾讯、阿里、字节三家公司的财报")
> **审查范围**: Planner → DAG Executor → Tool Layer → Monitor → Answer 全链路
> **审计方法**: 日志回放 + 源码逐行对照
> **状态**: **部分修复** — P0-02 / P0-01(短期) / P1-01 已完成；P0-03 已设计并分阶段实施中（见下方）；P0-01(长期) / P0-04 / P1-02 未做

---

## 发现汇总

| ID | 严重级别 | 问题 | 状态 |
|----|---------|------|------|
| P0-01 | P0 | Planner 未使用 MCP Runtime Discovery 的工具 Schema 做规划 | 🟡 短期已做 / 🔴 长期未做 |
| P0-02 | P0 | Soft Error 被当成 Success 继续传播（Transport 层与 Business 层耦合） | 🟢 已修复 |
| P0-03 | P0 | DAG 未利用失败信息阻断依赖链（SOFT_ERROR 被当 "done" 传递） | 🟡 已设计，分阶段实施中（门控 `step_normal`，见方案） |
| P0-04 | P0 | Revise 太晚 — 当前是 Batch Workflow 而非 Agent Loop | 🔴 未做 |
| P1-01 | P1 | Monitor Feedback 利用率低 — 有 Self-Healing 骨架但未闭环 | 🟢 已修复 |
| P1-02 | P1 | Answer Prompt 与 Runtime 耦合过重 — 全量 Tool Result 注入 | 🔴 未做 |

---

## P0-01: Planner 未使用 Runtime Discovery 的真实 Tool Schema 做规划

### 日志证据

```
17:08:13 [APP] Connected MCP server 'playwright': 24 tools
17:08:13 [APP] MCP discovery: 33 tools available via mcp_call
```

Planner 看到的 Available Tools 列表中，MCP 工具以 `playwright/browser_snapshot(...)` 格式列出（`app.py:73`）:
```
  - playwright/browser_snapshot(target, filename, depth, boxes): Capture accessibility snapshot...
```

但 Planner 输出的是:
```json
{"id": "s4", "tool": "mcp_call", "input": {"tool_name": "browser_snapshot"}, "depends_on": ["s1"]}
```

LLM 生成的 `tool_name` 是 `browser_snapshot` 而非 `playwright/browser_snapshot`，导致:
```
17:09:15 [AGENT] mcp_call memory/browser_snapshot args={}
17:09:15 [AGENT] mcp_call memory/browser_snapshot -> success (1 items)
```
MCP 返回 `-32602: Tool browser_snapshot not found`，但被当作 `success`（见 P0-02）。

### 源码定位

| 文件 | 行号 | 说明 |
|------|------|------|
| `harness/tools/mcp_call.py:13-48` | `MCP_CALL_DEF` | 单一 `mcp_call` 工具定义，MCP 子工具仅作为 description 中的文本 |
| `harness/api/app.py:64-79` | MCP discovery | 将 MCP 工具列表拼入 `MCP_CALL_DEF.description` 作为文本（非结构化 Schema） |
| `harness/core/planner.py:410-438` | `_build_tool_descriptions()` | 从 `registry.list_tool_defs()` 实时拉取 — **架构正确** |
| `harness/core/planner.py:398-408` | `_build_plan_prompt()` | 调用 `_build_tool_descriptions()` 注入 `Available Tools` 段 |

### 根因分析

**架构问题链**:

1. MCP 工具通过 `mcp_manager._register_tools()` 注册时，`fn=None`，工具仅登记为 ToolDefinition 但无实际 handler（`mcp_manager.py:210-228`）
2. 实际调用路由统一走 `mcp_call` 工具，通过 `tool_name` 参数分派
3. LLM 在 Plan 阶段看到的是**非结构化的文本列表**（MCP tool names in description text），而非结构化的 JSON Schema
4. LLM 容易在 MCP tool name 上产生幻觉/截断（`browser_snapshot` vs `playwright/browser_snapshot`）

**对比正确架构**:

```
Runtime Discovery (MCP list_tools)
       ↓
生成 ToolDefinition (结构化 input_schema / output_schema / name)
       ↓
注册到 ToolRegistry（与本地工具同权）
       ↓
Planner Prompt 使用 build_tool_schemas() 生成 JSON Schema
       ↓
LLM 按结构化 Schema 选择工具
```

当前架构的 MCP 路径:

```
Runtime Discovery (MCP list_tools)
       ↓
拼入 MCP_CALL_DEF.description 文本 ← 信息丢失（非结构化）
       ↓
Planner Prompt 中作为 Available Tools 文本列表
       ↓
LLM 凭文本描述猜测 tool_name          ← 根因所在
       ↓
mcp_call_fn 传递 tool_name 到 MCP    ← 无前置校验
```

### 修复建议

1. **短期**: 在 `mcp_call_fn` 中增加 MCP tool name 前置校验 — 调用前先 `session.list_tools()` 确认 tool 存在，不存在则 return `{"success": False, "error": "Tool 'X' not found in server 'Y'. Available: [...]"}` 
2. **中期**: 在 `_build_tool_descriptions()` 中对 MCP 工具增加强调说明：`"tool_name" 必须使用完整的 "server_name/tool_name" 格式`
3. **长期**: 将 `auto_register_tools` 改为默认开启，MCP 工具作为一等公民注册到 ToolRegistry，Planner 直接看到结构化 Schema

### 修复记录 (2026-07-23) — 短期

**已实施**:

1. `harness/tools/mcp_manager.py:30` — `_MCPSession` 增加 `tool_names: list[str]` 字段，`connect_server()` 中缓存工具名列表
2. `harness/tools/mcp_manager.py:143` — 新增 `get_tool_names(server_name)` 方法
3. `harness/tools/mcp_call.py:93-104` — 调用 `call_tool()` 前做 tool name 校验：
   - 若 tool_name 不含 `/` 且 `server_name/tool_name` 在已知工具列表中 → 自动补全前缀
   - 若 tool_name 不在列表中 → 返回错误并列出可用工具

---

## P0-02: Soft Error 被当成 Success 继续传播（Transport vs Business 耦合）

### 日志证据

```
17:09:15 [AGENT] mcp_call memory/browser_snapshot -> success (1 items)
17:09:15 [GUARD] [sandbox] Completed in 15ms (retries=0)
17:09:15 [AGENT] [semantic] [step] s4 → mcp_call completed SUCCESS (15ms)
17:09:15 [AGENT] [step] s4 completed — {"success": true, "content": ["MCP error -32602: Tool browser_snapshot not found"]}
17:09:15 [GUARD] Written event @ seq=24: DagStepCompleted (run=9f4c5fce, 0ms)
```

**Runtime 判定**: `DAG_STEP_COMPLETED` with `ExecState.COMPLETED`
**实际业务**: MCP 返回 `-32602 Method not found` — 工具调用失败了

### 源码定位

| 文件 | 行号 | 说明 |
|------|------|------|
| `harness/tools/mcp_call.py:92-107` | `mcp_call_fn` | 只要 `session.call_tool()` 不抛异常，就返回 `{"success": True}` |
| `harness/tools/mcp_call.py:47` | `MCP_CALL_DEF` | `success_indicator=SuccessIndicator(field="success", op="eq", value=True)` |
| `harness/tools/semantic.py:28-29` | `SemanticEvaluator.evaluate()` | 当 `field="success"` 且 `output["success"] == True` → `SUCCESS` |
| `harness/tools/executor.py:261` | `ToolExecutor.execute()` | 调用 `SemanticEvaluator.evaluate()` 判定 result_type |
| `harness/core/dag_executor.py:199-202` | `_execute_layer()` | `raw.is_completed` → `ExecState.COMPLETED` → DAG 认为是成功 step |

### 根因分析

MCP 协议的 `CallToolResult` 有两种语义:

1. **Transport 成功**: 网络调用完成，MCP Server 返回了 result（HTTP 200 / stdio pipe OK）
2. **Business 成功**: 工具实际执行了有意义的操作并返回了预期数据

当前 `mcp_call_fn` 把 Transport 层的"没抛异常"等同于 Business 层的"工具调用成功"。

MCP `-32602` 错误是 MCP Server 返回的 `CallToolResult.isError=True` 的 response，`session.call_tool()` 会返回正常的 `CallToolResult` 对象（不抛异常），但其 `isError` 属性为 True。

**缺少的语义分层**:

```
当前:  transport OK → tool success → step completed
应有:  transport OK → MCP isError=True → tool SOFT_ERROR → step has_soft_error
```

### 修复建议

1. **立即**: `mcp_call_fn` 中检查 `result.isError`，若为 True 则返回 `{"success": False, "error": "MCP tool error: <content>"}` 
2. **中期**: 在 `ToolExecutionResult` 中增加 `transport_status` 和 `business_status` 分层:
   - `transport_status`: HTTP/stdio 层的连通性状态
   - `business_status`: 工具实际是否完成了业务目标
   - Planner/Executor 基于 `business_status` 做决策，而非 `transport_status`

### 修复记录 (2026-07-23)

**已实施**: `harness/tools/mcp_call.py:96` — 在 `session.call_tool()` 返回后增加 `result.isError` 检查（使用 `is True` 防御 MagicMock）:
```python
if getattr(result, "isError", None) is True:
    # 提取 error content 并返回 {"success": False, ...}
```
MCP `-32602` 等协议级错误不再被当作 Success 传播。

---

## P0-03: DAG 未利用失败信息阻断依赖链

### 日志证据

```
Layer 1 (s1, s2, s3):  全部 SOFT_ERROR  → 日志显示 "all 3 step(s) completed"
Layer 2 (s4, s5, s6):  全部 EXECUTED    → depends_on s1/s2/s3 (SOFT_ERROR)
Layer 3 (s7, s8):       全部 SOFT_ERROR  → depends_on s4/s5 ("MCP error -32602")
Layer 4 (s9, s10):      全部 IDEMPOTENT  → depends_on s7/s8 (SOFT_ERROR)
└─ 10/10 steps finished. Revise 才发现全部失败。
```

**预期行为**: Layer 1 全挂 → Layer 2 应 SKIP/BLOCKED，而非继续执行

### 源码定位

| 文件 | 行号 | 说明 |
|------|------|------|
| `harness/core/dag_types.py:74-77` | `should_not_rerun` | SOFT_ERROR → `should_not_rerun=True`，即"工具已执行过" |
| `harness/core/dag_types.py:84-85` | `is_done` | `COMPLETED`, `SOFT_ERROR`, `IDEMPOTENT` 都返回 True |
| `harness/core/dag_executor.py:199-268` | `_execute_layer()` | SOFT_ERROR 不计入 `any_failed`，layer 返回 True |
| `harness/core/scheduler/plan.py:410-445` | `_execute_plan()` | 仅在所有 layer 完成后才检查 `soft_error_sids` 并触发 Revise |

### 根因分析

`StepResult.is_done` 的语义是"工具已执行过，不应重新调度"。这个语义在设计上没问题 — 工具确实执行了。

**但缺失的是**: Executor 没有在每层完成后做快速失败判断。当前逻辑:

```
_execute_layer → any_failed=False → return True → 继续下一层
```

缺少:

```
_execute_layer → 检查上游依赖的 business 状态 → 若依赖全部 SOFT_ERROR/FAILED → SKIP/BLOCKED
```

`DagExecutor` 没有"依赖健康检查"机制。`_execute_step()` 中 `upstream` 字典只收集输出数据，不检查依赖步骤的 `exec_state`。

### 修复建议

1. 在 `_execute_step()` 增加依赖健康检查: 若 `depends_on` 的步骤全部 `is_done` 但 `is_completed=False`（即全是 SOFT_ERROR/FAILED），返回 `ExecState.SKIPPED`
2. 在 `_execute_layer()` 中增加快速失败路径: 当 layer 内步骤出现 SOFT_ERROR 时，检查是否应触发即时 Revise 而非继续下一层
3. `all_results` 增加 SKIPPED / BLOCKED 传播机制

### 解决方案（2026-08-07 拍板，分阶段实施）

> 依据 `Handover/completion_semantics_chain_redesign_handover_20260807.md` D3/D7/D9，本项已设计并纳入阶段 B/C 实施。

- **门控判据**：`step_normal`（纯函数 `(exec_state, probe) → bool`），**取代** `is_done`/`should_not_rerun`
  作为依赖健康检查的唯一依据。UNSUCCESSFUL（非 probe）与 FAILED 均算 `step_normal=False`。
- **阻断行为**：`_execute_step` 依赖健康检查——存在依赖 `step_normal=False` → 本步 `SKIPPED`，
  **不再向下游传递坏数据**（修复"Layer 1 全挂 → Layer 2 仍执行"）。
- **SKIPPED 落记录**：门控产生的 SKIPPED 写 `DagStepSkipped` 事件（D9），可观测可审计。
- **fail-safe 兜底**：probe 否定答案不阻断下游（D7）——下游消费不了会自身 UNSUCCESSFUL → 完成门拦截 → revise。
- **完成口径同步**：完成计数改用 `step_normal` 聚合（D5/D8），彻底消灭"Completed 3/3"假绿。

**验收**：Layer 1 全 UNSUCCESSFUL/FAILED → Layer 2 全部 SKIPPED，run 不完成，revise 修复。

---

## P0-04: Revise 太晚 — Batch Workflow 而非 Agent Loop

### 日志证据

完整执行时序:

```
T0: Plan (10 steps, 4 layers)
T1: Layer 1 execute → 3/3 SOFT_ERROR
T2: Layer 2 execute → 3/3 "completed" (实际 MCP 失败)
T3: Layer 3 execute → 2/2 SOFT_ERROR
T4: Layer 4 execute → 2/2 IDEMPOTENT (缓存命中)
T5: All layers done → Revise (LLM declares failed=true)
T6: Answer
```

**10 个 steps 全部跑完才触发第一次 Revise。** Layer 1 已经显示 browser 不可用，但 Layer 2-4 仍然跑完。

### 源码定位

| 文件 | 行号 | 说明 |
|------|------|------|
| `harness/core/dag_executor.py:59-115` | `DagExecutor.execute()` | 按 topological_sort 层层执行，无中途拦截 |
| `harness/core/dag_executor.py:199-268` | `_execute_layer()` | SOFT_ERROR 不改变 `any_failed`，layer 总是返回 True (line 267-268) |
| `harness/core/scheduler/plan.py:344-408` | `_execute_plan()` | 仅 `not ok`（硬失败）时触发中途 Revise |

### 根因分析

当前 `DagExecutor.execute()` 的设计是 **fire-and-forget**: 给一个 Plan，跑完所有 layer 再返回结果。这是 Batch 语义。

Agent 语义应为:

```
Layer1 → 判断 → 暂停/继续/Replan → Layer2 → ...
```

中间缺少一个 **LayerPostCheck** 环节来做:
1. 监控该层 business 成功率
2. 如 SOFT_ERROR 连续超过阈值 → 暂停执行、触发 replan
3. 如硬失败 → 立即停止并 revise

Monitor 已在实时检测（P0-01 中 Monitor 在 Layer 1 就发现 3 次连续失败并注入 Feedback），但 Executor 没有"听取" Monitor 的实时反馈。

### 修复建议

1. 每层 `_execute_layer` 完成后，增加 `_should_continue()` 检查:
   - 本层 SOFT_ERROR 率 > 阈值 → 暂停、触发 `planner.revise()`
   - 新的 feedback 出现 → 暂停、重新进入 Plan→Revise 循环
2. 将 Monitor 的实时反馈作为 DAG executor 的中断信号（callback/event 机制）
3. 在 `_plan_execute_revise_loop` 中改为逐层 Plan-Execute-Check 而非全量 Plan → Execute All → Revise

---

## P1-01: Monitor Feedback 利用率低 — Self-Healing 未闭环

### 日志证据

Monitor 表现良好:
```
17:09:15 [MONITOR] SOFT_ERROR anomaly threshold hit (consecutive=3)
17:09:15 [MONITOR] Injecting feedback ... priority=high ... affected_tool=browser
17:09:15 [GUARD] Written event @ seq=13: FeedbackInjected
```

```
## System Monitoring Feedback
!! [HIGH] Endpoint 'browser' (tool 'browser') failed 3 consecutive times
```

LLM Revise 的结果:
```json
{
  "intent": "获取腾讯、阿里巴巴和字节跳动三家公司最新发布的财务报告文件",
  "steps": [],
  "failed": true,
  "reason": "所有浏览器导航操作均失败（NotImplementedError）..."
}
```

**监控发现了问题，反馈注入了，但 Planner 的 Revise 只是声明"失败了"而不是尝试替代方案。**

`run_monitor.py:352-372` 的 `_generate_suggestion()` 已经内置了替代建议:
```python
("browser", "NotImplementedError"):
    "The browser tool is unavailable on this platform. Use 'http_request' for web requests.",
```

这个 suggestion 虽然构建了（`run_monitor.py:318` 传入），但最终没有被 LLM 采纳。

### 源码定位

| 文件 | 行号 | 说明 |
|------|------|------|
| `harness/monitoring/run_monitor.py:352-372` | `_generate_suggestion()` | 已内置 4 条替代建议 |
| `harness/monitoring/run_monitor.py:298-319` | `_check_and_inject_feedback()` | suggestion 写入 feedback_text |
| `harness/core/scheduler/plan.py:218-220` | `_get_feedback_text()` | 从 state.feedbacks 拉取 |
| `harness/core/planner.py:251-311` | `revise()` | 将 feedback 注入 REVISE prompt |

### 根因分析

1. **Feedback 格式**: `feedback_text` 是自由文本，LLM 容易在长 prompt 中忽略
2. **System Prompt 未强调**: REVISE prompt 中没有明确指令 "当 feedback 包含替代工具建议时，必须尝试替代方案"
3. **无强制机制**: Monitor 的 feedback 是建议性的，没有系统强制力 — LLM 可以选择忽视

### 修复建议

1. **结构化 Feedback 指令**: 在 REVISE prompt 中增加:
   ```
   ## Mandatory Instructions
   - If feedback suggests an alternative tool (e.g. "Use 'http_request' instead of 'browser'"),
     you MUST create steps using the suggested tool. You may not ignore this guidance.
   ```
2. **加重 Feedback 权重**: 将 Monitor 的高优 feedback 以 `<system-critical>` 标签包裹
3. **Executor 层面的强制 Self-Healing**: 当 Monitor 连续检测到同一模式的 failure，Executor 主动替换 tool 而非依赖 LLM 做判断

### 修复记录 (2026-07-23)

**已实施**: `harness/core/system_prompt.py:116-120` — REVISE prompt 中 `{feedback_section}` 前增加结构化指令:
```
## System Monitoring Feedback
If feedback below suggests an alternative tool or approach (e.g. 'Use http_request
instead of browser'), you MUST create steps using the suggested alternative
before declaring failure. Ignoring actionable feedback is not permitted.
```
优先级 2（`<system-critical>` 标签）和 3（Executor 层面强制）未在本次实施。

---

## P1-02: Answer Prompt 与 Runtime 耦合过重

### 日志证据

Answer 阶段的 user message 包含:
```
[Tool execution results]
## Step 1: browser (status: soft_error)
Input: {...}
Output: {...}
Error: ...
Duration: 125ms

## Step 2: browser (status: soft_error)
...
[6 个 step 的完整 input/output/error/duration]
...
[Feedback]
Endpoint 'browser' ... failed 3 consecutive times...
```

**6 个 Tool Result + Feedback 全部塞入 Answer Prompt。**

### 源码定位

| 文件 | 行号 | 说明 |
|------|------|------|
| `harness/core/planner.py:313-385` | `generate_answer()` | 遍历 `state.tool_results` 全部输出，构造 user message |
| `harness/core/scheduler/plan.py:468-484` | `_finalize_with_summary()` | 调用 `_generate_answer()` |

### 根因分析

`generate_answer()` 遍历了 **全部** `state.tool_results`（line 327-351），而不是生成一个 Execution Summary 抽象层。随着 step 数量增长，prompt 长度线性膨胀，且大量失败/冗余数据降低了 LLM 生成答案的质量。

### 修复建议

1. **增加 ExecutionSummary 中间层**: Executor 在 Plan 完成后生成结构化摘要:
   ```json
   {
     "total_steps": 10,
     "completed": 0,
     "soft_errors": 6,
     "failed": 0,
     "cached": 4,
     "key_failure": "Browser tool unavailable: NotImplementedError",
     "recommendation": "Use http_request instead"
   }
   ```
2. **Answer 只吃 Summary**: `generate_answer()` 只接收 ExecutionSummary + `state.summary` (EpisodeSummary)，不逐条注入 Tool Result
3. **保留 fallback**: 如果 step 数 ≤ 3，可降级为全量注入

---

## 正面发现

### Event Sourcing 设计优秀

日志中完整的事件流证明了 Event Sourcing 的正确性:

```
ConversationStarted → RunStarted → AgentThought → PlanCreated
→ DagStepStarted × N → ToolCalled × N → ToolCompleted × N
→ DagStepCompleted × N → FeedbackInjected → PlanRevised
→ PlanCompleted → AgentThought (ANSWER) → RunCompleted
```

- 33 种事件类型全部有明确语义（`harness/models/events.py:9-36`）
- 事件写入统一走 `EventStore.append_event()`，带 seq 严格递增和幂等键唯一约束
- Monitor 通过 `store.on_append()` 回调实现实时观察，符合 CQRS 的 Event Listener 模式
- 这为后续的 Replay / Checkpoint / Recovery / Audit 奠定了坚实基础

### Idempotency 机制生效

```
17:09:15 [GUARD] [idem] Cache HIT (previous result @ seq=23) semantic=SUCCESS error=null
17:09:15 [AGENT] [semantic] [step] s9 → mcp_call idempotent (cached)
17:09:15 [AGENT] [semantic] [step] s10 → mcp_call idempotent (cached)
```

- 基于 `idempotency_key_fields` 自动计算幂等键（`harness/tools/idempotency.py`）
- `EventStore.find_by_idempotency_key()` 实现缓存命中（`harness/storage/event_store.py`）
- 即使 SOFT_ERROR 状态也被正确缓存（`executor.py:147` 检查 `ToolResultType.SOFT_ERROR`）
- 为 Resume / Retry / Recovery 提供了核心保证

### ExecState / TaskState 正交设计正确

`dag_types.py` 中的 `ExecState`（系统写入）和 `TaskState`（LLM 判定）分离符合 ADR-007 设计意图。当前问题在于 Executor 对 SOFT_ERROR 的传播逻辑，而非类型系统的设计缺陷。

---

## 修复优先级建议

| 优先级 | Issue ID | 修复依赖 | 预估工作量 |
|--------|----------|----------|-----------|
| ~~**P0-立即**~~ | ~~P0-02~~ | ~~无~~ | ✅ 已完成 — `mcp_call_fn` 增加 `isError` 检查 |
| ~~**P0-立即**~~ | ~~P0-01 (短期)~~ | ~~无~~ | ✅ 已完成 — tool name 前置校验 + 前缀补全 |
| **P0-本周** | P0-03 | 无 | 🟡 已设计 + 阶段 B/C 实施中 — `DagExecutor` 依赖健康检查（门控 `step_normal`） |
| **P0-本周** | P0-04 | P0-03 | 4-6h — Scheduler 增加逐层中断检查 |
| ~~**P1-下周**~~ | ~~P1-01~~ | ~~P0-04~~ | ✅ 已完成 — REVISE prompt 强化 |
| **P1-下周** | P1-02 | 无 | 2-3h — 增加 ExecutionSummary 抽象层 |
| **P0-长期** | P0-01 (长期) | 架构变更 | 1-2d — MCP 工具一等公民注册 |

---

*审查人: Agent 导师 (Architecture Mentor)*
*审查日期: 2026-07-23*
*最后更新: 2026-07-23 — P0-02 / P0-01(短期) / P1-01 已修复*
