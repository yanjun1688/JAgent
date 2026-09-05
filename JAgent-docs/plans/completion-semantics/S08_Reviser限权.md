# S08 — Reviser 限权（不可变目标字段强制）

> **所属层**: L3（Scheduler 受信校验）+ L4（Reviser 非受信输出）
> **关联**: `core/planner.py`（`Planner.revise` + `PlanGuardrail`）· `core/scheduler/plan.py`（修订合并/自愈）
> **决策编号**: D-01/D-02/D-05（契约不可变）· C-01（契约来源）· 问题三（Reviser 权限过大）
> **主控**: `JAGENT-2026-Completion-INDEX.md`

---

## 1. 前置依赖

- **S06** 已完成：完成门基于契约判定；**S04** 引用校验；**S03** 结构校验。
- 交付物快照（上游）：
  - `harness/core/planner.py:321-391` `Planner.revise`（LLM 可重写全部字段）。
  - `harness/core/scheduler/plan.py:930-1022` `_merge_revised_plan`、`:1024-1083` `_find_degenerate_revised_steps`、`:1096-1142` `_revise_with_degenerate_guard`。
  - `harness/core/scheduler/plan.py:560-722` 修订触发点。

## 2. 问题背景

日志证据（`harness.log`）：
- read 失败 → Reviser 改成 `list`（把"读取文件"弱化为"列目录"）。
- 引入不存在的 `blackbox-rerun` 目录。
- 生成新 `s3/s4` 步骤并改变依赖关系（`9a340fd0` 悬空依赖）。
- 生成 `$s1.result` 作为路径（无引用契约）。

Reviser 本应只**修复执行路径**，但当前权限过大：可修改工具名、operation、文件路径/URL、required operation 关键参数、step 数量、DAG 依赖、计划 intent、原始目标语义范围。

## 3. 为什么这么做

- 用户原始目标（`intent_raw`）与 DeliveryContract 是**不可变受信数据**，必须由系统强制保护，不能依赖 Reviser（弱模型）的"诚实"（约束 4）。
- 修订只允许在"不弱化交付目标"的前提下调整执行步骤（问题三）。
- 修订后必须重新通过完整受信校验（S03 结构 + S04 引用 + S06 覆盖/完成门）。

## 4. 做之前先检查影响范围

- `Planner.revise` 产出 `DagPlan`：`_parse_plan`（planner.py:592-693）解析 `intent`/`steps`/`required_operations`/`step_tasks`。
- 修订合并：`_merge_revised_plan` 会把原始 step 保留/替换/改写 depends_on——不可变校验须在此前后各做一次。
- 自愈触发点：`scheduler/plan.py` 的 layer 失败、UNSUCCESSFUL 两处 `_revise_with_degenerate_guard` 调用。
- 测试：`tests/test_dag_self_heal.py`、`tests/test_planner_revise_rerun.py`、`tests/test_probe_and_convergence.py`、`tests/test_self_heal_answer_regressions.py`。
- **兼容**：Reviser 合法场景（补 write、改参数、新增上游步骤）必须仍能工作，不可误杀。

## 5. 期望达到的目标

- 受信校验函数（加入 `PlanGuardrail` 或在 revise 路径强制）：

```python
def validate_revision_invariants(
    root_contracts: list[DeliveryContract],
    intent_raw: str,
    revised: DagPlan,
) -> list[str]:
    """校验修订计划未弱化交付目标。返回错误列表。"""
```

规则：
1. **不得修改 UserIntent**：`revised.intent`/`user_intent` 是 LLM 重述，只审计；`intent_raw` 不在计划流中被覆盖（S05 已保证落事件不可变，此处校验计划内引用）。
2. **不得删除/弱化 DeliveryContract**：每条契约仍需在修订后计划中命中（复用 S06 覆盖判定；正向覆盖——契约必须有匹配 step）。
3. **不得修改 required operation 关键参数**：对 `source=caller` 的契约，匹配 step 的 `tool`/`operation`/`path` 必须与契约一致；`content` 存底但本轮不强制匹配（D-03/L-02）。
4. **不得替换用户目标路径**：契约路径出现的地方不得被改成其他路径（如 `blackbox.txt` → `blackbox-rerun.txt`）。
5. **新增步骤必须重过完整校验**：S03 结构 + S04 引用 + S06 覆盖。
6. **修订后双向覆盖**：plan 中所有 mutating step（operation 级有副作用的，S02 契约判定）必须能在契约或"系统已知操作集"中找到（C-02 缓解——防止计划做了系统不知道的副作用）。

- Reviser 只允许变化的执行步骤：工具名/operation/参数仅在与契约一致的范围内可调（如 `read` 失败后先 `list` 定位再 `read`——允许**新增**辅助步骤，但最终契约 step 必须达成）。

## 6. 实现要点

- 在 `Planner.revise` 返回前，或在 `_revise_with_degenerate_guard` / 修订合并后（推荐**受信 Scheduler 侧**强制，不依赖 Planner 自觉）调用 `validate_revision_invariants`。
- 失败处理：写结构化 `PLAN_REVISED`（带 rejection reason）+ 重新进入 revise 循环（复用 `_revise_with_degenerate_guard` 的 retry 模式）；重试预算耗尽 → `_fail`。
- 覆盖判定复用 S06 的 `RequiredOperation.step_satisfies` + step_normal。
- **禁止事项**：
  - 禁止允许 Reviser 缩小/替换/改写原始目标（INDEX 验收项 1）。
  - 禁止允许 Reviser 删除或改路径契约（INDEX 验收项 4）。
  - 禁止让不可变校验依赖 LLM 配合（受信组件内做）。
  - 禁止误杀合法自愈（`list` 定位是允许的辅助步骤，只要契约 read 最终达成）。

## 7. 验收标准

1. 新增测试 `tests/test_reviser_restriction.py`：
   - Reviser 尝试删除 required write 契约 → 拒绝（测试要求 2）。
   - Reviser 尝试把 write 的 `path=blackbox.txt` 改成 `other.txt` → 拒绝（测试要求 3）。
   - Reviser 把 read 替换为 list → 不满足契约 read → 不得误判成功（测试要求 4）。
   - 合法修订（read 失败后新增 list 定位再 read，契约 read 达成）→ 通过。
   - 修订后生成悬空 `$s1.result` → 被 S04 拒绝。
   - 修订后新步骤产生 plan 未声明且契约未覆盖的 mutating 操作 → 反向覆盖拒绝（C-02）。
2. 现有 `tests/test_dag_self_heal.py`、`tests/test_planner_revise_rerun.py`、`tests/test_self_heal_answer_regressions.py` 回归全绿（合法自愈不误杀）。
3. 事件链：拒绝的修订写入结构化事件，可从事件流重放拒绝原因。

## 8. 这么做的后果

- **对 S12**：验收项 1/4（目标不可覆盖、契约不可弱化）由本步达成。
- **对自愈**：Reviser 自由度受限 → 部分"靠改目标糊弄"的自愈转为正确修复或失败；自愈成功率可能变化，需回归观察。
- **技术债**：`source=extracted` 契约的不可变强度低于 `source=caller`（抽取本身可能不完整，C-02）——记录在案。

## 9. 收尾自检清单

- [ ] 不可变校验在受信 Scheduler 侧强制
- [ ] 契约删除/路径替换/read→list 均被拒
- [ ] 合法自愈不被误杀
- [ ] 拒绝原因落结构化事件
- [ ] 自愈相关回归全绿
- [ ] 在 INDEX 状态表更新 S08

## 10. 完成状态

| 项 | 值 |
|----|----|
| 状态 | 已完成 |
| 执行会话 | opencode（S08 会话） |
| 完成日期 | 2026-08-13 |
| 备注 | `validate_revision_invariants`（正向覆盖 + caller 关键参数保留 + C-02 反向覆盖）接入 `_revise_with_degenerate_guard`：在合并副本上校验（避免重试中 results/aliases 变异），接受后调用方真实合并；失败 → SYSTEM REJECTION feedback 重试，预算耗尽 fail。意图不可变由 S05 intent_raw 事件层保证（不变量函数不做重复检查）。新增 `tests/test_reviser_restriction.py`（11 测试）。 |
