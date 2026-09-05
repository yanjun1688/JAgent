# 结构化输入需求 — 完成语义根治的架构级改造方向（Review）

| 属性 | 值 |
|---|---|
| **状态** | 已记录，待后续架构评审（未实施） |
| **日期** | 2026-08-12 |
| **来源** | JAGENT-2026-P1-13 黑盒测试（Run `cee7d7f6`：硬性交付未完成却成功） |
| **关联决策** | ADR-007（完成语义分层）、D1（failure_policy 声明式契约）、U2（deliverable_met） |
| **性质** | 架构级改造候选，不属于当前 Bug 修复范围 |

---

## 1. 背景与问题

JAGENT-2026-P1-13 的 Run `cee7d7f6` 暴露了一个系统性根因：

```
用户："创建 blackbox.txt，写入 hello harness blackbox，然后重新读取"
  ↓ Planner 只生成了 read blackbox.txt（write 从未进入任何计划）
  ↓ read 失败 → Reviser 改为 list .
  ↓ list 机械成功 → RunCompleted(all_normal=true, unmet_step_ids=[])
```

即使完成门回归 root_plan、即使加入 required-operations 契约，仍存在一个**无法用纯结构手段闭合的盲区**：

- 弱模型可以在 `required_operations` **和** plan steps **同时**丢掉 write（自洽地错误）。
- 系统只有自由文本 intent，没有结构化的"用户到底要求了什么"。
- 纯结构检查只能验证"LLM 自己声明的东西是否达成"，无法验证"用户要求的硬性交付物是否达成"。

这正是前一轮交接文档（soft_error_selfheal_blackbox_handover_20260805）中 U2 的结论：
> "是否在完成语义上区分 `deliverable_met` 需架构层决策（对应 ADR-007 完成语义分层）"

以及 D1 的方向：
> "系统不再猜'哪步是硬交付'，只强制 Agent 自己声明的规则……让'完成语义'变成声明式契约"

## 2. 本轮已采纳的缓解（非根治）

在本次 Bug 修复中，先实施两层缓解（治标但有效）：

- **Layer 1（prompt 补全）**：修 `_PLAN_PROMPT` / `_CLASSIFY_PROMPT` 的 MUST-plan 枚举缺口，补入 write/create/delete/append/update。operation 集合从工具注册表 schema 的 `enum` 生成，不硬编码。
- **Layer 2（required-operations 契约）**：计划 JSON 输出 `required_operations` 字段；PlanGuardrail 做覆盖检查（每条 required op 必须映射到 ≥1 个 plan step）；完成门检查每条 required op 达成 `step_normal`。

**残余盲区（本 review 要解决的核心）**：上述两层都依赖 LLM 的 `required_operations` 声明本身不丢字段。极端情况下弱模型自洽地少报，结构检查无法察觉。

## 3. 架构级改造候选方案（后续评审，未实施）

### 方案 A：结构化输入需求（用户侧契约）

让用户/调用方以**结构化方式**声明任务的硬性交付要求，而不是只有自由文本：

```json
POST /api/v1/runs
{
  "intent": "创建 blackbox.txt，写入 hello harness blackbox，然后重新读取",
  "required_operations": [
    {"tool": "file_op", "operation": "write", "path": "blackbox.txt"},
    {"tool": "file_op", "operation": "read", "path": "blackbox.txt"}
  ]
}
```

- **完成门**：每条 `required_operations` 必须有对应的 step_normal 结果，否则 RunFailed。
- **覆盖检查**：plan 必须包含所有 required operations，缺则拒绝。
- **优点**：彻底不依赖 LLM 理解用户意图；系统强制的是调用方自己声明的契约，与 D1 同构。
- **缺点**：需要前端/API 契约变更；`required_operations` 的抽取职责前移给用户或上游对话层；OpenAPI 同步（AGENTS.md §4.1 同源契约）。

### 方案 B：LLM 抽取 + 系统二次校验（半结构化）

保留自由文本 intent，但增加一个**独立的、受约束的抽取步骤**：

- 抽取调用：`intent → required_operations`（固定 schema，独立 prompt，与规划解耦）。
- 校验：抽取结果必须覆盖 plan 的 mutating steps（系统反查——plan 里出现的操作必须能在 required_operations 中找到，防止"计划做了系统不知道的事"），且 plan 必须覆盖 required_operations（双向覆盖检查）。
- **优点**：API 无变更；双向往返约束让"自洽地少报"更难。
- **缺点**：抽取仍是 LLM；双向覆盖只保证"系统知道的全做了"，不保证"用户要的全被知道"。

### 方案 C：完成语义分层（ADR-007 U2 落地）

将 RUN_COMPLETED 拆出两个正交维度（延续 ADR-007 的 ExecState/TaskState 分层思想）：

- `mechanical_complete`（系统机械聚合，已有）：所有声明步骤 step_normal。
- `deliverable_met`（交付物达成）：需要结构化交付物清单才能判定的新维度。

前端展示"已执行完成 / 交付物未验证"两种状态，不再用单一 completed 掩盖交付未达成。

## 4. 推荐路径

三个方案不互斥，建议按序演进：

1. **短期**（本次 Bug 已做）：Layer 1 + Layer 2，先堵住常见弱化路径。
2. **中期**（方案 B）：抽取与规划解耦 + 双向覆盖检查，降低"自洽少报"概率。
3. **长期**（方案 A + C）：API 增加 `required_operations` 契约，完成语义分层 `deliverable_met`，前端如实展示。这是真正的根治，需要前后端契约同步评审。

## 5. 待评审问题清单

- [ ] required_operations 的 schema 长什么样？tool + operation + path 的粒度是否够用？
- [ ] 是否允许用户声明 "operation": "*"（任意操作）？还是必须精确枚举？
- [ ] 与 `failure_policy`（D1）如何统一？required_operations 是 deliverable 层，failure_policy 是 step 层。
- [ ] 前端如何让用户便捷声明 required_operations，还是由对话层自动抽取后透传？
- [ ] 向后兼容：旧请求无 required_operations 时，回退到当前"仅机械完成"语义。
- [ ] 该改造涉及 L1（事件 schema）、L6（API）、L7（前端）跨层，需按 AGENTS.md §3.1 分层推进。

---

*本文档仅记录架构改造方向与待决策项，不包含实现。实现须在架构评审通过、AGENTS.md §3.4 审查完成后另起会话推进。*
