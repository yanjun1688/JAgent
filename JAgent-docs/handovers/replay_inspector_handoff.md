# 交接提示词：时间旅行调试器（Event Replay Inspector）排障 / 续作

> 用法：把下面「==== 复制以下提示词到新 session ====”」之间的整段内容贴给新的 opencode session。
> 项目根：`D:\Project\JAgent`；协作规范见 `AGENTS.md`。

====================================================================
## 复制以下提示词到新 session ↓↓↓

我在 JAgent(Harness) 项目里新增了一个**只读**的「时间旅行调试器 / Event Replay Inspector」，
代码已写完、测试全绿，但我实际跑起来可能遇到 bug，请帮我排查修复。**严格遵守 AGENTS.md 的分层与
受信边界，改动要根治（不要打补丁），并保持只读边界不被破坏。**

### 这个功能是什么
基于事件溯源：任意历史时刻的系统状态由事件流经 `harness/core/fold.py::fold_events` 唯一确定。
本功能把该能力暴露为只读 Web 调试工具：浏览 run 的事件时间线、重建任意 seq 时刻的完整状态、
对比两个时刻的状态差异（突出运行状态跃迁与步骤状态变化）。**纯只读，全 GET，无任何写入/执行。**

### 后端文件（新增）
- `harness/replay/projection.py` —— **纯函数层（也是未来回滚功能的接入缝）**：
  - `reconstruct_state(events, at_seq=None)`：仅做 `seq<=at_seq` 切片后调用 `fold_events`，绝不另写状态推导。
  - `project_state_view(events, at_seq, latest_seq) -> RunStateView`
  - `diff_states(events, from_seq, to_seq) -> StateDiff`
- `harness/replay/schemas.py` —— Pydantic v2 响应模型（OpenAPI 唯一来源）。
- `harness/replay/service.py` —— `ReplayInspectorService(store, trace_url_provider=None)`；
  异常 `ReplayRunNotFoundError`(→404)、`ReplaySeqOutOfRangeError`(→400)。
  **注意 seq 边界校验用的是租户可见流的真实 `[first_seq, last_seq]`，不是 `[1, latest]`**（这是修过的一个 bug）。
- `harness/api/replay_routes.py` —— 全 GET 路由。
- `harness/api/app.py` —— 已 `app.include_router(replay_router)`（这是对现有文件的唯一改动）。

### 后端接口（全 GET，租户隔离靠 `api.store` = ScopedEventStore，跨租户读为空→404）
- `GET /api/v1/replay/runs/{run_id}/meta`
- `GET /api/v1/replay/runs/{run_id}/timeline?cursor=&limit=`
- `GET /api/v1/replay/runs/{run_id}/state?at_seq=N`（不传=最新；run 不存在→404；seq 越界→400；at_seq<1→422）
- `GET /api/v1/replay/runs/{run_id}/diff?from_seq=A&to_seq=B`（A>B 或越界→400）

### 前端文件（新增，React18 + React Query + Tailwind，零新依赖）
- `frontend/src/api/replay-client.ts` —— fetch 封装 + `ReplayApiError`
- `frontend/src/pages/ReplayPage.tsx` —— 路由 `/replay` 与 `/replay/:runId`
- `frontend/src/components/replay/` —— `ReplayRunPicker / EventTimelineList / StateSnapshotPanel / StateDiffPanel / statusColors.ts`
- 已改现有文件：`frontend/src/App.tsx`（加路由）、`frontend/src/components/Header.tsx`（导航加“调试”）、
  `frontend/src/components/history/RunDetailPanel.tsx`（加“调试”深链）
- `frontend/src/api/schema.ts` 与 `frontend/public/openapi.json` 已由 `uv run python scripts/generate_openapi.py` 重新生成
  —— **如果你改了后端 Pydantic 模型，必须重跑这个脚本再让前端用新类型。**

### 测试
- 后端：`tests/test_replay_projection.py`、`tests/test_replay_service.py`、`tests/test_replay_api.py`
  （含租户隔离、404/400/422 边界、空 run、**只读静态守卫**：AST 扫描 replay 包不得 import
  scheduler/tools/execution/monitoring 等写入组件，且路由必须全 GET）；共用 `tests/replay_fixtures.py`。
- 前端：`frontend/tests/components/replay/` 下 4 个测试文件。

### 怎么跑
- 后端：`uv run python -m harness.api.serve`（:8000；无 LLM_API_KEY 时是 Mock 模式；Windows 若遇事件循环问题加 `--loop harness.api.loop:event_loop_factory`）
- 前端：`cd frontend; npm run dev`（:5173，/api 代理到 8000）
- 后端测试：`uv run python -m pytest tests/test_replay_projection.py tests/test_replay_service.py tests/test_replay_api.py -q`
- 前端测试：`cd frontend; npx vitest run tests/components/replay`
- lint：`uv run ruff check harness/replay harness/api/replay_routes.py`
- 类型检查：`cd frontend; npx tsc --noEmit`

### 已修复的一个关键 bug（避免回归）
跨租户共用 run_id 的脏数据（例：`.harness.db` 里 run `1910a3af`，31 条事件属 `blackbox-tenant-a`，
1 条 RunStarted 属 `default` 且在 seq 21）。default 租户请求 `state?at_seq=10` 时可见流切片为空，
`fold_events([])` 抛错曾导致 **500**。已在 `service.py` 改为按可见流 `[first_seq,last_seq]` 校验 → 干净 **400**，
并有回归测试 `test_partial_visible_stream_does_not_500_on_early_seq`。**不要把它改回按 [1,latest] 校验。**

### 已知边界 / 非 bug
- Langfuse 跳转：本期按决定**只预留字段** `langfuse_trace_url`（当前恒 null），前端显示“Langfuse: 未配置”，
  不渲染链接。trace_id 可由 `run_id` 确定性推导（`Langfuse.create_trace_id(seed=f"jagent:{run_id}")`），
  接入时在 `replay_routes._service` 注入 `trace_url_provider` 即可，不要改 projection/schemas。
- 时间线一次最多拉 1000 条（cursor 字段已就绪，未做无限滚动）。
- 启动时 MCP 'fetch' server 报 npm 404 是**既有问题、与本功能无关**。
- 前端时间线默认展示最新 seq 的状态；对比模式下在时间线点两下选 A→B。

### 文档
- 设计与“未来回滚接入点”说明：`JAgent-docs/architecture/REPLAY_INSPECTOR_v1.0.md`
- 架构章节：`JAgent-docs/architecture/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md` §16

请先按“怎么跑”把前后端起起来复现我遇到的问题，定位根因后修复，并补充对应回归测试；
改完跑上面列出的测试与 lint/typecheck，确保文档/代码/行为一致。

## 复制以上提示词到新 session ↑↑↑
====================================================================
