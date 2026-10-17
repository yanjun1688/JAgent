# 差异报告：删除 serial 生产 fallback + 局部修复默认开启（R1/R2 减法解）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-12 |
| **类型** | 差异报告 + 实施记录（TDD） |
| **来源** | 真实 E2E Run `19538cb0` 失败分析（根因 B）+ [审计 review_20260909 §R1/R2](./review_20260909_accidental_complexity_and_trusted_seam_audit.md) |
| **关联决策** | `PLAN_20260910_R8b_R1_R2` §2/§3（原方案 A/B 作废）· ADR-013（反应式模型改用显式选择而非降级） |
| **状态** | 已实现 + 全量回归通过 |
| **架构原则** | 减法 / 封装 / 抽象化；不改逻辑、不加功能；消除"第二条弱完成路径" |

---

## 1. 触发与本裁决

真实 Run `19538cb0`：Planner 连产 3 次非法计划 → 触发 `_get_or_fallback` → `new` 独立 `AgentLoopScheduler`（serial）→ 空转到 `max_iterations` 失败。

owner 裁决（三选一中的架构减法解）：
1. **删除 serial 生产 fallback**：planner 产不出合法计划 → 诚实 `RunFailed`。
2. **局部修复（F-6）默认开启**，自愈名副其实。
3. 跑测试 + 更新文档。

---

## 2. 现状事实（纠正"路径"与"方案"混淆）

- **serial 触发点**：唯一在 `PlanningExecutorScheduler._get_or_fallback`，仅当 `planner.plan()` 返回 `None`（plan 生成/校验 3 次全失败）。**与"步骤执行失败"无关**。
- **步骤失败**的既有处理：局部修复 `local_repair`（受限 think-act）→ 全局 `revise`（feedback 给 planner）→ 完成门诚实判定。均已存在。
- serial 的其它消费者：`evaluation/run_eval.py`（显式 `new`）；类被 re-export。

---

## 3. 变更清单

| 文件 | 现状 | 目标 |
|---|---|---|
| `harness/core/scheduler/plan.py` | `_get_or_fallback` 失败时 `new AgentLoopScheduler` + `serial.run` | 删除该方法；`plan()=None` → `_fail("Planner failed to produce a valid plan")`；移除 `AgentLoopScheduler` import |
| `harness/core/scheduler/base.py` | `local_repair_enabled=False`；手工白名单 `local_repair_allowed_tools` | `local_repair_enabled=True`（默认开启）；**删除** `local_repair_allowed_tools` 字段 |
| `harness/core/scheduler/local_repair.py` | 白名单取 `cfg.local_repair_allowed_tools` | `allowed_tools` 由已注册工具派生（`self.tool_defs`）；只读强制仍在 `validate_local_repair` 按 action 判定 |
| `harness/api/serve.py` | `HARNESS_LOCAL_REPAIR` / `HARNESS_LOCAL_REPAIR_TOOLS` 读 env | 使用默认（开启 + 派生候选）；删除 env 读取 |
| `harness/api/routes.py` | docstring："Plan 失败降级 AgentLoopScheduler" | "Plan 失败直接诚实判失败" |
| `scripts/smoke_resilience_trigger.py` | Prereqs 要求设置 env | 更新为"默认开启、无需 env" |
| `JAgent-docs/architecture/ADR-013...md` | 引用 `_get_or_fallback` 作为 serial 运行证据 | 改为"按 grounding 装配期选择调度器" |

**删除的重复**：`local_repair_allowed_tools` 手工白名单与 `HARNESS_LOCAL_REPAIR*` env 是"第二套只读判定"，与 `validate_local_repair` 的 action 级只读判定重复 → 收敛为单点（契约派生 + action 判定）。

---

## 4. 行为变更（显式标注）

- **BEHAVIOR CHANGE**：planner 无法产出合法计划时，Run 由"降级 serial（常空转/假绿）"改为**结构化 `RunFailed`**。
- **BEHAVIOR CHANGE**：F-6 局部修复由默认关改为**默认开**；候选工具由注册表派生（不再需要手工白名单）。
- `AgentLoopScheduler` 类**保留**（evaluation/scripts 显式使用），仅删除生产降级接线。
- 受信边界不变：自愈动作仍经 `validate_local_repair`（轮次/工具/只读，fail-closed）+ ToolExecutor 全链；完成门仍拒绝假绿。

---

## 5. 测试

- 新增 `tests/test_planner_failure_no_fallback.py`：planner 失败 → `RunFailed` 且**无** `RunCompleted`。
- 更新 `tests/test_local_repair_wiring.py`：默认开启断言；删除白名单入参。
- 更新 `tests/test_dag_self_heal.py` / `tests/test_dag_exec_state_decoupling.py`：显式 `local_repair_enabled=False` 以隔离"全局 revise"路径。
- 更新 `tests/test_api.py` full-wiring fixture：mock 不再依赖已删的 serial 弱路径。
- 回归：`pytest -q` → **1522 passed, 2 skipped**；`ruff check harness tests scripts` / `mypy harness` 全绿；`generate_openapi.py --check` 通过（routes docstring 变更后已再生成）。

---

## 6. 明确不做

- 不引入新引擎/编排器（ADR-013 反应式模型仍是未来独立项，且改为显式选择）。
- 不改 `AgentLoopScheduler` 的评测用途。

---

## 7. 追加：失败预算字段收敛（2026-09-12）

在删除 fallback 的基础上，把散落的"试几次就停"预算收敛为**一个**：

- **合并** `max_iterations`（外层 plan/execute/revise 轮数）与 `max_consecutive_failures`（连续失败熔断）→ 单一 **`run_attempt_budget`**（默认 20）。
- **保留** `max_revise_retries`（退化修订守卫重试）、`max_confirm_retries`（确认重试）、`local_repair_max_rounds`（每步修复轮次）——语义不同，非重复。
- 保留 Planner 的 `max_plan_retries`（LLM 结构化解析重试，另一层）。
- 同步：`serve.py` / `query.py`（`/query?type=schedulers` 投影）/ `ops-client.ts` / `run_eval.py`（`--run-attempt-budget`）/ scripts / 测试。

**收益**：`SchedulerConfig` 失败预算 6→5，且不再有两个数共同表达"重试上限"。到限统一走结构化 `RunFailed`（诚实失败）。

**行为变更（显式）**：连续失败熔断阈值由 5 提升为 `run_attempt_budget`（默认 20）；外层轮数默认由 50→20。持久失败仍由退化修订守卫 + 完成门提前诚实失败。



