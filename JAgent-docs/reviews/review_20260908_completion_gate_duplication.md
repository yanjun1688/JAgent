# Review — _execute_plan 完成门判定重复（收敛到单一分派）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-08 |
| **类型** | code review 维护性缺陷（完成门判定多处内联，C-02 fail-safe 关键路径） |
| **分支** | `review/code-review-fixes`（自 `main` c7c6230） |
| **相关文档** | [DESIGN_v3.4 §11.6](../Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md) |
| **状态** | ✅ 已裁决（方案 B 治本）+ 已实现（TDD）+ 全量验证通过 |

---

## 1. 发现（reviewer 描述 + 核对修正）

Reviewer 指出 `scheduler/plan.py` 中"revise 返回空 steps → 过完成门 → finalize/fail"在两处几乎重复（layer 失败后 revise 分支 / 整层跑完但有 unsuccessful 触发 revise 的分支），而文件尾部已有 `_finalize_or_fail_verdict` 封装未被复用。

**核对结果**：
- reviewer 声称"两处都没有复用 `_finalize_or_fail_verdict`"**不准确** —— layer 失败分支（site1）的空 steps 落点本就在调用它（旧 plan.py:755）。
- 但底层担忧**成立且被低估**：完成门判定实际有 **3 份内联拷贝 + 1 个仅单点使用的 helper**：

| 位置（旧行号） | 内容 |
|---|---|
| plan.py:715-757（site1 空 steps） | 完成门 → PLAN_REVISED → `_finalize_or_fail_verdict`（唯一用 helper 的点） |
| plan.py:940-954（site2 空 steps） | 完成门（只查 mechanical）→ fail / 落空穿到成功尾 |
| plan.py:956-1023（成功尾） | 再内联 mechanical + deliverable + PLAN_COMPLETED + finalize —— 本质是 helper 的**另一份展开** |

外加 `revised.failed → "Task cannot be completed"` 守卫两处重复、PLAN_REVISED payload 构造 4 份、`step_tasks` 推导 6 份。

**drift 症状（已实锤）**：site2 空 steps 事件在判空前发出，empty+failed 时 PLAN_REVISED 文案写 *"task complete"* 随后却 RUN_FAILED；site1/site2 空 steps 失败都不发 PLAN_FAILED（与正常完成尾不一致）；site1 空 steps 成功不发 PLAN_COMPLETED（与 site2/成功尾不一致）。

## 2. 约束

- 完成门是 C-02 fail-safe 关键路径（"宁可标未达成，绝不假绿"）—— 重构必须保持"任何入口都过同一条机械 + 交付判定"。
- 当前为 demo/开发期，无历史数据与生产约束 —— 允许归一事件序列（治本优先于最小 diff）。
- 受信边界不变：全部判定仍为纯机械（`_completion_gate` 未动）。

## 3. 方向裁决：B（治本收敛）

- **A（仅抽纯重复件，门逻辑留三处）**：只消除症状，C-02 关键判定仍分三处，下次照样漂移 —— 否决。
- **B（收敛到单一分派例程）** —— 采纳：把"完成门判定 + 终局收尾"收敛为一个方法，全部入口（层失败后 revise 空 / unsuccessful revise 空 / 正常完成尾）汇于同一条真源，并顺带归一 PLAN_FAILED / PLAN_COMPLETED / PLAN_REVISED 文案的事件语义。

## 4. 实现（TDD：先 pin 失败测试，再重构转绿）

1. **RED**：`tests/test_completion_gate.py` 新增 pin 测试（FAILED 工具 → site1 `step_failure_revised`；429 → site2 `unsuccessful_revised`，两者 revise 返回空）：
   - 断言完成门失败必须发 `PLAN_FAILED` 且先于 `RUN_FAILED`（重构前两条路径都不发 → 红）；
   - 断言末条 PLAN_REVISED 的 summary 不得自称 "task complete"（site2 旧文案自相矛盾 → 红）。
2. **GREEN**（`harness/core/scheduler/plan.py`）：
   - 新增 `_emit_plan_revised` —— PLAN_REVISED 事件 + step_tasks + trace 共享构造器；
   - 新增 `_dispose_completion_verdict` —— 单一完成门分派（替换 `_finalize_or_fail_verdict`）：mechanical 不全 → `PLAN_FAILED`+fail；deliverable failed → `PLAN_FAILED`+fail；通过 → [emit `PLAN_COMPLETED`] + maybe_compress + `_finalize_with_summary`；
   - 新增 `_settle_empty_revised` —— site1/site2 共用的"revise 空 steps"分派（failed 声明 → 如实 "task failed" 事件+fail；否则过门 → 准确 summary → dispose）；
   - site1/site2 空 steps 分支各自收敛为一次 `_settle_empty_revised` 调用；site2 的 PLAN_REVISED 仅在"仍有步骤继续"时于合并前落（文案如实）；
   - 成功尾内联门整段替换为 `_dispose_completion_verdict` 调用，且复用 all_layers_ok 入口已算的 `verdict`（消除二次过门）。

## 5. 行为归一（实测，两分支一致）

```
失败：PlanRevised(NOT complete — unmet…) → PlanFailed → RunFailed
成功：… → PlanCompleted → RunCompleted(all_normal=True)   （保持不变）
```
- 移除 site2 "task complete" 文案后紧跟 fail 的矛盾事件。
- revise-empty 失败路径与正常完成尾统一发 PLAN_FAILED。

## 6. 验证

- `pytest` 全量：**1419 passed / 2 skipped**。
- `ruff check`：0 error。
- 相关定向：`test_completion_gate.py`（18 项）、`test_deliverable_gate.py`（18 项，含 **成功尾 deliverable-fail 端到端 pin** `test_tail_deliverable_fail_emits_plan_failed_and_run_failed` —— 机械全 normal 但契约 unmet 时必须 PLAN_FAILED 先于 RUN_FAILED、不发 PLAN_COMPLETED/RUN_COMPLETED）、`test_scheduler.py`、`test_step_normal_gate.py` 全绿。

## 6.1 审计补遗（docs 后置核验）

压缩摘要中遗留"需验证"事项逐条闭合：
- 旧 helper `_finalize_or_fail_verdict` / `verdict3` 全仓 grep：仅存于文档（描述重构前状态），代码零残留。
- 结构性再读 `_execute_plan`（L688-872）与三 helper（L935-1123）：收敛点完整、无第 4 处内联门、成功尾复用入口 `verdict` 无二次过门。
- 缺口确认为 1 个：**成功尾 deliverable 分支无端到端 pin**（既有测试仅覆盖纯函数与 revise-empty 路径）→ 上方新增 pin 闭合。

## 7. 遗留备注

- `_completion_gate`（CompletionGateMixin）纯判定未改动，仅在收尾侧归一。
- site1/site2 空 steps **成功**分支为防御性代码（实际有失败/非 normal 步骤即不可能过门），现同样发 PLAN_COMPLETED —— 与成功尾一致，属预期归一。
- 完成事件序列归一后，事件流消费方（fold/Replay/前端）对终局事件顺序的依赖只会更简单，不破坏既有断言。
