# Harness v2.1 — 实现路线图

> 基于 `harness_v2.1.md` 架构方向生成
> 已完成里程碑：MVP → V0.2 → V0.3 → V0.4 → V0.4+ → V0.5 → V0.5+ → V0.6 → V0.6+ → V0.7 → V1.0 分析平台
> 当前阶段：V0.7 — Planner-Executor + DAG（已实现）+ `dynamic` 字段移除
> 下一阶段：V0.9 — 生命周期恢复（设计文档已就绪）

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
| V0.6+ | 架构加固：Skill Tool Layer 路由 / 输出校验 / 循环检测 / side_effects 消费 / 幂等验证 + 271 项测试全通过 | ✅ |
| V0.6.1 | 反馈机制增强：结构化 Payload / per-tool RunMonitor / Schema 统一治理 / Planner 反馈注入 / Operator API + 334 项测试全通过 | ✅ |
| V1.0 | 分析平台：AnalysisService + 6 个 API 端点 + 操作锚点预埋 + 时间窗口 + 分页 | ✅ |
| **V0.7** | **Planner-Executor + DAG：Planner / DagExecutor / PlanGuardrail / 7 个新事件 / dynamic 退化路径 / 降级回退** | ✅ |
| **V0.7 (Phase 5)** | **旧 Scheduler 重构：BaseScheduler 执行基础设施统一 / _fail 合并 / AgentLoopScheduler 精简为纯降级路径 / 355 项测试全通过** | ✅ |
| **V0.9** | **生命周期恢复：服务器重启孤儿 Run 检测 + abandon/retry 决策** | 📄 设计完成 |

当前基线：**341 项测试全通过**。
当前阶段：V0.7 — Planner-Executor + DAG 已实现。
下一阶段：V0.9 — 生命周期恢复（设计文档已就绪，待用户审查）。

> **已明确暂缓（用户决策，一期线上观察后再定）**：
> - Predictive Guardrails (PlanRiskReport + self-correction)
> - Revise 失败原因分类 `_classify_failures()`
> - `_execute_static/dynamic_plan` 去重（收益有限，不增加复杂度）

### 架构修复（2026-06-07）
- ✅ DAG_STEP fold 去重（4 个新测试）
- ✅ seq 分配原子性 + asyncio.Lock（2 个新测试）
- ✅ 执行/事件写入分离（改1）
- ✅ Revise 策略修复：parameters→input 兼容映射 + 最终总结回答 + 压缩白名单（改3 部分）
- ✅ 信号量并发控制（改5）
- ✅ 配置修复：Mock 压缩禁用、max_response_bytes
- ✅ 顶层导出 + P3 代码规范
- ✅ 20 个新 V0.7 测试（test_dag_executor.py + test_planner.py）
- ✅ Scheduler 层次重构：AgentLoopScheduler(BaseScheduler)继承，净减78行（5 个新继承测试）
- ✅ P1-1: `_max_retries` 重试循环（event_store.py seq 冲突自动重试）
- ✅ P2-1: `on_append` 回调错误隔离（try/except 包裹每个回调）
- ✅ P2-2: `_check_max_parallel` 简化（移除未使用的 `warnings` 变量）
- ✅ P2-3: `_get_feedback_text` 复用（替换 `_run_loop` 内联重复）
- ✅ P2-4: `PlanningExecutorScheduler._fail` 覆写（"execution round(s)" 措辞）
- ✅ P2-5: 熔断检查提取（`_breaker_tripped()` 统一方法）
- ✅ #1: `models/__init__.py` 补全 V0.7 导出（8 个事件模型 + `EpisodeSummary` + `DagPlan`/`DagStep`）
- ✅ #2: `_generate_answer` 委托给 `Planner.generate_answer()` 修复封装
- ✅ #3: `_seq_locks` TOCTOU 修复（批量定时清理替代 check-then-pop）
- ✅ #4: `build_dag_status_text` 中文→英文
- ✅ #5: `DagPlan`/`DagStep` `@dataclass` → Pydantic `BaseModel`
- ✅ #6: `EpisodeSummary` 导入路径修复（`models.events`）
- ✅ #7: `RateLimitGuardrail._call_history` 污染警示 docstring
- ✅ 自然边界压缩：fold 记录 `plan_boundary_seqs`，`select_compression_window` 对齐到最近的 plan 边界
- 🚫 Predictive Guardrails 已明确暂缓（需更多设计评估，一期线上观察后再定）
- 🚫 Revise 失败分类 `_classify_failures()` 已明确暂缓（用户决策）
- 🚫 `_execute_static/dynamic_plan` 去重已明确暂缓（revise 策略/返回类型不同，提取收益有限）

### 架构修复（2026-06-08）
- ✅ P0: 移除 flattening，输出=Schema — `http_request.py` 删 `result.update(body)`（-2 行），输出结构与 output_schema 100% 一致；`dag_executor.py` typed resolution 不变，但 `$s1.uuid` 不再可用（需 `$s1.body.uuid`）；Prompt 两模板各加一行 `Use $step_id.field to reference a previous step's output.`，Example 2 替换为无变量引用的依赖示例
- ✅ P1: revise 时 intent 从 `state.intent` 传递 — `scheduler.py` 4 个 `planner.revise()` 调用点传入 `intent_fallback=s.intent`；修正 revise prompt 中原意图始终为 `(unknown)` 的 bug
- ✅ P2: `_parse_plan` 支持前缀文本 — `planner.py` JSON 解析失败时回退提取 `{...}` 内容，LLM 在 revise 时先解释再输出 JSON 不再导致解析失败（+7 行）
- ✅ Phase 5: 旧 Scheduler 重构完成 — `_fail` 从 3 份统一为 BaseScheduler 1 份；`_run_tool_call` / `_breaker_tripped` / `_find_tool_def` / `_wait_for_resume` 上移 BaseScheduler；`_ensure_run_started` / `_is_cancelled` 新增；AgentLoopScheduler 精简 ~180 行（仅剩核心循环 + _FallbackKernel）；PlanningExecutorScheduler 移除 `_fail` 覆写和 RunStarted 内联；`run()` 统一 finally cleanup；338 项测试全通过
- 📄 **设计完成**：生命周期恢复机制 — `JAgent-docs/lifecycle_recovery.md` v1.0

### 架构修复（2026-06-11）
- ✅ 内存泄漏修复：`BaseScheduler` 新增 `run_end_cb` 参数，`run()` finally 调用 → API 层清理 `_schedulers` / `_ws_clients`（`deps.py:cleanup_run_resources`）；`ws.py` WebSocket 断连时 pop 空 key；`routes.py:delete_run` 末尾补充清理；两个子类（`AgentLoopScheduler`/`PlanningExecutorScheduler`）同步添加 `run_end_cb` 透传（355 项测试通过）
- ✅ output_schema 两阶段校验 + 错误文本脱敏：`executor.py` Phase 1（严格 jsonschema）→ Phase 2（`_structurally_usable()` 结构性兜底，dict/list 通过，None/bool/str/int/float 拒绝）；Phase 2 失败时 error 脱敏（仅含类型名，不含原始数据），保护两条 LLM 数据通路（ToolResult 直接展示 + Monitor 反馈 injected error_detail）；新增 8 项测试（355 项测试全通过）
- ✅ `ARCHITECTURE_v2.1.md` 新增 Cleanup 契约说明（§4.7）和 §7 已知技术债务
- Known Technical Debt 记录在三处代码注释 + TODO_v2.1.md 底部

---

## V0.9 — 生命周期恢复（Lifecycle Recovery）

**状态**: 📄 设计完成（待审查）
**前置依赖**: V0.7 Phase 5 完成（✅）
**目标**: 服务器重启后检测孤儿 Run，提供 abandon/retry 用户决策路径

### 设计文档

`JAgent-docs/lifecycle_recovery.md` v1.0

### 核心变更

| 变更 | 说明 |
|------|------|
| 新增 `RunOrphaned` 事件类型 | 服务器重启时写入，标记 Run 与 Scheduler 失联 |
| 新增 `RunState.orphaned` 字段 | fold_events 感知孤儿状态，不影响 `status` |
| 新增 `harness/core/lifecycle.py` | 孤儿检测 + abandon/retry 服务函数（与 BaseScheduler 解耦） |
| `BaseScheduler._try_checkpoint_recovery()` | 从 `_ensure_run_started` 抽取 checkpoint 恢复逻辑 |
| `EventStore.list_all_run_ids()` | 启动扫描时获取全量 run_id |
| `app.py lifespan` 接入 | 启动时自动扫描并标记孤儿 Run |
| `POST /abandon` + `POST /retry` | 用户决策端点 |
| `confirm` 检查 orphaned | 阻止无意义的确认操作 |

### 新增/修改文件

| 文件 | 职责 |
|------|------|
| `harness/models/events.py` | `RUN_ORPHANED` 事件类型 + `RunOrphanedPayload` |
| `harness/core/fold.py` | `RunState.orphaned` 字段 + fold case |
| `harness/core/lifecycle.py` | **新文件** — 孤儿检测、标记、abandon、retry |
| `harness/core/scheduler.py` | `_try_checkpoint_recovery()` — 从 `_ensure_run_started` 抽取 |
| `harness/storage/event_store.py` | `list_all_run_ids()` |
| `harness/api/app.py` | lifespan 调用 `mark_orphans` |
| `harness/api/routes.py` | `abandon` / `retry` 端点 + `list_runs` 加 `orphaned` + `confirm` 检查 |
| `tests/test_lifecycle.py` | **新文件** — 16 个测试用例 |

### 验收检查清单

- [ ] `RunOrphaned` 事件类型定义完整（枚举 + Payload + PAYLOAD_MODEL_MAP + fold）
- [ ] `fold_events` 处理 `RUN_ORPHANED` 时设置 `orphaned=True`
- [ ] `orphaned` 不与 `status` 耦合（RUNNING+orphaned 或 PAUSED+orphaned 均合法）
- [ ] `lifecycle.mark_orphans()` 幂等：重复调用不重复写入
- [ ] `lifecycle.mark_orphans()` 不误伤 COMPLETED/FAILED run
- [ ] `lifecycle.abandon_run()` 仅允许 orphaned run
- [ ] `lifecycle.retry_run()` 创建新 Run + 启动 Scheduler
- [ ] `list_runs` 返回 `orphaned` 字段
- [ ] `confirm` 在 orphaned run 上返回 409
- [ ] `BaseScheduler._try_checkpoint_recovery()` 行为与之前 `_ensure_run_started` 内联逻辑一致
- [ ] 全部现有 338 项测试不受影响

---## V0.5+ — 记忆压缩优化（P1）

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
- [x] `episode_range` 和 `original_event_refs` 已被真实值填充（通过 fold 时 ThoughtEntry.seq + ToolResult.event_seq 追踪）
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

## V0.6.1 — 反馈机制增强（Feedback Redesign）

**状态**: ✅ 已完成（2026-06-08）
**新增测试**: 334 项全通过（基线 315 + 19 项适配/新增）
**架构调整**: per-tool 连续追踪→改为最小方案（全局 counter + 防重 per-tool+error）；_parse_plan 返回 tuple

**前置依赖**: V0.6 完成
**触发**: test-logs.md 日志暴露了 Monitor 反馈"形同虚设"的问题
**目标**: 解决四个独立问题——反馈内容宽泛、无建设性建议、反馈流不到 Planner revise、反馈永不过期

### 架构审查修复（2026-06-08）

设计文档  `feedback_redesign.md`  v1.0 版经架构审查，修复了以下设计缺陷：

| # | 缺陷 | 修复 |
|---|------|------|
| P0 | `feedback_id` 含 `time.time()` → 重放/重启 ID 不同，`resolves_feedback_id` 失效 | 改基于 `run_id+category+tool+error` 的确定性 hash |
| P0 | `_failure_feedback_sent` 是内存状态 → Monitor 重启重复注入 | 改由 EventStore 折叠推导 `_has_active_feedback()` |
| P0 | CONDITION_RESOLVED 触发器太宽（任一工具成功即可） | 收紧为：仅当成功的工具与 dominant 一致时触发 |
| P1 | `dominant_tool = max(per_tool)` 未检查纯度 → 混合失败误判 | 加 80% 纯度阈值，混合模式不给工具级建议 |
| P1 | `error.split(":")[0]` 丢失 exception 内细节 | 改 `_extract_error_type()` 取异常类名，`error_detail` 保留完整消息 |
| P1 | 缺少 `FeedbackSource` 区分 operator/monitor | 加枚举 + `FeedbackInjectedPayload.source` 字段 |
| P1 | `plan()` 无 feedback 参数 → 动态规划模式反馈丢失 | `plan()` 加 `feedback` 参数 + `_PLAN_PROMPT` 加 `{feedback_section}` |
| P1 | revise 路径 high/medium/low 全塞入 → Planner 被噪音淹没 | `_get_feedback_text(for_revise=True)` 仅保留 high + operator |

### 设计方案

详见 `JAgent-docs/feedback_redesign.md`（v1.1）

### 新增/修改文件

| 文件 | 职责 |
|------|------|
| `harness/models/events.py` | `FeedbackCategory` + `FeedbackSource` 枚举 + `FeedbackInjectedPayload` 新增 8 个可选字段 |
| `harness/monitoring/run_monitor.py` | per-tool 追踪 + 统一 TOOL_FAILED/GUARDRAIL_TRIGGERED + EventStore 推导防重 + 建议生成 + 纯度检查 + 分辨率 + 确定性 hash |
| `harness/core/scheduler.py` | 结构化反馈渲染 + `for_revise` 过滤 + 过期/已解决隐藏 + source 标签 |
| `harness/core/planner.py` | `_REVISE_PROMPT` + `_PLAN_PROMPT` 加 `{feedback_section}` + `revise()`/`plan()` 加 `feedback` 参数 |
| `harness/api/routes.py` | 新增 `POST /api/v1/runs/{run_id}/feedback` Operator 手动反馈入口 |
| `tests/test_monitoring.py` | 新增 ~8 个测试用例（17 项总） |

### 任务清单

| # | 任务 | 交付物 | 验收标准 |
|---|------|--------|----------|
| 9.8 | `FeedbackCategory` + `FeedbackSource` 枚举 + Payload 增强 | `harness/models/events.py` | 新字段全 Optional，老数据零兼容成本 |
| 9.9 | RunMonitor per-tool 追踪 + 统一检测 + EventStore 防重 | `harness/monitoring/run_monitor.py` | TOOL_FAILED 和 GUARDRAIL_TRIGGERED 都走同一模式识别；`_has_active_feedback()` 从 EventStore 推导 |
| 9.10 | 建议生成器 + 纯度检查 | `run_monitor._generate_suggestion()` + `_check_and_inject_feedback()` | 纯度 ≥80% 才给工具级建议；混合失败返回 None |
| 9.11 | 分辨率信号（同工具检查） | `run_monitor.py` TOOL_COMPLETED | 仅当成功 tool == dominant_tool 时发 CONDITION_RESOLVED |
| 9.12 | 确定性 feedback_id | `run_monitor._inject_feedback()` | 相同输入始终产生相同 ID；不同输入一定不同 |
| 9.13 | 过期 + 过滤逻辑 | `scheduler._get_feedback_text()` | expires_at_seq 生效；RESOLVED 自动隐藏被解决项 |
| 9.14 | 结构化渲染 + source 标签 + revise 过滤 | `scheduler._format_feedback()` + `_get_feedback_text(for_revise=True)` | operator 反馈显示 `[Operator]` 标签；revise 模式仅保留 high |
| 9.15 | Planner revise + plan 注入 | `planner.py` | `revise()` 和 `plan()` 都接收 `feedback` 参数；两个 prompt 都含 `{feedback_section}` |
| 9.16 | Operator 手动反馈 API | `harness/api/routes.py` | `POST /api/v1/runs/{run_id}/feedback`，source=operator |
| 9.17 | 测试 | `tests/test_monitoring.py` | 17 项测试覆盖全逻辑 + 378 行回归 |

### 验收检查清单

- [x] FeedbackInjectedPayload 新增字段序列化/反序列化正确
- [x] FeedbackCategory + FeedbackSource 枚举定义完整
- [x] RunMonitor per-tool 追踪覆盖 TOOL_FAILED 和 GUARDRAIL_TRIGGERED
- [x] 3 次同工具失败 → feedback 含 affected_tool + error_type + suggestion
- [x] 混合工具失败（2 browser + 1 http）→ suggestion=None
- [x] EventStore 已有同类 active 反馈时不重复注入（重启安全）
- [x] 失败 ≥3 后仅同工具成功 → 发 CONDITION_RESOLVED
- [x] 失败 ≥3 后不同工具成功 → 不发 RESOLVED
- [x] `feedback_id` 基于内容确定，不依赖时间
- [x] 过期反馈不展示（expires_at_seq < state.seq）
- [x] 已解决反馈自动隐藏
- [x] `_get_feedback_text(for_revise=True)` 仅返回 high + operator 反馈
- [x] operator 反馈渲染含 `[Operator]` 标签
- [x] `Planner.revise()` 和 `Planner.plan()` 都收到 feedback 并注入 prompt
- [x] Operator API 写入 source=operator，走完整 EventStore→fold→Scheduler 路径
- [x] 反馈生命周期文档已注明 Event Sourcing 天然支持 checkpoint/replay/recover
- [x] 全部 334 项现有测试通过（19 项适配，0 breakage）

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

### 9.2 — Event Store 数据隔离（P1，与鉴权并行）

Event Store 当前是单实例全权限设计，多租户场景下必须隔离租户数据。不做读/写接口分离，只做**查询级租户过滤**，降低侵入性。

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 11.0 | Event Store 加 `tenant_id` 列 | `harness/storage/event_store.py` | 表结构新增列，现有查询不加过滤时返回全部数据（向后兼容） | 1d |
| 11.1 | `ScopedEventStore` 包装类 | `harness/storage/scoped.py` | 持有 `tenant_id`，所有 get/append 自动加 `WHERE tenant_id=?` | 1d |
| 11.2 | API 层注入租户上下文 | `harness/api/deps.py` + middleware | 从 JWT/Header 提取 `tenant_id`，创建 `ScopedEventStore` 下传 | 1d |

**设计原则**：不做 Reader/Writer 接口拆分（增加复杂度但当前收益有限），改为在 EventStore 上层加一个 `ScopedEventStore` 包装，自动注入 `tenant_id` 过滤和 `user_id` 检查。底层 SQLite 表加一列就行。受信组件（Scheduler、Executor）仍持有完整 Writer 权限。

### 9.3 — 用户角色与鉴权（P1）

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 12.1 | `ToolACL` 权限模型 | `harness/auth/acl.py` | 角色-工具映射；`can_invoke()` / `can_confirm()` | 1.5d |
| 12.2 | ACL 集成到 Executor | Executor 在 Guardrails 后、ToolCalled 前检查 | 拒绝时写入 `GuardrailTriggered(guardrail_id="acl_denied")` | 1d |
| 12.3 | `user_id` 贯穿调用链 | API → Scheduler → Executor → Store | API 接收 `user_id`，Scheduler 透传给 ACL | 1d |
| 12.4 | 确认权限控制 | 确认接口检查用户角色 | critical 级别仅 admin 可确认 | 0.5d |
| 12.5 | 测试 | `tests/test_acl.py` | 角色拦截、角色放行、确认权限 | 1d |

### 9.4 — 业务兼容（P2）

| # | 任务 | 交付物 | 验收标准 | 预计 |
|---|------|--------|----------|------|
| 13.1 | 业务 SKILL 示例（电商订单） | `harness/tools/skills/order_skill.py` | 对外单工具，内部多步业务逻辑 | 1.5d |
| 13.2 | 业务适配器框架 | `harness/adapter/` 模块 | 业务输入 → 内部格式 → 业务输出 | 1d |

---

## 总结优先级

| 方向 | 优先级 | 预计投入 | 关键决策 |
|------|--------|----------|----------|
| 监控 + 反馈 | **P0** — 现在就能做 | 5d | 不通过 Tool，通过 System Prompt 注入反馈；使用 on_append 回调 |
| 记忆压缩优化 | **P1** — V0.5+ | 3.5d | 改为结构化 `EpisodeSummary`；无 LLM 降级纯文本 |
| Event Store 数据隔离 | **P1** — V1.0 Phase 1 | 3d | `ScopedEventStore` 包装 + `tenant_id` 列，不做 Reader/Writer 拆分 |
| 语义记忆 + 鉴权 | **P1** — V1.0 Phase 1 | 7d | 记忆按 `user_id` 隔离；权限在 Tool Layer 前拦截 |
| 业务兼容 | **P2** — V1.0 Phase 2 | 2.5d | 通过 SKILL + 适配器封装业务流程 |

---

---

## V0.6+ — 架构加固（Architecture Hardening）

**状态**: ✅ 已完成（2026-06-06）

**前置依赖**: V0.6 完成
**目标**: 修复上次会话识别的 5 个 P0/P1 架构漏洞

### 修复清单

#### P0-1: Skill 绕过 Tool Layer ✅

**问题**: `Skill.build_fn()` 返回的 `skill_fn` 内部 step 函数直接调 `tool_fns["xxx"]()`，绕过 `ToolExecutor.execute()` 的完整 8 步流程（Guardrails / 幂等键 / ToolCalled 事件 / 确认流程 / 超时控制）。

**修复**: `Skill.build_fn()` 新增可选参数 `executor` 和 `tool_defs_provider`。当传入 executor 时，step 函数收到的 `tool_fns` 中的每个函数被包裹为通过 `executor.execute()` 的代理函数：
- `_make_executor_wrapper()` 创建 async wrapper，通过 `current_run_id` contextvar 获取 run_id，调用 `executor.execute()` 获取 `ToolExecutionResult`，返回 `result.output`
- 向后兼容：不传 executor 时行为不变
- 调用方示例：`registry.register(skill.definition, skill.build_fn(registry.list_tool_fns, executor, registry.list_tool_defs))`

**验收**: 内层工具调用写入 ToolCalled/ToolCompleted 事件；现有 Skill 测试不受影响。

#### P0-2: 工具输出无语义验证 ✅

**问题**: `ToolExecutor` Step 7 完成后只检查 `success=False` 一个软失败模式，`output_schema` 声明了但从未被校验。HTTP 403、CAPTCHA、空结果全部被当成 `ToolCompleted`。

**修复**: Step 7 写入 ToolCompleted 前，当 `tool_def.output_schema` 非空时，执行 `jsonschema.validate()`：
- 验证失败 → 写入 ToolFailed 事件，返回 FAILED 状态
- `output_schema` 为空时跳过（向后兼容）

#### P0-3: Agent 无终止策略 ✅

**问题**: 只有 `max_iterations=50` 一个兜底，Agent 可重复相同工具+参数+输出 50 次。

**修复**: 将循环检测从 Scheduler 移至 **RunMonitor**（受信监控组件），遵循其"软反馈 + Agent 自愈"的设计模式：
- RunMonitor 在 `_on_event_impl` 中通过 `TOOL_CALLED`+`TOOL_COMPLETED` 事件关联追踪 `(tool_name, input_hash, output_hash)` 签名
- 连续 3 次相同签名 → 写入高优先级 `FeedbackInjected`（每多 3 次重复再追加一条）
- Scheduler 在 THINK 前拉取 `state.feedbacks` 注入 AgentKernel System Prompt
- Agent（LLM）看到 feedback 后**自行修正行为**
- 最终兜底：`max_iterations`（SchedulerConfig 默认 50）
- Scheduler 不做硬终止，`SchedulerConfig` 不新增字段

#### P1-4: 受信组件约束未完全落地 ✅

**修复**:
- **side_effects 运行时消费**: Step 7 完成时，当 `tool_def.side_effects` 非空时记录 `[sidefx] tool=X side_effects=[...]` 日志，使声明字段在运行时可见
- **Sandbox.invoke() 信任边界文档**: 添加 docstring 警告 `Sandbox.invoke()` 绕过 ToolExecutor，调用方应优先通过 `executor.execute()`
- **Skill 绕过修复**: 减少直接调用 `Sandbox.invoke()` / `tool_fns` 的路径

**剩余（V1.0）**: EventStore `append_event` 类型白名单、容器级沙盒隔离、Guardrails 强制路由。

#### P1-5: 幂等键字段验证 ✅

**验证结果**: 实现正确 — `IdempotencyKeyGenerator.compute()` 仅使用 `idempotency_key_fields` 指定的字段子集计算哈希，不使用全量 input。各工具定义的 `idempotency_key_fields` 覆盖了所有语义相关的输入字段。无需修改。

#### P1-6: 声明式事件前置条件（`depends_on`）✅

**问题**: `DependencyGuardrail` 只能通过 `guardrails[].config.required_events` 配置，依赖关系散落在 guardrail config 中，类型不安全。且工具声明了 `depends_on` 时，`GuardrailRunner` 不自动执行——ORCHESTRATE_DEF 先例已暴露此问题。

**修复**:
- `ToolDefinition` 新增 `depends_on: list[DependencyConstraint]` 字段
- 新增 `DependencyConstraint` Pydantic 模型，支持 `event_type` + `payload_filter` + `message`，类型安全的声明式依赖
- `GuardrailRunner.run()` 在 SchemaGuardrail 后自动检查 `tool_def.depends_on`，独立于 `guardrails[]` 列表
- `DependencyGuardrail.check()` 优先读取 `tool_def.depends_on`（推荐），fallback 到 `config["required_events"]`（向后兼容）
- `ORCHESTRATE_DEF` 声明 `depends_on=[DependencyConstraint(event_type="RunStarted")]`
- `_matches_filter` 重构为 `DependencyGuardrail._matches_payload` 静态方法，类职责内聚

**验收**: 5 个新测试覆盖 depends_on 全路径（缺失事件拦截 / 存在事件放行 / payload_filter 匹配 / payload_filter 不匹配 / 优先于 required_events）。

### 验收检查清单

- [x] Skill 内层工具调用不再绕过 Tool Layer（通过 `_make_executor_wrapper` 包裹）
- [x] ToolExecutor Step 7 对 `output_schema` 非空的工具执行 `jsonschema.validate()`
- [x] RunMonitor 检测连续 3 次相同签名并写入 FeedbackInjected（每多 3 次追加一条）
- [x] `side_effects` 字段在运行时被消费（日志记录）
- [x] 幂等键实现确认只使用 `idempotency_key_fields`
- [x] 声明式前置条件：`DependencyConstraint` + `depends_on` + GuardrailRunner 自动校验
- [x] 全部 271 项测试通过（0 回退）

---

## V1.0 — 分析平台（Analysis Platform）✅ 后端完成

**前置依赖**: V0.6+ 完成（2026-06-06）
**目标**: 让 Event Store 中已有的事件数据产生对人（开发/运维/业务）有价值的可观测性信息

### 核心洞察

Event Store 中已有完整的事件流（AgentThought / ToolCalled / ToolCompleted / GuardrailTriggered …），
但除了 Scheduler 折叠状态驱动 Agent 循环外，没有任何消费端利用这些数据。
分析平台 = 写新的消费端，把事件流变成对人有用的信息，**不改任何现有组件**（纯消费层）。

### 设计原则

- **纯消费层**: AnalysisService 只读 Event Store，不写任何事件
- **操作锚点预埋**: `ToolTraceItem.retryable` 携带 `eligible` / `ineligible_reason` / `suggested_backoff_ms` / `requires_input_modification` 四维信息，前端直接判断是否显示操作按钮
- **读写分离**: 分析（读）→ `harness/analysis/`；操作（写）→ `harness/operations/`（将来），API 路由已预留 501 占位
- **时间窗口**: 所有聚合端点支持 `?since=&until=`，默认最近 24 小时
- **分页**: `?limit=&cursor=` 游标分页，避免一次返回体过大

### 数据模型（`harness/analysis/schemas.py`）

| 模型 | 用途 |
|------|------|
| `RetryableInfo` | 操作锚点多维信息（不限于布尔值） |
| `ParsedEventDetail` | 单事件完整展开 + 操作锚点字段 |
| `ToolTraceItem` | 按 tool_call_id 关联的完整工具生命周期 |
| `DashboardOverview` | 全局概况（Run 数 / 事件数 / Token / 成功率） |
| `ToolStatItem` | 工具维度统计 |
| `GuardrailStatItem` | Guardrail 拦截统计 |
| `RunAnalysisSummary` | 单 Run 概要 |
| `TimelineResponse` | 分页事件时间线 |
| `ToolTracesResponse` | 单 Run 的完整工具 Trace 列表 |

### API 端点

| 端点 | 方法 | 响应 | 说明 |
|------|------|------|------|
| `/api/v1/analysis/dashboard` | GET | `DashboardResponse` | 全局概况卡片数据，支持 `?since=&until=` |
| `/api/v1/analysis/tools` | GET | `ToolStatsResponse` | 所有工具使用统计，支持 `?since=&until=` |
| `/api/v1/analysis/guardrails` | GET | `GuardrailStatsResponse` | Guardrail 拦截统计，支持 `?since=&until=` |
| `/api/v1/analysis/runs/{run_id}` | GET | `RunAnalysisSummary \| 404` | 单 Run 概要（轻量） |
| `/api/v1/analysis/runs/{run_id}/timeline` | GET | `TimelineResponse` | 分页事件时间线，`?limit=&cursor=` |
| `/api/v1/analysis/runs/{run_id}/tool-traces` | GET | `ToolTracesResponse` | 单 Run 完整工具 Trace 列表 |
| `/api/v1/operations/retry` | POST | `501` | 将来操作层预留 |

### 新增/修改文件

| 文件 | 类型 | 职责 |
|------|------|------|
| `harness/analysis/__init__.py` | 新增 | 包导出 |
| `harness/analysis/schemas.py` | 新增 | 9 个 Pydantic 响应模型 |
| `harness/analysis/service.py` | 新增 | AnalysisService 聚合查询引擎（6 个查询方法） |
| `harness/api/analysis_routes.py` | 新增 | 6 个 GET + 1 个 POST 占位端点 |
| `harness/api/app.py` | 修改 | 注册 analysis_router |

### 验收检查清单

- [x] 架构方案设计完成
- [x] 后端 3 个模块 + 6 个 API 端点就绪
- [x] 分析 API 返回完整 payload 字段，前端可直接消费
- [x] 操作锚点通过 `RetryableInfo` 四维字段预埋
- [x] 操作路由 `POST /api/v1/operations/retry` 返回 501 占位
- [x] 时间窗口过滤：所有聚合端点支持 `?since=&until=`
- [x] 游标分页：timeline 端点支持 `?limit=&cursor=`
- [x] 前端可视化由用户自行实现（Supabase UI 主题）
- [x] 现有 271 项测试不受影响

---

## V0.7 — Planner-Executor + DAG 执行引擎

**前置依赖**: V1.0 分析平台完成（✅）
**目标**: 将当前串行 think→act→observe 循环拆分为 Planner（规划）+ Executor（DAG 并行执行），解决多轮失忆、串行瓶颈、LLM 认知负荷过重三个核心问题。

### 核心架构变更

```
旧循环:                      新循环:
think → act(串行) → observe  plan → execute(并行) → observe → revise
  ↑ 每轮 1 个 think           ↑ 每轮 N 步 plan，同层并行
  ↑ LLM 既要规划又要执行       ↑ LLM 只负责战略（Plan），系统负责战术（DAG 执行）
```

### 设计原则

1. **规划与执行分离**: Planner（非受信，调 LLM）只输出结构化 JSON Plan；DagExecutor（受信）按拓扑序并行执行
2. **DAG 拓扑执行**: 同层独立步骤通过 `asyncio.gather()` 并行，消除串行瓶颈
3. **系统强制注入状态**: Revise 前由受信组件注入不可压缩的 DAG 进度摘要，防止 LLM 失忆
4. **渐进迁移**: 新 `PlanningExecutorScheduler` 与旧 `AgentLoopScheduler` 并存，5 个 Phase 逐步切换

### 事件扩展

| 事件类型 | 写入方 | 关键字段 |
|----------|--------|----------|
| `PlanCreated` | Scheduler | `plan_id, intent, steps_summary, layer_count` |
| `DagStepStarted` | DagExecutor | `plan_id, step_id, tool_name, depends_on` |
| `DagStepCompleted` | DagExecutor | `plan_id, step_id, output_summary` |
| `DagStepFailed` | DagExecutor | `plan_id, step_id, error, retryable` |
| `PlanRevised` | Scheduler | `plan_id, revision_reason, remaining_steps_summary` |
| `PlanCompleted` | Scheduler | `plan_id, completed_steps, total_layers, summary` |
| `PlanFailed` | Scheduler | `plan_id, completed_steps, total_layers, final_error` |

### 风险管理（来自架构审查）

| # | 风险 | 缓解方案 | 对应 Phase |
|---|------|----------|-----------|
| R1 | Plan 解析格式异常 | PlanParser 自动重试 2 次 + 降级旧串行路径 | P3 |
| R2 | 上游 output 膨胀上下文 | `upstream_selectors` 字段路径提取 + output_summary 截断 | P2 |
| R3 | Revise 时 LLM 失忆 | 受信组件注入不可压缩 DAG 状态摘要 | P2 |
| R4 | 动态条件分支 | `dynamic: true` 标记 → 退化为逐层串行 + 每次 revise | P4 |
| R5 | Guardrail 盲区（危险组合） | PlanGuardrail 增加 `_check_dangerous_combinations` + `ParallelGuardrail` | P3 |
| R6 | fold 规则未定义 | 事件按白名单分级 fold（不可 fold / 摘要化 / 可跳过） | P1 |

### 数据模型（`harness/models/plan.py`）

```python
@dataclass
class DagStep:
    id: str
    tool: str
    input: dict[str, Any]
    depends_on: list[str] = None       # 依赖的上游 step id
    description: str = ""
    upstream_selectors: dict[str, str] = None  # 如 {"s1": "weather.summary"}
    branches: dict | None = None       # 预留条件分支，V2
    max_parallel: int = 10            # 同层并行度上限

@dataclass
class DagPlan:
    intent: str
    steps: list[DagStep]
    dynamic: bool = False              # true=走逐层串行+revise

    def topological_sort(self) -> list[list[str]]:
        """Kahn 算法拓扑排序，返回按层分组的 step id"""
        ...
```

### 新增/修改文件

| 文件 | 类型 | 职责 |
|------|------|------|
| `harness/models/plan.py` | 新增 | DagStep、DagPlan 数据模型 |
| `harness/core/planner.py` | 新增 | Planner 类（LLM 生成 Plan + 重试 + 降级） |
| `harness/core/dag_executor.py` | 新增 | DagExecutor 类（拓扑执行 + 并行 + 结果摘要化） |
| `harness/core/scheduler.py` | 修改 | 新增 `PlanningExecutorScheduler` 类 |
| `harness/models/events.py` | 修改 | 新增 7 个事件类型 + Payload 模型 |
| `harness/core/fold.py` | 修改 | 新增 plan_history 字段 + fold 白名单规则 |
| `harness/tools/registry.py` | 修改 | ToolDefinition 新增 `dangerous_with`、`max_parallel` |
| `harness/core/__init__.py` | 修改 | 导出新类 |

### 迁移阶段

| Phase | 周期 | 交付物 | 验收标准 |
|-------|------|--------|----------|
| **P1** | 1d | 数据模型 + 事件类型 + fold 白名单 | 事件写入/读取正确；fold 按白名单分级 |
| **P2** | 2d | DagExecutor（拓扑排序 + 并行执行 + 状态注入 + 摘要化） | 同层并行正确；依赖等待正确；上游选择器工作 |
| **P3** | 2d | Planner（LLM Plan 生成 + 重试 2 次 + 降级 + PlanGuardrail 增强） | JSON Plan 解析/校验/重试/降级全路径 |
| **P4** | 2d | PlanningExecutorScheduler（Plan→Execute→Revise 循环 + 动态标记） | 完整 5~8 步任务跑通；确认/暂停/恢复兼容 |
| **P5** | 1d | 旧 Scheduler 退役 + 回归测试全量覆盖 | 297+ 测试全通过 |

### 验收检查清单

- [x] DagPlan 拓扑排序正确（Kahn 算法，检测有环）
- [x] DagExecutor 同层并行执行（asyncio.gather）
- [x] 上游结果摘要化（upstream_selectors 字段路径提取 + 默认截断 200 chars）
- [x] 系统强制注入 DAG 状态（`【系统状态 - 不可折叠】` 标记）
- [x] Planner 解析失败自动重试 2 次 + 降级旧串行路径
- [x] PlanGuardrail 检测危险组合（dangerous_with）和并行超限（max_parallel）
- [x] 动态 Plan（dynamic: true）退化为逐层串行 + 每步 revise
- [x] 事件 fold 白名单分级（不可 fold / 摘要化 / 可跳过）
- [x] 全部 297 项测试通过，旧 Scheduler 可退役
- [x] 确认/暂停/恢复流程在 Scheduler 新循环中正常工作

---

---

## Known Technical Debt

### `harness/core/fold.py` — tool_calls/feedbacks 不截断

`fold_events` 的 `CONTEXT_COMPRESSED` 分支（第 201-208 行）截断 `thought_history`
和 `tool_results`，但未处理 `tool_calls`（第 129 行）和 `feedbacks`（第 238 行）。
超长 Run 下这两个 list 持续增长。

**影响**: 单 Run 内存峰值偏高。Run 结束后自然 GC 释放。
**修复方向**: 在压缩处理分支补充：
```python
state.tool_calls = state.tool_calls[-keep:]
state.feedbacks = state.feedbacks[-keep:]
```

### `harness/storage/event_store.py` — _seq_locks 计数淘汰可能漏锁

每 50 次写入清理一次未锁定的锁（第 171-174 行）。锁若被持有跨越计数窗口则泄漏。

**影响**: 极端情况下 `_seq_locks` 累积死锁对象。
**修复方向**: 改为基于 `time.monotonic()` 的超时淘汰（60s TTL）。

### `harness/tools/guardrails.py` — _call_history 类级字典无生产清理

`RateLimitGuardrail._call_history` 是类级 `dict[str, list[float]]`，key 永久增长。
`reset()` 仅测试中调用。

**影响**: 长期运行的服务器中 key 数量理论上无界。
**修复方向**: 改为实例级存储（per-Scheduler 实例），或加定时清理。

---

*基于 `harness_v2.1.md` 架构方向生成*
*分层推进，禁止跨层，每层交付物需验收后方可进入下一层*
