# REPLAY INSPECTOR v1.0 — 时间旅行调试器（Event Replay Inspector）

> 状态：已交付（只读）
> 层级：L6 只读 API + L7 前端可观测性
> 关联：[ARCHITECTURE_v3.3](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md) §16

## 1. 目标与范围

把事件溯源的"架构红利"——任意历史时刻状态可由事件流经 `fold_events` 唯一重建——做成一个可直接上手的 Web 调试工具。核心场景："这次 Run 的 Plan 判定失败了，定位是哪一步、什么原因让状态从正常变为失败"。

**本期交付（严格只读）**
- 时间线浏览：一次 Run 的完整事件时间线（数百条流畅）。
- 任意时刻状态重建：选中时间线一点，展示该时刻完整状态（运行状态、Plan 与各步骤、工具结果、Guardrail 拦截、待确认、错误）。
- 区间状态对比：选两个时刻，突出"运行状态如何变化"与"哪些步骤状态改变"。
- Langfuse 交叉查看：**字段已预留**（`langfuse_trace_url`），本期不接跳转（见 §6 已知局限）。

**本期明确不做（但已为其预留结构）**
- 从历史时刻回滚 / 分叉重放（见 §5 接入点说明）。
- 不做独立部署、不新增权限体系、不做仪表盘式花哨呈现。

## 2. 关键设计决策及理由

1. **状态重建唯一来源 = `fold_events`**。`projection.reconstruct_state(events, at_seq)` 只做事件切片（`seq <= at_seq`）后调用 `harness/core/fold.py::fold_events`，**不另起任何状态推导**。这是"状态由事件流唯一确定"原则的直接体现。
2. **纯函数式重建缝，不与"展示"焊死**。`projection.py` 无 I/O、无 tenant、无 store，签名是 `events -> RunState/View/Diff`。它不关心调用方拿状态做什么——未来回滚可直接复用，不需要重构。
3. **读写结构分离且可静态审查**。只读能力集中在 `harness/replay/`（projection/schemas/service）+ `harness/api/replay_routes.py`：
   - HTTP 全 GET；
   - 只读包不得 import 任何写入/执行/监控组件，由 AST 扫描测试强制；
   - 一律经 `api.store`（`ScopedEventStore`），租户隔离自动生效，跨租户 = 404。
4. **结构化字段从事件切片补齐**。`fold` 会把 Guardrail 拦截拍平成错误字符串；`project_state_view` / `diff_states` 在折叠态之外，额外扫描同一段事件切片以恢复 `guardrail_id/reason/event_seq` 等结构化信息（状态本身仍只来自 fold）。
5. **差异突出两类信息**。`StateDiff` 把 `status_change`（运行状态跃迁）与 `steps_changed`（步骤状态变化）作为一等、醒目字段；另含 `tool_results_added`、`guardrails_triggered`、`error_change`、`events_in_range`。
6. **前后端契约同源**。响应模型为 Pydantic v2，前端 TS 类型由 `scripts/generate_openapi.py` 从 OpenAPI 生成到 `src/api/schema.ts`，零手写重复、零新前端依赖。

## 3. 模块与接口

后端（`harness/replay/`）
- `projection.py`：`reconstruct_state` / `project_state_view` / `project_timeline_event` / `diff_states`（纯函数）。
- `schemas.py`：`ReplayRunMeta` / `ReplayTimelineResponse` / `RunStateView` / `StateDiff` 等。
- `service.py`：`ReplayInspectorService(store, trace_url_provider=None)`；`ReplayRunNotFoundError`(404) / `ReplaySeqOutOfRangeError`(400)。
- `harness/api/replay_routes.py`：全 GET 路由，`app.py` 注册。

| 方法 | 路径 | 边界 |
|---|---|---|
| GET | `/api/v1/replay/runs/{run_id}/meta` | 不存在→404 |
| GET | `/api/v1/replay/runs/{run_id}/timeline?cursor=&limit=` | 不存在→404 |
| GET | `/api/v1/replay/runs/{run_id}/state?at_seq=N` | 不存在→404；越界→400；`at_seq<1`→422 |
| GET | `/api/v1/replay/runs/{run_id}/diff?from_seq=A&to_seq=B` | 不存在→404；`A>B`/越界→400 |

前端（`frontend/src/`）
- `api/replay-client.ts`：GET fetch 封装 + `ReplayApiError`。
- `pages/ReplayPage.tsx`（`/replay`、`/replay/:runId`）。
- `components/replay/`：`ReplayRunPicker`、`EventTimelineList`（memo 行）、`StateSnapshotPanel`、`StateDiffPanel`、`statusColors.ts`。
- 入口：顶部导航"调试" + 历史页详情"调试"深链。React Query 拉取，无 mutation、无 WS。

## 4. 测试（TDD/BDD）

- 后端 `tests/test_replay_projection.py`（纯函数：fold-at-seq、view、diff）、`test_replay_service.py`（异常 + trace provider 注入）、`test_replay_api.py`（HTTP、租户隔离、404/400/422 边界、空 run、**GET-only + 导入白名单静态守卫**），共用 `tests/replay_fixtures.py`。
- 前端 `frontend/tests/components/replay/`：`statusColors` 纯逻辑、`EventTimelineList`（选点/A-B 标记）、`ReplayPanels`（状态/diff 渲染、空态、错误态）、`ReplayPage`（加载、对比模式、404 反馈）。

## 5. 未来"回滚 / 从历史点分叉"接入点说明

**复用、无需改动**
- `projection.reconstruct_state(events, at_seq)` —— 回滚/分叉首先需要的"历史 `RunState`"由它产出。
- `project_state_view` / 全部 GET 查询与路由 / 前端时间线与状态渲染。

**需要新增（独立、显式、同样可审查的写入路径）**
- 新的写入服务（建议 `harness/replay/` 旁新增写入模块或独立包），显式 import 受信写入组件，经 `ScopedEventStore.append_event` **追加**回滚/分叉事件（append-only 下回滚 = 追加补偿/分叉事件，而非 UPDATE/DELETE）。
- 新的**非 GET** 路由（如 `POST /api/v1/replay/runs/{run_id}/fork`），与只读 router 物理分离。
- 只读包的导入白名单守卫保持不变；为写入 router 单独定义其允许导入的模块集合。
- 前端在对比/状态视图旁新增显式"分叉/回滚"入口（本期不放置任何半成品按钮）。

**为什么不会推倒重来**："历史时刻"概念（`at_seq`/`from_seq`/`to_seq`、`RunStateView`）从命名到数据结构都不与"仅展示"绑定；重建是返回 `RunState` 的纯能力，写入路径在其之上新增即可。

## 6. 已知局限

1. **Langfuse 跳转本期未接通**：按交付决策，`langfuse_trace_url` 字段已预留但当前恒为 `null`，前端仅在非空时渲染跳转入口（显示"Langfuse: 未配置"）。trace_id 可由 `run_id` 确定性推导（`Langfuse.create_trace_id(seed=f"jagent:{run_id}")`），接入时在 `replay_routes._service` 注入 `trace_url_provider` 即可，无需改 projection/schemas。
2. 时间线一次性拉取（`limit<=1000`，默认 1000）；超千条事件的 Run 暂未做无限滚动（游标字段已就绪）。
3. 状态视图为调试投影，非全量 `RunState`（省略 context_snapshot、thought 正文等大字段，仅给计数/摘要）；需要原始 payload 时可展开时间线事件查看。
4. 只读边界靠"全 GET + 导入白名单测试"约束；若未来有人在 `harness/replay/` 内引入写入 import，CI 的静态守卫测试会失败。

## 7. 崩溃恢复（孤儿 run）：当前策略与"自动续跑"未来接入点

### 7.1 当前策略（方向 A：孤儿即终态，不自动续跑）

进程崩溃/重启时，调度器内存态（`_running_tasks` / `_pause_events` / `_confirm_events` / `_deadlines` 等 `dict[run_id, ...]`）全部丢失，但事件流里这些 run 仍是 RUNNING/PAUSED——成为"事件流在跑、无人推进"的孤儿。启动时 `harness/core/lifecycle.py::mark_orphans` 扫描全部 run，对 RUNNING/PAUSED 的孤儿**追加两个事件收敛为终态**：

1. `RUN_ORPHANED` —— 诊断旗标（fold 置 `state.orphaned=True`），供 UI 徽标、归因、资源回收器订阅（浏览器池据此释放租约）；
2. `RUN_FAILED` —— 终态收敛，payload 明确"因服务重启中断、请重新发起任务、已完成步骤不会重复执行"。

**本期明确不做崩溃后自动断点续跑**。操作员对中断任务的处理方式是**基于事件溯源重新发起一次任务**：幂等键 + F-4 证据重建（`harness/core/recovery.py::rebuild_results_from_evidence`）保证重跑时已完成步骤不重复执行、副作用不重放。相应地，控制接口在"无活 scheduler"时 fail-closed：`POST /runs/{id}/pause` 与 `/resume` 返回 409，不再写入无人执行的 `RunPaused`/`RunResumed`（那会制造假 PAUSED / 假 RUNNING 僵尸）；`/confirm` 因 run 已 FAILED 被既有 `status != PAUSED` 守卫拒绝。

### 7.2 为什么"自动续跑"能力已铺好路但未接线

- `rebuild_results_from_evidence`（F-4）已能从受信 `step_evidence` 投影重建 DAG `results`：终态步骤跳过、崩溃时在飞/未启动步骤复原为 PENDING 重跑；`ContextManager.find_resume_seq` 提供上下文 checkpoint。
- 但这些目前只服务于**同进程内** revise 重入；崩溃后**没有任何代码为孤儿 run 重建 scheduler / execution backend / 确认窗口**——"最后一公里"接线（启动时 rehydrate scheduler）不存在。

### 7.3 未来"自动续跑"接入点（本期保留，不实现）

若未来要做崩溃后自动续跑，需新增（与本只读调试器物理分离的写入/执行路径）：
- 启动时为每个 **PAUSED 等待确认** 的孤儿重建 scheduler 并挂起等待确认；RUNNING 孤儿回滚到上一个 `find_resume_seq` checkpoint 后，经 `_execute_plan` 的证据重建（plan.py:430-461 已就位）续跑。
- execution backend（Docker/SSH/目录）的重建与孤儿容器/连接 reaper（当前 backend 清理只走 scheduler 终态回调，孤儿载体资源需独立回收器）。
- 明确并发归属：一个 run 同时只能被一个 scheduler 驱动，需受信租约/ fencing 防止重启竞态双驱动。
- 该能力属独立里程碑，不应作为补丁塞进 `mark_orphans`；届时孤儿策略从"即终态"切换为"可续跑"需同步修订本节与 lifecycle 注释。

