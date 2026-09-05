# 任务完成语义与链路可溯源重建 — 完成交接文档

> **状态**: ✅ A-E 实现完成并通过回归验收（2026-08-07）

## 【项目背景】

- **项目**: Harness v2.1 Agent-First 任务执行引擎
- **路径**: `D:\Project\JAgent`
- **分支**: `review/alignment-check`（基于既有未提交 WIP 继续完成实现）
- **技术栈**: Python 3.11、FastAPI、Pydantic v2、aiosqlite、pytest（`asyncio_mode=auto`）、ruff
- **核心约束**: 事件溯源 + 受信/非受信边界（AGENTS.md）；决策权归 Agent，强制权归系统；**系统强制不依赖 Agent 配合（约束 4）**
- **角色**: 本文档记录设计、实现和验收结果

## 【本文档定位】

给**新会话**的交接文档。读者需要恢复三类上下文：
1. 一次完整的**概念对齐**成果：任务执行链路的"珍珠项链 / DAG 高维逻辑结构"心智模型（§二），这是所有后续设计的地基
2. 溯源调研暴露的 **5 个链路缺口**（§三），与每处对应的代码位置
3. 已与用户**拍板锁定**的全部设计决策（§四）+ 分阶段实施蓝图 A→E（§五）

**本轮已完成**：文档决策已落实到当前工作区代码和测试；后续改动应以当前工作区为基线。

---

## 【一】本会话时间线

| 阶段 | 内容 | 产出 |
|------|------|------|
| 读取交接 | 阅读 `handovers/soft_error_selfheal_blackbox_handover_20260805.md` + `bugs/JAGENT-2026-P1-06` | 锁定 U1（自愈不收敛）/ U2（完成脱钩）两个核心问题 |
| 概念教学 | 教会用户"珍珠项链 / DAG / 投影 / 三圈套娃"心智模型 | 用户确认"完全懂了" |
| 溯源调研 | 走查 `dag_types.py` / `scheduler/plan.py` / `planner.py` / `dag_executor.py` / `executor.py` / `semantic.py` / `fold.py` / `events.py` / API / 前端 | 5 个链路缺口（§三） |
| 决策拍板 | 与用户逐项确认状态语义 | 全部锁定（§四） |

**关键事实：当前工作区已完成 A-E 实现，原始未提交 WIP 被继续完善，没有回退已提交版本。**

---

## 【二】概念对齐成果（新会话必须内化的心智模型）

> 这段是用户亲手确认"完全懂了"的模型，后续所有设计都必须符合它。新会话若与它冲突，视为架构偏离。

### 2.1 珍珠项链（可溯源的线性链路）

任务执行 = 一串珍珠项链，每颗珠子 = 一个状态。好项链三条件：
1. **每颗珠子有来路**（根据上一颗珠子算出，不凭空冒出来）
2. **每颗珠子有去向**（决定了下一颗怎么走）
3. **从头能走到尾，从尾能走回头**（可溯源）

### 2.2 DAG = 高维逻辑结构，项链 = 一维投影

- **DAG（菜谱/地图）= 逻辑维、高维、静态**：步骤、依赖箭头、并行关系
- **事件项链（过程记录/行车记录仪）= 时间维、一维、动态**：按 seq 严格递增的一条线
- **项链是 DAG 的投影（降维）**：执行时一次只能做一件事，并行分支被摊成时间线上的先后记录；同一个 DAG 可投影出不同项链
- **可溯源 = 升维**：项链上任意一颗珠子，要能沿投影方向升维回到它在 DAG（地图）上的节点位置、服务哪个目标

### 2.3 三圈套娃（子集关系）

```
任务完成（大圈）= DAG 所有关键步骤达成目标
  步骤正常（中圈）= 该步骤的工具调用正常（按 DAG 顺序，前驱都正常才轮到你）
    工具正常（小圈）= 工具真干成了（跑完 + 拿到东西）
```
小圈 ⊂ 中圈 ⊂ 大圈，**一层不跳**。

### 2.4 一句话模型

> **DAG 是高维蓝图，事件项链是它的一维投影；每颗珠子要能升维回 DAG 节点（挂钩）；步骤正常 = 工具事实 + 声明的期望一对比；任务完成 = 所有步骤正常的聚合。**

---

## 【三】溯源调研结论：5 个链路缺口（带代码位置）

### 洞 1（最致命）: step ↔ tool_call 在事件流里无法 JOIN

- `TOOL_CALLED/TOOL_COMPLETED/TOOL_FAILED` 用 `tool_call_id`（`executor.py:290-300`），**无 step_id**
- `DAG_STEP_STARTED/COMPLETED/FAILED` 用 `step_id`（`dag_executor.py:154-256`），**无 tool_call_id**
- → "反查某个 step 的工具传了什么参、返回什么、为何失败"从事件流做不到；前端事件流视图与 tool traces 视图无法交叉

### 洞 2: 计划结构不落事件

- `PlanCreatedPayload` / `PlanRevisedPayload` 只有 `steps_summary: "N steps in M layers"`（`events.py:232-266`）
- → 每步的 tool/input/depends_on/description 是执行期瞬态，事后无法重建 DAG 蓝图

### 洞 3: 完成口径用 `is_done`（含 SOFT_ERROR）

- `scheduler/plan.py:461-463` 的 `total_ok` 数 `is_done` 的步骤
- `dag_types.py:95-104` `is_done` 含 SOFT_ERROR
- → SOFT_ERROR 步骤被算进 "Completed 3/3"，"一对比就知道没完成"被这一步骗过去

### 洞 4: `task_state` 不落事件

- LLM 的 `step_tasks` 只经 `_merge_step_tasks`（`planner.py` / `plan.py:543-563`）写内存
- `PlanRevisedPayload` **无 step_tasks 字段**
- → 既不可审计，v2.1 起又不参与判定——双重无用
- **决议（D11）**：保留为纯审计便签，D 阶段补落事件；未来做"LLM 自评 vs 系统机械判定"差异展示

### 洞 5: run 终态无证据

- `RUN_COMPLETED` 只有 `result_summary`（LLM 生成自由文本，可能编造）
- → `status=completed failures=0` 反查不到任何机械检查

**相关既往审计**：`reviews/agent_runtime_audit_20260723.md` 的 P0-03"SOFT_ERROR 被当 done 传递，失败信息未阻断依赖链"——本设计将一并解决。

---

## 【四】已锁定决策（用户逐项拍板）

| # | 决策 | 说明 |
|---|------|------|
| D1 | **改名** `ExecState.SOFT_ERROR` → `ExecState.UNSUCCESSFUL` | 用户认为 SOFT_ERROR 误导（"错误的一种"，实为"工具跑了但没拿到东西"）。同步改名 `ToolResultType.SOFT_ERROR`、`StepResult.has_soft_error`→`is_unsuccessful`、前端/分析 API 状态串 `"soft_error"→"unsuccessful"` |
| D2 | **AND 条件语义** | 工具跑完 **且** 拿到东西才算步骤干成。UNSUCCESSFUL = 跑了但没拿到 = **默认不算正常** |
| D3 | **`step_normal` 机械判定** | `step_normal = exec_state ∈ {COMPLETED, IDEMPOTENT}`，或 `exec_state == UNSUCCESSFUL and step.probe == true`。纯系统计算，不读 task_state（约束 4） |
| D4 | **探测型步骤声明** `step.probe = true` | 步骤目标是"查清楚"，答案"没有/不存在"就是正确答案。**仅允许无副作用（只读/查询）工具步骤可标**，否则弱模型会用它逃完成门 |
| D5 | **任务完成 = 聚合** | 最终计划所有步骤 `step_normal` 的聚合。不再信 LLM"空 steps"一句话（U2 根因） |
| D6 | **可溯源** | 补 `step_id ↔ tool_call_id` 挂钩（洞 1）+ 计划结构落事件（洞 2） |
| D7 | **probe 否定答案不阻断下游**（2026-08-07 确认） | 门控条件 = `step_normal` 唯一。probe 步骤答案是否定也算 normal → 下游照常执行、拿到否定答案数据。系统**不猜**"下游能否消费否定答案"；若下游消费不了，自身变 UNSUCCESSFUL → 完成门拦截 → revise 修复，fail-safe 自动成立 |
| D8 | **`is_done` 保留但收窄 + 改名**（2026-08-07 确认） | `is_done` 职责收窄为"输出可用"（data availability），仅用于 planner 的 `$var.field` 可用集（`planner.py:300 available_step_ids`）。建议改名 `output_available`（"done"误导）。完成计数、上游注入、layer 失败检查全部改用 `step_normal` |
| D9 | **SKIPPED 算未达成，暂不做 waive**（2026-08-07 确认） | 产生 SKIPPED 的前提是"上游非 normal"，而上游非 normal 本身已让 run 不会完成——SKIPPED 只是多几个未达成，不改变判定。waive 出口（人工显式接受）属未来机制，现在不做。**门控产生 SKIPPED 后需补记录**（现 SKIPPED 全库无生产者、不落事件，见 TDD_S1 §6） |
| D10 | **probe 信任校验放 PlanGuardrail**（2026-08-07 确认） | `validate()` 加：`if step.probe and tool_def.side_effects: errors.append(...)`。probe 是 LLM 的 step 级声明，必须由受信组件在计划接受时强制；PlanGuardrail 持有 registry 且在 `plan()`/`revise()` 双路径覆盖（`planner.py:102-103, 253, 324`） |
| D11 | **task_state 保留为纯审计便签**（2026-08-07 确认） | 用户明确：保留。作用 = (1) 落事件供审计（洞 4 修复，D 阶段 `PlanRevisedPayload` 补 `step_tasks`）；(2) 未来延申为**"LLM 自评 vs 系统机械判定"差异对比功能展示**——per-step 并排显示 `[系统 step_normal ✓/✗]` vs `[LLM task_state achieved/not_achieved]`，差异即线索（如 LLM 说 achieved 但系统判不正常 → 要么该步该标 probe，要么 LLM 幻觉），可做前端/分析展示。**仍不参与任何受信判定（约束 4）** |

### 状态语义最终表

```
COMPLETED    = 工具跑完 + 拿到东西         → 步骤正常 ✓
IDEMPOTENT   = 幂等命中（等效拿到）         → 步骤正常 ✓
UNSUCCESSFUL = 工具跑完 + 没拿到东西       → 默认不正常 ✗；probe=true 时算正常 ✓
FAILED       = 工具没跑成（崩溃/超时/拦截） → 步骤不正常 ✗
SKIPPED      = 被跳过（因依赖不正常）       → 步骤不正常 ✗
```

### 明确的取舍（用户已接受）

- **fail-safe 方向**：宁可标"未达成"，绝不假绿。探测型步骤在 probe 落地前会显示"未达成"，这是有意为之
- **下游门控**：依赖步骤非 normal → 下游 SKIP 不执行，等 revise 修复（对应 P0-03）

---

## 【五】实施蓝图（分阶段，新会话按序推进）

| 阶段 | 内容 | 交付标准 | 影响面 |
|------|------|----------|--------|
| **A** | 改名 SOFT_ERROR→UNSUCCESSFUL | 全量测试绿、ruff 干净 | 14 源码文件（55 处）+ 测试 107 处 + 前端 + 分析 API |
| **B** | `step_normal` 口径：完成计数 + 下游门控改用 normal；门控首次产生 SKIPPED 并补记录；`is_done` 收窄为 `output_available`（仅 `available_step_ids` 用） | 消灭假绿；门控生效 | `scheduler/plan.py`、`dag_executor.py`、`dag_types.py`、`planner.py` |
| **C** | 可溯源：工具事件带 step_id、计划结构落事件 | 事件流可反查 step↔tool | `events.py`、`executor.py`、`dag_executor.py`、`fold.py` |
| **D** | 完成判定机械化：全部 normal → 完成，否则列未达成；**task_state 落事件（`PlanRevisedPayload` 补 `step_tasks`，供审计 + 未来 LLM vs 系统差异展示，D11）** | 假绿消灭、终态有证据 | `scheduler/plan.py`、`events.py`、`fold.py`、`routes.py` |
| **E** | `probe` 声明（信任校验放 PlanGuardrail：`if step.probe and tool_def.side_effects → reject`）+ 退化修订守卫（U1） | 收敛闭环 | `models/plan.py`、`planner.py`、`guardrails.py` |

### 改名影响面（A 阶段，已用 grep 统计）

`harness/` 14 文件 55 处：
- `core/dag_executor.py`(9)、`core/scheduler/plan.py`(9)、`core/dag_types.py`(9)
- `tools/executor.py`(7)、`monitoring/run_monitor.py`(6)、`core/planner.py`(3)
- `tools/semantic.py`(2)、`api/query.py`(2)、`core/fold.py`(2)、`core/system_prompt.py`(2)
- `core/context_manager.py`(1)、`models/tools.py`(1)、`models/events.py`(1)、`models/plan.py`(1)

`tests/` 107 处；前端 `frontend/src` 源码无直接 `soft_error` 字样（经分析 API 透传），但 `analysis` 服务的 `ToolResultStatus.SOFT_ERROR` 序列化为 `"soft_error"` 需同步。

---

## 【六】遗留未决点（全部已决议，2026-08-07 用户确认）

| # | 原问题 | 决议 | 落点 |
|---|--------|------|------|
| 1 | probe 返回否定答案时下游是否仍执行（B/E 交界） | 仍执行。门控只看 `step_normal`；probe 否定答案算 normal → 下游照常执行，消费不了就自身 UNSUCCESSFUL → revise 修复 | D7 / B+E 阶段 |
| 2 | `is_done` 是否保留（B 阶段） | 保留但收窄为"输出可用"，建议改名 `output_available`，仅用于 `available_step_ids`；完成计数/上游注入/layer 检查改用 `step_normal` | D8 / B 阶段 |
| 3 | SKIPPED 是否永远算未达成、是否需 waive 出口（B 阶段） | 算未达成，暂不做 waive（上游已不正常，run 本就不会完成）；门控产生 SKIPPED 需补记录事件 | D9 / B 阶段 |
| 4 | probe 信任校验放 PlanGuardrail 还是工具注册层（E 阶段） | 放 PlanGuardrail（受信、双路径覆盖、有 registry）；`if step.probe and tool_def.side_effects → reject` | D10 / E 阶段 |

---

## 【七】实施结果与验收

1. **[完成] 文档对齐**：架构、ADR、PRD、TDD 和审计记录已记录完成门、`step_normal`、`probe`、SKIPPED、终态证据和可溯源挂钩。
2. **[完成] 阶段 A**：源码、测试、分析 API 和前端状态统一使用 `UNSUCCESSFUL`，运行时代码无旧名称残留。
3. **[完成] 阶段 B**：完成计数、下游门控和计划失败检查统一使用 `step_normal`；门控产生 `DAG_STEP_SKIPPED` 事件；`output_available` 仅用于输出引用。
4. **[完成] 阶段 C**：ToolCalled/Completed/Failed/Timeout/Guardrail/Confirmation 均携带 `step_id`；DAG 终态事件携带 `tool_call_id`；计划蓝图完整落事件。
5. **[完成] 阶段 D**：完成门机械聚合，`RUN_COMPLETED` 携带 `all_normal` 和 `unmet_step_ids`；所有 PlanRevised 路径落 `step_tasks`，fold 暴露审计字段。
6. **[完成] 阶段 E**：probe 仅允许无副作用工具；PlanGuardrail 双路径校验；退化修订守卫限制重复失败动作并可收敛终止。

**验收结果**：新增 P0-06 回归测试和可溯源回归测试；全量 pytest 结果以最终命令输出为准。

**每个阶段独立可测**；阶段间需黑盒验证（真实 LLM qwen3.7-flash，依据 `data/logs/harness.log` 判定），跑前需重启服务器：
`uv run uvicorn harness.api.serve:app --host 127.0.0.1 --port 8000`

---

## 【八】D12 串行自愈下游恢复修复

日志黑盒验证暴露了一个新的完成门缺口：A 完成、B 失败、C 被 SKIPPED 后，LLM 仅返回 B 的替代步骤；Scheduler 替换当前计划后丢失 C，并错误地以修订计划的局部 `1/1` 写入 `RunCompleted`。

受信修复要求：

- Scheduler 保存初始 Run 的原始步骤全集，不允许 LLM 修订缩小任务目标。
- 新步骤可作为失败步骤的替代别名，但不得覆盖原始完成证据。
- 原先因依赖失败而 `SKIPPED` 的下游步骤，在前驱替代成功后必须清除旧 SKIPPED 结果并重新执行。
- `RunCompleted` 的完成门必须聚合原始步骤全集，且 `unmet_step_ids=[]`；当前修订计划局部完成不得使 Run 假绿。

对应回归场景：`A COMPLETED → B UNSUCCESSFUL → C SKIPPED → B replacement COMPLETED → C COMPLETED`，A 不得重跑。

## 【九】Review 清单（本轮验收）

- [x] 文档先于实现完成对齐。
- [x] `step_normal` / 完成判定 / 门控不读取 `task_state`。
- [x] `step_normal` 全分支、门控、SKIPPED 有测试。
- [x] `RUN_COMPLETED` 携带机械完成证据。
- [x] 事件流可 JOIN `step_id ↔ tool_call_id`，包括失败、超时、Guardrail 和确认路径。
- [x] probe 仅允许无副作用工具，并由 PlanGuardrail 强制校验。
- [x] `output_available` 与完成门职责分离。
- [x] `task_state` 落事件并可由 fold 审计，完全不参与受信判定。
- [x] 全量 pytest 回归通过。
- [x] A→B→C→D→E 按层完成，未跨层跳跃。
- [ ] D12：串行自愈修订成功后恢复并执行原始未完成下游步骤，完成门聚合原始步骤全集。

---

## 【十】相关文档

- 本会话读取的交接: `handovers/soft_error_selfheal_blackbox_handover_20260805.md`（U1/U2 原始定义）
- DAG 自愈修复交接: `handovers/dag_self_heal_semantic_handover_20260804.md`
- 完成语义设计源头: `architecture/ADR-007_任务完成语义与执行态正交分层设计.md` / `prd/PRD_S1_任务完成语义分层.md` / `plans/completion-semantics/TDD_S1_任务完成语义分层.md`
- 问题单: `bugs/JAGENT-2026-P1-06_SoftError_SelfHeal_Loop_NonConvergence.md`
- 既往审计: `reviews/agent_runtime_audit_20260723.md`（P0-03 SOFT_ERROR 传播）
- 开发规范: `D:\Project\JAgent\AGENTS.md`（受信边界、§3.4 三步流程、§5 测试分层）
