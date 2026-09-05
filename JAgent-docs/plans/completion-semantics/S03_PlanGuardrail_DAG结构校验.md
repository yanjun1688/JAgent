# S03 — PlanGuardrail DAG 结构校验（纯函数化）

> **所属层**: L3（Scheduler 前置受信校验）
> **关联**: `core/planner.py`（`PlanGuardrail`）· `models/plan.py`（`DagPlan.topological_sort`）
> **决策编号**: 问题四（非法 DAG 未在 Executor 前拦截）· AGENTS.md §3.5（根治而非打补丁）
> **主控**: `JAGENT-2026-Completion-INDEX.md`

---

## 1. 前置依赖

- **S01** 已完成（决策编号可引用）。
- 交付物快照（上游）：
  - `harness/core/planner.py:117-243` 现有 `PlanGuardrail.validate`：已检查 step_id 缺失/重复、tool 存在、probe 信任、depends_on 引用存在、required_operations 正向覆盖（v2.2 Layer 2）、dangerous combinations、max_parallel。
  - `harness/models/plan.py:52-106` `DagPlan.topological_sort`：**运行时**抛 `ValueError` 处理未知依赖与循环。

## 2. 问题背景

日志证据（`harness.log:986`、`harness.log:2359`）：
- Run `9a340fd0`：`ValueError("Step 's3': depends on unknown step 's1'")`
- Run `3b88b26a`：`ValueError("Step 's2': depends on unknown step 's1'")`

错误发生在 **DagExecutor 执行期**（`plan.topological_sort` 被调用时），而非受信 PlanGuardrail 前置拦截。即使 Guardrail 已做了部分检查，以下结构问题仍可能漏过：
1. step_id 唯一性（现有代码在 `planner.py:148` 用 `in [s.id for s in plan.steps[:i]]` 检查，O(n²) 且与 Guardrail 其他规则解耦）。
2. 自依赖（`depends_on` 含自身）。
3. 循环依赖（当前只靠 `topological_sort` 抛 ValueError，没有结构化错误分类）。
4. 层级一致性 / 依赖是否被合理地层级化（当前只做"能拓扑排序"这一最弱断言）。
5. 未解析的 `$step.output` 引用（本步先立结构，具体引用契约在 S04 落地）。

**原则**：非法计划必须在进入 DagExecutor 之前被受信 PlanGuardrail 拒绝，禁止让 Executor 抛裸 ValueError（AGENTS.md §3.5）。

## 3. 为什么这么做

- 把"非法 DAG"从运行时崩溃提前到计划期拒绝，让 Planner 收到结构化重试反馈（`_retry_prompt`）。
- 将结构校验做成**纯函数**（无 I/O、无 LLM），符合测试分层（AGENTS.md §5.1 单元测试目标组件）。
- 为 S06 的覆盖校验、S08 的 Reviser 限权提供统一的"计划合法性"判定入口。

## 4. 做之前先检查影响范围

- `PlanGuardrail.validate` 被调用点：
  - `harness/core/planner.py:309`（`planner.plan`）
  - `harness/core/planner.py:377`（`planner.revise`，带 `completed_step_ids`/`available_step_ids`）
- `DagPlan.topological_sort` 被调用点：
  - `harness/core/dag_executor.py:96,603`
  - `harness/core/scheduler/plan.py:490`
  - 测试：`tests/test_planner.py`、`tests/test_dag_executor.py`、`tests/test_dag_self_heal.py`、`tests/test_completion_gate.py`、`tests/test_probe_and_convergence.py`
- **注意**：`revise` 会传入 `completed_step_ids`/`available_step_ids`（历史已完成步骤），结构校验必须区分"当前 plan 内依赖"与"外部已交付依赖"，不得把外部依赖当未知 step 拒绝。

## 5. 期望达到的目标

- 新增纯函数 `validate_dag_structure(plan, *, completed_step_ids, available_step_ids) -> list[str]`，覆盖：
  1. step_id 唯一性；
  2. `depends_on` 引用存在于 当前 plan ∪ completed ∪ available；
  3. 自依赖检测；
  4. 循环依赖检测（不依赖拓扑排序抛异常，独立图算法判定并给出环路径）；
  5. 层级一致性（拓扑结果与 `depends_on` 层级语义一致，任何依赖深度 > 0 的 step 必须位于依赖之后）；
  6. 关键参数结构（`input` 为 dict、必填键存在——结构级，不做工具 schema 级校验，那属于 Executor SchemaGuardrail）。
- `PlanGuardrail.validate` 在原有检查后**追加**结构校验，失败时错误消息可直接进入 `_retry_prompt`（LLM 可读）。

## 6. 实现要点

```python
def validate_dag_structure(
    plan: DagPlan,
    completed_step_ids: set[str] | None = None,
    available_step_ids: set[str] | None = None,
) -> list[str]:
    """纯函数。返回错误列表；空列表 = 合法。禁止抛 ValueError。"""
```

- 环检测：DFS 三色标记（white/gray/black），返回首个环的节点路径，错误消息形如 `Cycle detected: s1 -> s3 -> s1`。
- 自依赖：`dep in step.depends_on and dep == step.id` → `Step 's1' depends on itself`。
- 层级一致性：构建依赖图后验证每个 step 的层级 == max(依赖层级)+1（对完成/外部依赖不计算层级）。
- **禁止事项**：
  - 禁止依赖 `topological_sort` 的 ValueError 作为判定手段（那是运行时路径）。
  - 禁止把 `completed`/`available` 当作非法依赖来源拒绝。
  - 禁止在本步实现 `$step.output` 引用契约（S04 负责）。

## 7. 验收标准

1. 新增单元测试 `tests/test_plan_guardrail_structure.py`（纯函数、无 I/O）：
   - step_id 重复 → 错误。
   - `depends_on` 引用不存在 → 错误。
   - 自依赖 → 错误。
   - 简单环（s1→s2→s1）→ 错误。
   - 长环 → 错误且含路径。
   - 合法分层（s2 depends_on s1）→ 无错误。
   - `completed_step_ids` 中的外部依赖 → 不误报。
   - 空 steps / 单 step → 无错误。
2. 现有回归：`pytest tests/test_planner.py tests/test_dag_executor.py tests/test_dag_self_heal.py tests/test_completion_gate.py` 全绿。
3. 验证非法计划在 `planner.plan` 中触发重试（`_retry_prompt` 收到结构错误消息），不进入 DagExecutor。

## 8. 这么做的后果

- **对 S04**：结构校验函数为引用校验提供 `completed/available` 上下文复用。
- **对 S06**：覆盖校验复用同一入口，保证"非法 DAG 不进完成门"。
- **对 S08**：Reviser 修订后必须重跑完整结构校验。
- **行为变化**：部分此前"能跑到 Executor 才崩"的 Run 现在会在 Planner 重试阶段被纠正或失败——这是预期改进，需在回归中留意测试用例假设。

## 9. 收尾自检清单

- [ ] 纯函数单元测试全绿
- [ ] 受影响回归全绿
- [ ] 环检测含路径消息
- [ ] 外部依赖（completed/available）不误报
- [ ] 在 INDEX 状态表更新 S03

## 10. 完成状态

| 项 | 值 |
|----|----|
| 状态 | 已完成 |
| 执行会话 | opencode（S03 会话） |
| 完成日期 | 2026-08-13 |
| 备注 | `models/plan.py` 新增纯函数 `validate_dag_structure`（step_id 唯一/依赖存在/自依赖/环检测含路径/层级一致性/input 结构），无 I/O 不抛 ValueError；PlanGuardrail.validate 集成，替代 O(n²) 查重与 topological_sort 运行时兜底。新增 `tests/test_plan_guardrail_structure.py`（18 测试）。 |
