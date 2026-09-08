# Review — step_is_mutating 未知工具 fail-open（Q-06 反向覆盖失效）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-08 |
| **类型** | code review 受信分类器 fail-open（未知工具默认"非 mutating"，Q-06 覆盖失效） |
| **分支** | `review/code-review-fixes`（自 `main` c7c6230） |
| **相关文档** | [DESIGN_v3.4 §11.7](../Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md) |
| **状态** | ✅ 已裁决（B1 fail-closed）+ 已实现（TDD，RED→GREEN 实证）+ 全量验证通过 |

---

## 1. 问题（reviewer 描述 + 核对）

`planner/revision_invariants.py::step_is_mutating`：

```python
tool_def = registry.get_tool_def(step.tool)
if tool_def is None:
    return False   # 未知工具 → 判定为"非 mutating"
```

⇒ Q-06（"mutating 步骤必须被 DeliveryContract 覆盖"，ADR-009 反向覆盖，堵 self-authorize 漏洞）对引用不存在工具的步骤**静默失效**。

**核对确认**：
- 与 `recovery.py::_is_read_only_action`（未知工具 → `return False` = 非只读 = 当作有副作用）语义恰好相反 —— **同一"未知工具=?"问题，两个受信分类器取相反默认值**，`step_is_mutating` 是代码库唯一的乐观 outlier（其余受信方：PlanGuardrail guardrail.py:52、dag_executor.py:505、agent base base.py:930 一律拒绝未知工具）。
- 当前无实洞：invariants 在合并副本上先跑（revision_guard.py:310）→ 真实合并后 `guardrail.validate` 拒未知工具 → 兜底执行层也拒。但这是**三个不同文件受信校验器间的隐式顺序依赖** —— 若未来某调用点单独跑 `validate_revision_invariants` 而不串联 PlanGuardrail，fail-open 即成真洞。
- 影响面：`step_is_mutating` 仅 Q-06 一处消费；生产侧 `validate_revision_invariants` 仅 revision_guard 一个调用点。无测试依赖该 fail-open。

## 2. 方向裁决

与用户商讨后按 **B1（分类器 fail-closed）** 落地：

- **A（仅注释/断言）** 否决 —— 不使该校验器独立成立，Q-06 对未知工具仍静默漏判。
- **B2（未知工具作独立不变量错误）** 否决 —— 在 invariants 里复制 guardrail 的 tool-existence 规则，属"同一受信规则两处实现"会漂移的反模式（同 review #2 收敛原则）。
- **B1 采纳** —— 修在语义原语：无法证明无副作用 ⇒ 视为 mutating ⇒ 必须被契约覆盖。与 `_is_read_only_action` 对齐，符合 C-02。A-lite 作为文档补充（调用点注释声明分层职责，不替代机制）。

## 3. 实现（B1）

- `step_is_mutating`：未知工具 → `return True`；docstring 说明 fail-closed 理由与对齐对象。
- Q-06 循环注释补充：未知工具 fail-closed，不依赖下游 PlanGuardrail 顺序兜底。
- `revision_guard.py` 守卫内加 A-lite 注释：invariants 只管交付不变量 + Q-06 覆盖；tool 存在性/schema/probe 是 PlanGuardrail 职责，真实合并后必由其收口，新调用点不得只跑本校验。

## 4. 测试（TDD）

`tests/test_reviser_restriction.py` 新增 3 项 pin：
- `test_unknown_tool_uncovered_step_rejected_fail_closed`：未知工具 + 未覆盖 → 报 "un-declared mutating step"。**RED 实证**：临时还原旧 `return False` 后该测试失败，确认测试确实钉住 fail-open。
- `test_unknown_tool_covered_by_contract_passes`：被同名契约覆盖的未知工具步骤在 Q-06 放行（残余角落——契约侧已在上游拒绝未知工具契约 contract_extractor.py:72 / intent.py:62，实际不可达，此处 pin 不变量层不回归）。
- `test_unknown_tool_no_contracts_skips_reverse_coverage`：无契约 legacy 走 unverified，未知工具不受罚。

## 5. 验证

- `pytest` 全量：**1422 passed / 2 skipped**（新增 3）。
- `ruff check`：0 error。
- 定向：`test_reviser_restriction.py`、`test_recovery_core.py`、`test_planner.py` 全绿。

## 6. 遗留备注

- 行为变化：未知工具 + 未覆盖的修订现在于 invariant 阶段被拒（触发 revise 重试 + 反馈），而非延迟到 guardrail 硬 fail run —— 更早、对 LLM 反馈更可操作，安全性不变。
- 未统一 `_is_read_only_action` 与 `step_is_mutating` 为单一副作用分类原语：二者机制不同（recovery 用静态白名单集合，revision 用 registry 定义），归并属更大重构，留作后续可选。
