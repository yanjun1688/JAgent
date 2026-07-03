# 架构问题审查报告

> **日期**: 2026-06-07
> **范围**: Harness v2.1 V0.7 Planner-Executor 架构审查 + 代码质量全面清理
> **状态**: 改0/改1/改5/改14 + 14项代码质量修复已完成，改2/改4 待做

---

## 一、背景

Harness 架构从 V0.6（串行 think→act→observe）演进到 V0.7（Plan→Execute→Revise）。核心变化是把"编排"从 LLM 每轮 Think 里抽出来，交给 Planner（LLM 生成 DAG Plan）+ DagExecutor（拓扑排序并行执行）驱动。

但测试阶段发现以下问题：

---

## 二、问题清单

> 状态：✅ = 已修复  ❌ = 未修复  ⚠️ = 部分修复

### 🔴 P0 — 运行时崩溃 / 数据损坏

| # | 文件 | 行 | 问题 | 影响 | 状态 |
|---|------|-----|------|------|------|
| 1 | `scheduler.py` | 419, 560 | `is_paused()` 在无 pause 历史的 run 上调用时，`_pause_events.get(run_id)` 返回 `None`，随后 `None.is_set()` 抛出 `AttributeError`。两个 class 都有此问题（AgentLoopScheduler + BaseScheduler） | **崩溃** | ✅ 加 None 检查 |
| 2 | `fold.py` | 268-282 | `DAG_STEP_STARTED`、`DAG_STEP_COMPLETED`、`DAG_STEP_FAILED` 都在 `latest_plan["steps"]` 执行 `append`，导致每个 step 有两条重复记录（"started" + "completed"）。前端消费 `plan_history` 得到错误数据 | **数据损坏** | ✅ 按 `step_id` 更新 |
| 3 | `event_store.py` | 142-164 | `_seq_locks` dict 只增不减，每次 `append_event` 产生一个新 lock 但从不移除。生产环境运行数天/数周后可能积累数百万个无用的 lock 对象 | **内存泄漏** | ✅ 写入后 `not locked()` → `pop` |
| 4 | `event_store.py` | 全局 | **seq 冲突"静默污染"**：并发写入不再报 PK 冲突，但同一 seq 被多个事件共享（如 seq=6 同时是 s1/s2/s3 的 DagStepStarted）。`INSERT OR REPLACE` 或移除了 UNIQUE 约束导致静默覆写。事件流失去"一个 seq 唯一对应一个事件"的保证，前端时间线无法归因，trace/审计回放完全失效 | **数据永久损坏** | ✅ `asyncio.Lock` + 串行写 |

### 🟠 P1 — 功能缺陷 / 设计错误

| # | 文件 | 行 | 问题 | 影响 | 状态 |
|---|------|-----|------|------|------|
| 5 | `harness/__init__.py` | 全文件 | V0.7 全部新类型未在顶层包导出。`from harness import PlanningExecutorScheduler` 抛出 `ImportError` | **不可用** | ✅ 已补，缺 `BaseScheduler` |
| 6 | `serve.py` | 169 | `token_limit=1000` 过低，Mock 模式空转压缩 | **配置错误** | ✅ `token_limit=0` |
| 7 | `http_request.py` | 40, 68 | `max_response_bytes` 默认值从 1MB 降到 4KB | **破坏性变更** | ✅ 改回 64KB |
| 8 | `tests/` | — | V0.7 组件无专用测试 | **测试缺失** | ✅ 新增 `test_planner.py`(139行) + `test_dag_executor.py`(121行) |
| 9 | `planner.py` | `_parse_plan` | Revise 参数丢失：LLM 用 `parameters` 而非 `input`，静默当作空 dict | **连环死亡** | ✅ `parameters`→`input` 兼容映射 |
| 10 | `planner.py` | revise 策略 | Revise 替代方案绕过 Guardrail：失败原因未分类 | **无效执行** | 🚫 已明确暂缓（用户决策，观察一期线上表现后再定） |
| 11 | `scheduler.py` | plan-execute | 无总结回答轮，`PlanRevised(empty)` → 直接 `RunCompleted` | **体验灾难** | ✅ `_finalize_with_summary()` |
| 12 | `planner.py` + `context_manager.py` | 压缩 | Context 压缩丢失 Plan 参数（10:1），间接导致问题 9 | **信息丢失** | ✅ 压缩白名单保留 Plan 步骤 |

### 🟡 P2 — 逻辑问题 / 坏味道

| # | 文件 | 行 | 问题 | 状态 |
|---|------|-----|------|------|
| 13 | `scheduler.py` | 982 | `_generate_answer()` 直接访问 `self.planner.llm` | ❌ |
| 14 | `scheduler.py` | — | `AgentLoopScheduler` 未继承 `BaseScheduler`，~130行重复 | ✅ 已重构：AgentLoopScheduler(BaseScheduler)继承，净减78行 |
| 15 | `scheduler.py` | 764-960 | `_execute_static_plan` 和 `_execute_dynamic_plan` ~80% 重复 | 🚫 已明确暂缓（两者 revise 策略/事件写入时机/返回类型不同，提取收益有限，暂不处理） |
| 16 | `scheduler.py` | 426, 518 | `_fail()` 硬编码中文 | ✅ 全英文 |
| 17 | `planner.py` | 67-79 | `_REVISE_PROMPT` 缺原始 `intent` | ✅ 已加 |
| 18 | `scheduler.py` | 75-80 | 类注释过时 | ✅ AgentLoopScheduler 注释已更新；PlanningExecutorScheduler 等后续 |
| 19 | `planner.py` + `dag_executor.py` | `max_parallel` | Plan 语法限制导致层数膨胀 | ✅ 改为信号量控制 |
| 20 | `event_store.py` | 163-164 | `_seq_locks` 清理 TOCTOU 竞争：A释放→B复用→A pop 误删 | ⚠️ 不崩溃但锁频繁重建 |

### 🔵 P3 — 小问题 / 代码规范

| # | 文件 | 行 | 问题 | 状态 |
|---|------|-----|------|------|
| 21 | `tests/test_api.py` | 272-276 | 函数体内 import | ✅ 已移到文件头 |
| 22 | `event_store.py` | 120 | `max_retries` 参数未用 | ✅ 已改名 `_max_retries` |
| 23 | `scheduler.py` | 52 | `execution_mode` 未用 | ✅ 已移除 |
| 24 | `agent_kernel.py` | 101-115 | `MockAgentKernel` docstring 中文 | ✅ 已英文 |

### ⚪ 功能退化风险

| 风险 | 说明 |
|------|------|
| Orchestrator 被移除 | 旧 `Orchestrator.execute()` 返回 `{"status", "completed_steps", "results", "error"}` dict；新的 `PlanningExecutorScheduler.run()` 返回 `RunState`。依赖此结构的调用方直接报错 |
| 事件类型变更 | `ORCHESTRATION_STARTED` / `STEP_COMPLETED` / `ORCHESTRATION_COMPLETED` 被 `PLAN_CREATED` / `DAG_STEP_*` / `PLAN_COMPLETED` 替换，任何基于旧事件流的消费者需同步迁移 |

---

## 三、已执行清理

### Phase 1 — 删除 V0.4 Orchestrator

| 文件 | 操作 | 行数 |
|------|------|------|
| `harness/core/orchestrator.py` | **删除** | -336 |
| `harness/models/events.py` | 删 5 个 EventType + 5 个 Payload 模型 + 5 条 PAYLOAD_MODEL_MAP | -15 |
| `harness/core/fold.py` | 删 5 个 import + 2 个 RunState 字段 + 5 个 match case | -30 |
| `harness/__init__.py` | 删 import + 9 条 `__all__` 导出 | -14 |
| `tests/test_orchestrator.py` | **删除** | -473 |
| `tests/test_guardrails_v04.py` | OrchestrationStarted → PlanCreated | -3 |
| `tests/test_event_store.py` | Orchestrator 事件 → DAG 事件 | -11 |

### Phase 2 — Bug 修复

| 文件 | 行 | 修复 |
|------|------|------|
| `harness/core/scheduler.py` | 697 | `return (state, failures)` tuple → `return state`，删 2 行死代码 |
| `harness/core/scheduler.py` | 415-417, 558-560 | `is_paused()` 加 None 检查 |
| `harness/core/fold.py` | 233-262 | DAG_STEP 事件改为按 `step_id` 覆盖更新 |
| `harness/storage/event_store.py` | 142-164 | `asyncio.Lock` 保护 seq 分配 + 锁清理逻辑 |
| `harness/core/dag_executor.py` | 105-183 | `_execute_layer` 串行写 STARTED→gather 执行→串行写 COMPLETED/FAILED |
| `harness/core/planner.py` | 338-342 | `parameters`→`input` 兼容映射 |
| `harness/core/scheduler.py` | 985-1001 | `_finalize_with_summary()` 生成总结回答 |
| `harness/core/context_manager.py` | 253-270 | 压缩白名单保留 PlanCreated/PlanRevised 步骤详情 |

### Phase 3 — 入口点重构

| 文件 | 修改 |
|------|------|
| `harness/api/deps.py` | `HarnessAPI.start_run` 直接创建 `PlanningExecutorScheduler`，`kernel_factory` 替换为 `llm_client` + `registry` |
| `harness/api/serve.py` | 删 `_planning_start_run` 函数 + `api.start_run = _planning_start_run` 猴子补丁 |

### Phase 4 — Scheduler 层次重构

| 改动 | 说明 | 行数变化 |
|------|------|---------|
| `AgentLoopScheduler` 改为继承 `BaseScheduler` | 删除 run/pause/cancel/resume/is_active/is_paused 6 个重复方法，__init__ 调用 super()。保留 _fail() override（用词"thought(s)" vs 基类"planning round(s)"），保留 _wait_for_resume（确认暂停 vs 普通暂停差异），保留内联 pause 代码（含计时和 post-resume 刷新）。类定义顺序重排 | **净减78行** |
| 新增 5 个继承验证测试 | `TestInheritanceFromBaseScheduler` | +58 行 |

### 清理后验证

```
tests\ — 284 passed in 28.63s (Phase 2-4 cleanup)
tests\ — 315 passed in 27.54s (after scheduler hierarchy refactor, +5 inheritance tests)
```

---

### ARCH-1 — 自然边界压缩对齐

| 文件 | 改动 | 状态 |
|------|------|------|
| `fold.py` | `RunState` 新增 `plan_boundary_seqs` 字段；`PLAN_COMPLETED`/`PLAN_FAILED` handler 记录当前 seq 到列表 | ✅ |
| `context_manager.py` | `select_compression_window` 紧急压缩路径：计算 `mid` 后检查最近 plan 边界；若距离 `< span×20%`，则将 mid 对齐到边界之后 | ✅ |

**效果**: 压缩窗口不再在 plan 执行中间截断，对齐到 `PLAN_COMPLETED`/`PLAN_FAILED` 的自然章节边界。向后兼容——无 plan 边界时行为不变。

---

## 四、待做项（按优先级）

### 改0 — 修复现存 P0 bug ✅ 已完成

| # | 原始问题 | 文件 | 修复方案 | 状态 |
|---|---------|------|----------|------|
| 1 | `is_paused()` 崩溃 | `scheduler.py:416-418,559-561` | 已含 None 检查 | ✅ 无需修改（代码已修复） |
| 2 | DAG_STEP fold 重复 | `fold.py:233-247` | 改为按 `step_id` upsert，不再 append | ✅ 已修复 + 4 个新测试 |
| 3 | seq 分配竞态 + 锁泄漏 | `event_store.py` | 追加 `asyncio.Lock` per run_id + 写入后清理 | ✅ 已修复 + 2 个新测试 |
| 4 | seq 冲突 | `event_store.py` | UNIQUE 约束完好，追加 Lock 保证原子性 | ✅ 已修复 + 2 个新测试 |

### 改1 — 分离执行和事件写入 ✅ 已完成

**改动**:
- `_run_step` → `_execute_step_only`，移除所有 `append_event`
- `_execute_layer`: 先串行写 DAG_STEP_STARTED → `gather` 执行 → 串行写 COMPLETED/FAILED
- 配合改0：`event_store.py` `asyncio.Lock` per run_id 保证 seq 原子性

**效果**: 事件流确定、seq 严格递增、无交错、无孤立事件。

### 改3 — Revise 策略修复 ✅ 部分完成

**已完成**:
| 改进 | 文件 | 状态 |
|------|------|------|
| `parameters`→`input` 兼容映射 + 缺失时 reject | `planner.py:_parse_plan` | ✅ |
| `_REVISE_PROMPT` 加入 `## Original User Intent` | `planner.py:67-79` | ✅ |
| 最终总结回答 `_finalize_with_summary` | `scheduler.py` (新增方法) | ✅ |
| 压缩白名单: plan_history 纳入摘要 | `context_manager.py:_generate_summary` | ✅ |

**统一决定暂缓（一期线上观察后再定）**:
- Revise 失败原因分类 (schema_error / tool_unavailable / 替代方案预校验) — 见 #10
- Predictive Guardrails (PlanRiskReport + self-correction) — 新功能，需更多设计评估

### 改5 — max_parallel 信号量控制 ✅ 已完成

**改动**:
| 文件 | 改动 | 状态 |
|------|------|------|
| `dag_executor.py` | 初始化时创建 `asyncio.Semaphore(max_parallel)`，执行时 `acquire`/`release` | ✅ |
| `planner.py:PlanGuardrail._check_max_parallel` | 硬错误 → warning 级别 | ✅ |

### 改2 — PlanStateMachine（P1）🔜 待做

结构化 Plan 执行状态，支持 dump/restore。

### 改4 — Debug APIs（P2）🔜 待做

`/state-snapshot`, `/plan-diff`, `/trace` 三个端点。

### P1/P2/P3 修补清单

> 说明：编号沿用"二、问题清单"的全局编号。#9(parameters→input)、#11(总结回答)、#12(压缩白名单) 已含在改3中。#10 已明确暂缓（非待修）。#20 在 P2 中为 TOCTOU。

| # | 原始问题 | 优先级 | 改动 | 状态 |
|---|---------|--------|------|------|
| 5 | 顶层导出缺失 | P1 | `harness/__init__.py` 补充 V0.7 类型导出 | ✅ 已修复 |
| 6 | Mock 模式空转压缩 | P1 | `serve.py:169` `token_limit=0` 禁用压缩 | ✅ 已修复 |
| 7 | max_response_bytes 过小 | P1 | `http_request.py:40,68` 改回 64KB | ✅ 已修复 |
| 8 | V0.7 测试缺失 | P1 | 新建 V0.7 专用测试文件：`test_planner.py` + `test_dag_executor.py` | ✅ 已修复 (+26 tests) |
| 10 | Revise 失败未分类 | P1 | `planner.py:revise()` 加 `_classify_failures()` 预处理 + prompt 约束禁止跨工具替代 | 🚫 已明确暂缓（见改3"统一决定暂缓"） |
| 13 | `_generate_answer` 紧耦合 | P2 | `scheduler.py` 委托给 `Planner.generate_answer()` | 🔜 待做 |
| 14 | 俩调度器重复 | P2 | `AgentLoopScheduler` 改为继承 `BaseScheduler` | ✅ 已完成（净减78行） |
| 15 | 静态/动态 plan 执行重复 | P2 | 提取公共方法 | 🚫 已明确暂缓（收益有限，不增加复杂度） |
| 16 | 中文硬编码 | P2 | `_fail()` 改为英文 | ✅ 已修复 |
| 17 | revise prompt 缺 intent | P2 | `_REVISE_PROMPT` 加入 `## Original User Intent` | ✅ 已修复 |
| 18 | 过时注释 | P2 | 更新 scheduler.py 类注释 | ✅ AgentLoopScheduler 已完成，PlanningExecutorScheduler 等后续 |
| 19 | `max_parallel` 层数膨胀 | P2 | 见改5 — 信号量控制 | ✅ 已修复 |
| — | `_seq_locks` TOCTOU | P2 | 批量定时清理（每50次append）替代 check-then-pop | ✅ 已修复 |
| 20 | 函数体内 import | P3 | `test_api.py` import 移到文件头 | ✅ 已修复 |
| 21 | `max_retries` 未使用 | P1 | 实现 seq 冲突重试循环（最多 `_max_retries+1` 次），成功后 break | ✅ 已修复（P1-1） |
| 22 | `execution_mode` 未使用 | P3 | 从 `ThinkResult` 移除 | ✅ 已修复 |
| 23 | `MockAgentKernel` docstring | P3 | 中文→英文 | ✅ 已修复 |
| 24 | `on_append` 无错误隔离 | P2 | `try/except` 包裹每个回调，单独 logging | ✅ 已修复（P2-1） |
| 25 | `_check_max_parallel` 返回值永远空 | P2 | 移除 `warnings` 变量，直接 `return []` | ✅ 已修复（P2-2） |
| 26 | `_get_feedback_text` 未复用 | P2 | `_run_loop` 内联代码替换为 `self._get_feedback_text(state)` | ✅ 已修复（P2-3） |
| 27 | `_fail` 措辞不一致 | P2 | `PlanningExecutorScheduler` 覆写 `_fail`，用 "execution round(s)" | ✅ 已修复（P2-4） |
| 28 | breaker 重复3次 | P2 | 提取 `_breaker_tripped()` 方法，统一熔断检查 | ✅ 已修复（P2-5） |
| 29 | `models/__init__.py` 遗漏 V0.7 导出 | P1 | 新增 8 个事件模型 + `EpisodeSummary` + `DagPlan`/`DagStep` | ✅ 已修复（#1） |
| 30 | `_generate_answer` 违反封装 | P1 | 委托给 `Planner.generate_answer()`，不再访问 `self.planner.llm` | ✅ 已修复（#2） |
| 31 | `build_dag_status_text` 中文硬编码 | P2 | 全部改为英文描述 | ✅ 已修复（#4） |
| 32 | `DagPlan`/`DagStep` 使用 `@dataclass` | P2 | 改为 Pydantic `BaseModel`，跨边界契约有 Schema 验证 | ✅ 已修复（#5） |
| 33 | `EpisodeSummary` 导入路径冗余 | P3 | 从 `harness.core.agent_kernel` → `harness.models.events` | ✅ 已修复（#6） |
| 34 | `RateLimitGuardrail._call_history` 污染 | P3 | 添加 docstring 提醒 `reset()` | ✅ 已修复（#7） |
| 35 | `plan.py:upstream_outputs` key 命名与 `dag_executor.py` 解析器不匹配 | P0 | `upstream_outputs()` 以 `f"{dep_id}_result"` 为 key（如 `"s1_result"`），但 `_resolve_variables_in_input()` 查找裸 `"s1"`。导致所有 `$stepId.field` 变量引用永不相交，全部以字面量字符串传递给下游工具。影响：文件写入大小错误（12B 应为 31B）、SchemaGuardrail 拦截变量引用字符串、下游工具收到错误数据。 | ✅ 已修复（Phase 1: 修 key + 提交流程中） |
| 36 | `dag_executor.py:_execute_step_only` 将 CONFIRMATION_NEEDED 折叠为 error 导致确认死循环 | P0 | **代码层已修复**：`_execute_step_only`（L330-336）和 `_execute_layer`（L246-266）已正确识别 `confirmation_needed` 状态并抛出 `PlanSuspended`。残留问题：revise 机制可能生成相同 plan 导致重新触发确认。**现已由 `max_confirm_retries` 兜底**（`_run_tool_call` + `_execute_dynamic_plan` + `_execute_static_plan` 三处确认循环均有上限）。完整根治需 revise 机制学会识别"此操作必然触发确认"并跳过而非重新 plan。 | ✅ 代码层已修复 + `max_confirm_retries` 兜底；revise 改进待下迭代 |
| 37 | `run_monitor.py:_on_event_impl` 未监听 DAG_STEP_FAILED，监控在 DAG 路径下形同虚设 | P0 | `run_monitor.py:88` 的事件过滤只包含 `TOOL_FAILED` 和 `GUARDRAIL_TRIGGERED`，但 DAG 执行路径在失败时写入的是 `DAG_STEP_FAILED`（`dag_executor.py:167-173`），不写 `TOOL_FAILED`。导致 RunMonitor 的 `_consecutive_failures` 计数器在 DAG 路径下始终为 0。日志中每个 Run 的清理行（`[MONITOR] Cleaned up run ... consecutive_failures=0`）即使有失败也全是 0，包括连续失败的 Run `302d6684`（实际 2 次 guardrail_blocked）、Run `fb656fc4`（14 轮循环失败）。监控在 DAG 路径下完全不工作。 | ✅ `_on_event_impl` 事件过滤加入 `DAG_STEP_FAILED`；增加 2 个测试覆盖 |
| 38 | `plan.py:DagStep.max_parallel` 和 `dag_executor.py:DagExecutor.__init__` 默认值从文档中的 3 改为 10 | P3 | 代码中 `plan.py:16` `max_parallel: int = 10` 和 `dag_executor.py:39` `max_parallel: int = 10`，但 `ARCHITECTURE_v2.1.md` 和 `TODO_v2.1.md` 示例值为 `3`。该变更为独立配置调整，不涉及架构改动。 | ✅ 已同步（3 文件均修正为 10） |
| 39 | `scheduler/plan.py:_plan_execute_revise_loop` 的 `_pending_plan` 多 cycle 路径实际从不执行 | P2 | `_plan_execute_revise_loop` 设计了 Plan → Execute → Revise → Re-execute 的多 cycle 循环（`while state.status == RUNNING` → `_pending_plan` → `continue`）。但实际场景中 `revise()` 几乎总是失败。**修复**：删除 `_pending_plan` / `_accumulated_results` 实例变量及全部多 cycle 逻辑；`_execute_static_plan` 改为 `while True` 内部自愈循环，`revise()` 返回非空 steps 时 `plan = revised; break` 重新拓扑排序执行（`results` 跨迭代保留）；全部层成功时直接 `_complete()` 不再调 all-ok revise。主循环 `plan()` 成为无条件入口。净减 68 行。 | ✅ 已修复 |
