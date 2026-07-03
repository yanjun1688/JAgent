# 运维看板前端开发 — 上下文与需求

*将此文件内容作为提示词，在新 session 中开始前端开发。*

---

## 1. 背景

**后端系统** `Harness v2.1` — Agent-First 任务执行引擎。用户输入自然语言意图，系统自动规划(DAG Plan)、调度(Planner/Executor)并调用工具层(Tool Layer / MCP)执行。关键技术栈：事件溯源(Event Sourcing)、CQRS、幂等设计。

**现有的前端** `frontend/` 是一个面向用户的 Dashboard，展示宏观指标和分析数据（路由 `/analysis`, `/analysis/tools`, `/analysis/guardrails`, `/runs/:runId` 等）。

**新增需求**：构建一个**运维看板（Ops Dashboard）**，与现有 Dashboard 完全隔离，关注系统内部运行状态——"什么组件执行了几次、状态是什么样的、最终是什么样的"。面向运维人员。

---

## 2. 后端 API — `GET /api/v1/query`

新建的统一查询入口，单端点覆盖后端全部数据，前端通过 `?type=` 参数切换查询维度。

### 2.1 通用参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 查询类型（见下方表格） |
| `run_id` | string | 部分 | type 为 per-run 类查询时必需 |
| `include` | string | 否 | 逗号分隔子资源（仅 type=run 支持：events, timeline, tool-traces, run-analysis, plans） |
| `page` | int | 否 | 页码，默认 1，最小 1 |
| `page_size` | int | 否 | 每页大小，默认 20，最大 100 |
| `since` | float | 否 | Unix 时间戳（秒），分析类查询起始时间 |
| `until` | float | 否 | Unix 时间戳（秒），分析类查询结束时间 |

### 2.2 统一响应格式

```jsonc
// 列表类
{
  "type": "runs",
  "data": [ /* items */ ],
  "meta": { "page": 1, "page_size": 20, "total": 42, "has_more": true }
}

// 单条类
{
  "type": "run",
  "data": { /* detail */ },
  "meta": null
}

// 含 include（仅 type=run 时可能）
{
  "type": "run",
  "data": { /* run detail */ },
  "meta": null,
  "_included": {
    "events": { "type": "events", "data": [...], "meta": { ... } },
    "timeline": { "type": "timeline", "data": [...], "meta": { ... } }
  }
}
```

### 2.3 全部 18 种 type

#### 面向用户的查询（现有 API 数据的统一包装）

| type | 需要参数 | data 内容 |
|------|---------|----------|
| `runs` | page, page_size | 分页 Run 列表摘要：`run_id, intent, status, event_count, tool_call_count, tool_success_count, tool_failure_count, created_at, updated_at` |
| `run` | run_id, ?include | 单 Run 完整详情：`run_id, status, intent, seq, event_count, event_type_counts {}, total_tokens, created_at, completed_at, last_error, summary, pause_reason, tool_stats {tool_name: {call_count, completed, failed, timeout, guardrail_blocked}}, tool_results[], thought_count, pending_confirmations[], latest_plan, plan_history[], feedback_count, checkpoint_seq` |
| `events` | run_id, page, page_size | 分页事件流：每个事件 `{run_id, seq, event_type, payload, idempotency_key, created_at}` |
| `dashboard` | since, until | 全局聚合指标：`overview {total_runs, running_runs, paused_runs, completed_runs, failed_runs, total_events, total_tool_calls, total_tool_failures, total_guardrail_triggers, total_tokens_consumed, avg_tool_success_rate}` |
| `tool-stats` | since, until | 全局工具统计：`tools [{tool_name, call_count, success_count, failure_count, timeout_count, guardrail_blocked_count, avg_duration_ms}]` |
| `guardrail-stats` | since, until | 全局防护统计：`guardrails [{guardrail_id, trigger_count, tools_affected[], recent_reason}]` |
| `run-analysis` | run_id | 单 Run 分析：`{run_id, intent, status, event_count, total_tokens, total_duration_ms, created_at, completed_at, tool_trace_count, guardrail_event_count, feedback_count}` |
| `timeline` | run_id, page, page_size | 分页事件时间线：含解析后的结构化事件详情（tool_call_id, tool_name, duration_ms, error 等） |
| `tool-traces` | run_id | 单 Run 工具调用全链路追踪：`tool_traces [{tool_call_id, tool_name, status, input, output, duration_ms, guardrail_id, retryable {eligible, ineligible_reason}}]` |

#### 运维内部状态查询（全新 API，当前无其他接口可达）

| type | 需要参数 | data 内容 |
|------|---------|----------|
| `tool-defs` | — | 已注册工具定义：`[{tool_name, definition {name, description, input_schema, output_schema, side_effects[], timeout_ms, retry_policy, guardrails[], requires_confirmation}}]` |
| `schedulers` | — | 活跃调度器状态：每项 `{run_id, status, intent, seq, event_count, last_error, pause_reason, is_active, is_paused, config {max_iterations, max_consecutive_failures, pause_timeout_ms, confirm_timeout_ms, max_confirm_retries}, tool_stats {tool_name: {call_count, completed, failed, timeout, guardrail_blocked}}, latest_plan}` |
| `mcp` | — | MCP 服务器连接状态：`servers [{name, command, url, enabled, auto_register_tools, timeout_ms, connected}]` + `connected_count` |
| `plans` | run_id | DAG 计划历史：`{run_id, plan_history[], latest_plan {plan_id, intent, steps_summary, layer_count, steps[{step_id, tool_name, status}], revision_reason, remaining_steps_summary}, plan_boundary_seqs[]}` |
| `system` | — | 系统配置：`llm_client {type, model, base_url, total_calls}, tool_registry {tool_count, tool_names[]}, scheduler_config {max_iterations, ...}, tool_defs_count` |
| `ws-clients` | run_id(可选) | 不传 run_id：`total_connections, by_run {run_id: count}`；传 run_id：`{run_id, connected_clients}` |

#### 反馈 / 监控 / 健康查询（v2.2 新增）

| type | 需要参数 | data 内容 |
|------|---------|----------|
| `feedback` | run_id(可选), page, page_size | 不传 run_id：全局 FeedbackInjected 事件流（按 created_at DESC 分页）。传 run_id：该 run 的反馈事件。每项 `{run_id, seq, created_at, feedback_id, source, category, feedback_text, priority, affected_tool, error_type, error_detail, suggestion, expires_at_seq, resolves_feedback_id, consumed_at_seq}` |
| `monitor` | run_id(可选) | 不传 run_id：全局摘要 `{monitored_run_count, runs[{run_id, last_seen_seq, consecutive_failures, estimated_tokens, token_warning_sent, endpoints_tracked, tools_with_failures, deduped_feedback_count}], config}`。传 run_id：单 run 详情 `{run_id, last_seen_seq, consecutive_failures, consecutive_per_endpoint, failures_per_tool, failure_error_map, estimated_tokens, token_warning_sent, repeated_call_count, repeated_fail_count, pending_calls, deduped_feedback_keys[], config}` |
| `health` | — | 系统健康检查：`{status: "ok"\|"degraded", components: {store, schedulers, ws_clients, llm_client, mcp_manager, monitor, tool_registry}}`，每个组件 `{status: "ok"\|"error"\|"missing"}` |

---

## 3. 前端技术栈与现有架构

### 3.1 技术栈

| 层 | 内容 |
|----|------|
| 框架 | **React 18** + **TypeScript 5.5** (strict) |
| 打包 | **Vite 5** (dev port 5173, proxy `/api` → `localhost:8000`) |
| 路由 | **react-router-dom v6** |
| 图表 | **recharts 3.8.1** |
| UI 库 | **无** — 全部 CSS-in-JS（`React.CSSProperties` 内联对象） |
| HTTP | **原生 fetch()** — 无 axios |
| 状态管理 | **无** — 页面直接 fetch，useState/useEffect |

### 3.2 样式体系

复用 `frontend/src/api/analysis-styles.ts`：
- `colors` 对象（primary #3ECF8E, blue, red, orange, purple, teal, gray, bg #f5f6f8, card #fff, border #e8eaed 等）
- `card` 样式对象（圆角卡片 + 阴影）
- `valueText` / `labelText`（KPI 数值/标签样式）
- `badge(bg, fg)`（状态徽章）
- `statusBadge(status)`（running/completed/failed/paused 颜色映射）
- `sectionTitle`, `table`, `th`, `td`（表格和章节标题）
- `formatTime(ts)`, `formatDuration(ms)`, `fmt(n)`, `pct(n)`（工具函数）

### 3.3 API 调用模式

模式见 `frontend/src/api/client.ts` 和 `frontend/src/api/analysis-client.ts`：
- 每个 API 函数导出为 async 函数，直接用 `fetch(BASE + path)`
- 类型在 `api/schema.ts`（OpenAPI 自动生成）和 `api/analysis-types.ts`（手写）
- 错误处理：调用的页面组件 try/catch

### 3.4 现有路由

```tsx
// App.tsx
<Routes>
  <Route path="/" element={<RunList />} />
  <Route path="/runs/:runId" element={<RunDetail />} />
  <Route path="/analysis" element={<Dashboard />} />
  <Route path="/analysis/tools" element={<ToolsPanel />} />
  <Route path="/analysis/guardrails" element={<GuardrailPanel />} />
  <Route path="/analysis/runs/:runId" element={<RunAnalysis />} />
</Routes>
```

导航栏在 `App.tsx` 中：Dashboard | Tools | Guardrails | Runs

---

## 4. 需要新建的文件（全部隔离，不修改任何现有文件）

### 4.1 `frontend/src/api/ops-client.ts` — Ops API 客户端

```ts
// 基础 URL: /api/v1，所有查询走同一个端点
// 模式：fetch(`${BASE}/query?type=X&...`)

// 导出类型接口（TS 接口，根据上面 2.3 节的 data 字段定义）
// 注意：key 名与后端 Pydantic 模型保持一致（snake_case）

export interface RunsItem {
  run_id: string
  intent: string
  status: string
  event_count: number
  tool_call_count: number
  tool_success_count: number
  tool_failure_count: number
  created_at: number
  updated_at: number
}

export interface RunDetail {
  run_id: string
  status: string
  intent: string
  // ... 全部字段见 2.3 节 run 定义
}

// 按 2.3 节定义每种 type 的响应类型
// 导出对应的 async 函数：queryRuns(), queryRun(), queryEvents(), ...
```

### 4.2 `frontend/src/pages/OpsDashboard.tsx` — 运维看板主页面

**路由**：`/ops`

**布局建议**（单页，不做分 tab）：

```
┌─────────────────────────────────────────────────────┐
│  [KPI 行]                                           │
│  总 Runs │ 运行中 │ 已完成 │ 已失败 │ 工具调用 │ ... │
├──────────────────────────┬──────────────────────────┤
│  活跃 Schedulers         │  工具调用排行            │
│  (调度器状态表)          │  (柱状图/表格)           │
│                          │                          │
│  run_id | status |       │  tool_name | calls |     │
│  intent | tools | ...    │  success | fail | ...    │
│                          │                          │
├──────────────────────────┼──────────────────────────┤
│  最近 Runs               │  MCP 服务器状态          │
│  (最近 10 条 run 列表)   │  (连接状态列表)          │
│  点击跳转到 /ops/runs/   │                          │
│  :runId                  │  server | connected | ...│
├──────────────────────────┴──────────────────────────┤
│  防护触发统计                                        │
│  guardrail_id | trigger_count | tools_affected | ... │
└─────────────────────────────────────────────────────┘
```

**数据获取**（页面加载时并行）：
```ts
Promise.all([
  queryDashboard(),       // type=dashboard — KPI 行
  querySchedulers(),      // type=schedulers — 活跃调度器
  queryToolStats(),       // type=tool-stats — 工具排行
  queryRuns(1, 10),       // type=runs — 最近 runs
  queryMcp(),             // type=mcp — MCP 状态
  queryGuardrailStats(),  // type=guardrail-stats — 防护统计
])
```

**可选**：5 秒轮询刷新 `type=schedulers` 和 `type=dashboard`（和现有 RunList 的 setInterval 模式一致）。

### 4.3 `frontend/src/pages/OpsRunDetail.tsx` — 运维 Run 详情页

**路由**：`/ops/runs/:runId`

**与现有 RunDetail / RunAnalysis 的区别**：
- 聚焦**运维视角**：不展示 ChatDrawer、ConfirmDialog
- 核心信息：工具调用详情（什么工具、几次、什么状态、最终结果）
- 使用 `include` 参数一次拉取全部子资源：

```ts
queryRun(runId, include="events,timeline,tool-traces,run-analysis,plans")
```

**布局建议**：
```
┌─────────────────────────────────────┐
│  基本信息 + 状态                    │
│  run_id | status | intent | tokens │
├─────────────────────────────────────┤
│  工具执行摘要                       │
│  tool_stats（每种工具调用次数+状态） │
├─────────────────────────────────────┤
│  DAG 计划执行状态                   │
│  latest_plan → steps 进度          │
├─────────────────────────────────────┤
│  事件流（简洁模式）                 │
│  只显示关键事件：ToolCalled/        │
│  Completed/Failed/Guardrail        │
└─────────────────────────────────────┘
```

### 4.4 `frontend/src/pages/OpsSystem.tsx` — 系统配置/状态页（可选）

**路由**：`/ops/system`

展示 `type=system` 的返回数据：LLM 配置、工具注册表、调度器默认配置等。

### 4.5 修改 `frontend/src/App.tsx`（唯一需修改的现有文件）

在导航栏添加 `Ops` 链接，在 Routes 中注册新路由：

```tsx
import OpsDashboard from './pages/OpsDashboard'
import OpsRunDetail from './pages/OpsRunDetail'

// 在 header 的 <NavLink> 组中添加：
<NavLink to="/ops" label="Ops" />

// 在 <Routes> 中添加：
<Route path="/ops" element={<OpsDashboard />} />
<Route path="/ops/runs/:runId" element={<OpsRunDetail />} />
```

---

## 5. 关键约束

1. **隔离性**：所有新文件放在 `pages/` 下以 `Ops` 前缀命名，API 客户端放在 `api/ops-client.ts`。不修改任何现有 page/component 文件
2. **样式一致性**：复用 `analysis-styles.ts` 的导出对象，不用新样式框架。不安装新 npm 依赖（除非确有必要）
3. **TypeScript**：所有类型显式定义在 `ops-client.ts` 中，接口名和键名与后端 snake_case 保持一致
4. **错误处理**：每个 fetch 调用 try/catch + loading/error state，和现有页面模式一致
5. **无全局状态**：不用 Redux/Zustand/React Query，遵循现有 useState + useEffect 模式

---

## 6. 实现步骤建议

1. 创建 `frontend/src/api/ops-client.ts` — 定义全部 TS 类型 + query 函数
2. 创建 `frontend/src/pages/OpsDashboard.tsx` — 主看板页面
3. 创建 `frontend/src/pages/OpsRunDetail.tsx` — Run 详情页
4. 修改 `frontend/src/App.tsx` — 加导航和路由
5. `tsc --noEmit` 验证类型
6. `npm run dev` 启动开发服务器验证

---

## 7. 验证方法

```bash
# 1. 启动后端（已有进程确保 8000 端口在运行）
python -c "from harness.api.app import app; import uvicorn; uvicorn.run(app, port=8000)"

# 2. 先 curl 验证 API 可用
curl "http://localhost:8000/api/v1/query?type=dashboard"
curl "http://localhost:8000/api/v1/query?type=runs&page=1&page_size=5"

# 3. 启动前端
cd frontend && npm run dev

# 4. 访问 http://localhost:5173/ops
```

---

*文件位置: JAgent-docs/FRONTEND_OPS_DASHBOARD_PROMPT.md*
*目标: 在新 session 中直接作为提示词使用，无需额外上下文*
