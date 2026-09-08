# Review — F-5 判定粒度 vs 退化修订守卫不一致（A′ 修正）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-08 |
| **类型** | code review 缺陷（受信守卫判定粒度漂移） |
| **分支** | `review/code-review-fixes`（自 `main` c7c6230） |
| **相关文档** | [DESIGN_v3.4 §11.5](../Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md) |
| **状态** | ✅ 已裁决 + 已实现（TDD）+ 全量验证通过 |

---

## 1. 发现

两处受信守卫对"修订是否真正修复了已知坏步骤"判定粒度不一致：

| 守卫 | 判定谓词 | 位置（快照期） |
|---|---|---|
| 退化修订守卫 | `(tool, 规范化 input)` 全签名 + 上游闭包须含新步骤 | `harness/core/scheduler/revision_guard.py` |
| F-5 `unresolved_known_bad_steps` | 仅 `tool`，**忽略 input**；无上游感知 | `harness/core/recovery.py` + `scheduler/plan.py`（修订合并后注入 high 优 FeedbackInjected） |

**后果**：LLM 保持同一工具、仅修正输入参数（修 URL/字段）时——这是退化守卫眼中合法的"新尝试"（input 已变，非退化）——F-5 却在同一轮把它判为"未真正修复"，注入"You MUST switch tool or give up"高优反馈。能一次修好的情况被多绕一轮；且 F-5 反馈文案（*"will fail again if re-run unchanged"*）在 input 已变时与自身 tool-only 检查自相矛盾。F-5 另无上游感知，"上游已修、本步同签名"的合法修复亦会被误杀。

## 2. 约束（不可破坏的不变量）

- e05087b6 回归必须仍被拦截：merge 把未被补丁覆盖的失败步骤**原样恢复**（同 tool + 同 input）→ 必须判 unresolved。
- F-5 是**受信纯函数**，无 I/O、无 LLM；机械判定，不依赖 Agent 配合（AGENTS.md §2.2）。
- 受信组件不得依赖脆弱的错误文本正则分类来判定语义。

## 3. 方向裁决：A′（否决纯 A）

- **纯 A（把 F-5 完全并入退化守卫"全签名 + 闭包无新步骤"谓词）被否决**：会重新打开 e05087b6 的洞——merge 恢复的坏步骤若其 `depends_on` 被 LLM 换成新替换步骤，闭包即含新签名 → 判"已修复"→ 已知坏动作静默重放不再被拦。闭包条款回答的是"重跑**可能**成功吗"（乐观），F-5 回答的是"坏动作是否**原样加回**"（悲观），二者互补而非等价，不能共享闭包逻辑。
- **A′（采纳）**：两守卫**共享"同一动作"的唯一定义**（全签名），但各自保留语义与角色：
  - 收敛点 = 签名定义（而非整个谓词）：`(tool, 规范化 input)` 提到叶模块 `dag_types` 作单一事实源。
  - F-5 从 tool-only 改为**全签名比较**；**保留**瞬时排除与无闭包语义；post-merge 角色不变。

## 4. 实现（TDD：先红后绿）

1. `harness/core/dag_types.py`：新增 `action_input_signature(inp)` / `action_signature(tool, inp)`（key-order 无关的规范化签名）——两守卫共享的唯一"同一动作"定义。
2. `harness/core/scheduler/revision_guard.py`：`step_signature` 委托 `dag_types.action_signature`，删除本地 `_normalize_input` 与重复 `json` 实现。
3. `harness/core/recovery.py`：`unresolved_known_bad_steps(prev_evidence, patch_steps, *, original_inputs=None)`
   - 新增关键字参数 `original_inputs`：sid → 该步**实际执行过**的原始 input（证明 input 修正的必要证据）。
   - 已修复判据：tool 切换，**或** 同 tool 且 `original_inputs[sid]` 可得、规范化 input 已变。
   - 原始 input 缺失时 **fail-closed 退回 tool-only**（不静默假设"input 已修"）。
4. `harness/core/scheduler/plan.py`：修订合并后调用处从 merge 前失败计划构造 `original_inputs` 传入；反馈文案改为 *"re-add the SAME action (same tool and same input)"*。
5. 测试 `tests/test_recovery_core.py`：新增/改写——同 tool 改 input = 已修复不报；同 tool 同 input 原样重放 = 仍报；input 规范化 key 序无关；原始 input 缺失 = 保守回退仍报。既有 e05087b6 回归用例保持拦截。

## 5. 验证

- `pytest` 全量：**1417 passed / 2 skipped**。
- `ruff check` 改动文件：**0 error**。
- 纯函数层用例（test_recovery_core.py）19 项全绿。

## 6. 遗留备注

- D12 强制 1:1 绑定（revision 用**新 step id** 覆盖失败步）时，F-5 因证据/合并步 id 不同仍会保守报 unresolved——属既有行为，本次未扩范围（review 聚焦同 id 改 input 场景）。
- HANDOFF_v3.4 为历史快照，其 F-5 高层描述（"检测修订补丁未覆盖的非瞬时失败步骤"）仍成立；精确粒度以 DESIGN §11.5 与本记录为准。

## 7. 审计核销补遗（2026-09-08，终态复核）

- **旧 helper/死引用核销**：`_normalize_input` 本地实现已删除且代码零残留；`recovery._TOOL_UNAVAILABLE_PATTERNS`（全程未使用的编译正则）已于终审删除并留注记。
- **行为 pin 逐一对应**（`tests/test_recovery_core.py::TestUnresolvedKnownBad`）：e05087b6 原样重放仍拦 ✓；同 tool 改 input=已修复不报 ✓；key 序无关 ✓；瞬时排除 ✓；completed 不报 ✓；缺 original_inputs 保守回退 ✓。
- **接线核验**：`plan.py` 调用点（~L647-652）确以 `{s.id: s.input for s in plan.steps}` 传 `original_inputs`，注释与反馈文案一致。
- **已知残余（非静默）**：plan.py 该调用点无**独立 scheduler e2e** 驱动 evidence 后再 revise 验证 unresolved 反馈注入（既有 scheduler 自愈测试用 mock execute_layer，不产生证据事件，故不触达该分支）。谓词语义已由纯函数层 pin 全覆盖；调用点正确性由代码审查 + 注释保证。若未来再动 F-5 接线，建议补一条 evidence 驱动 e2e。
