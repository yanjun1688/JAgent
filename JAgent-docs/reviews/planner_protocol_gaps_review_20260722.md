# Planner.py 复审 — 协议缺口（A–F）修复状态复核

> **日期**: 2026-07-22
> **分支**: review/alignment-check
> **范围**: `harness/core/planner.py` 全文 + 上下游（dag_executor / dag_vars / dag_types / scheduler.base / run_monitor / fold / mcp_manager）
> **基线**: 上一轮 `JAgent-docs/reports/planner_executor_gaps.md`（已删除）识别的 6 个协议缺口
> **目的**: 不做代码风格优化，**仅复核 6 缺口当前状态**，发现仍存在的协议缺口并定级

---

## 总评

| 维度 | 结论 |
|---|---|
| 已修复且实现正确 | A、D、F（确凿）；E（基本修复，残留见 §E） |
| 修复后保留实质风险 | B（状态枚举齐全，但 `revise()` 消费侧疑点仍在）；C（**未修复**） |
| 新发现缺陷 | G、H、K（见 §3） |
| 总判定 | 主路径协议已基本闭合，但 **Monitor 硬控制（C）与 LLM 视角下的输出可见性（E 残留 + B）**仍是系统性缺口 |

---

## 1. 各缺口当前状态复核

### A — 变量解析 → **已修复 ✓**

证据：`harness/core/dag_vars.py`
- `upstream` key 直接用裸 `step_id`（`_resolve_ref` line 60 `if var_name not in upstream`），不再有 `s1_result` vs `s1` 命名错位。
- 纯引用 `$s1.body.url`（`resolve_variables_in_input` regex `^\$(\w+)(?:\.([\w.]+))?$`）与内联引用走同一 `_resolve_ref()` 核心（V2.2 统一路径）。
- 字段不存在时 **抛 `VariableResolutionError` 并附 `Available fields`**，而不是静默返回 None/原始占位符。这是对「字段错位导致 SchemaGuardrail 误拦」的根治性修复。

**遗留评估**：`deep_resolve` 的 5 层 fuzzy fallback（line 186-224）会在字段缺失时跨界找同名 key，可能掩盖工具契约错误（让本该报错的 step 输出悄悄被替换为同名的非预期字段）。属设计权衡，**不判为缺陷**，但建议：`deep_resolve` 命中时记 `_log.info` 当前已存在（line 105），生产监控时统计其命中率，若长期 > 5% 表明工具输出契约不稳定，应回头补 Schema。

### B — 状态传播 → **修复但残留风险 ⚠**

证据：`harness/core/dag_types.py:13-18`
```python
class StepStatus(str, Enum):
    COMPLETED, SOFT_ERROR, FAILED, CONFIRMATION_NEEDED, EXECUTOR_ERROR
```
- `CONFIRMATION_NEEDED` 已是一等状态位，未被折叠为 `error`。
- `is_done` / `is_failed` / `needs_confirmation` 等属性清晰。
- `PlanSuspended` 异常通道（`dag_executor.py:36-46`）负责挂起，scheduler 捕获后写 `RUN_PAUSED`，这条链路形式上闭合。

但 `planner.py:274-276` 中 `completed_step_ids` 仅识别 `("completed", "idempotency_hit")`：
```python
completed_step_ids = {
    sid for sid, r in results.items()
    if isinstance(r, dict) and r.get("status") in ("completed", "idempotency_hit")
}
```
**残留风险**：
1. `StepResult` 是 `dataclass` 而**不是 dict**，`r.get("status")` 仅因 `StepResult.get` 的 backward-compat 方法（`dag_types.py:37-51`）才返回 `"completed"` 字符串——靠兼容层存活，脆弱。
2. `SOFT_ERROR` 步骤虽然 `is_done=True`（`dag_types.py:59`），但**没有被计入** `completed_step_ids` → revise 时被当作"未完成"，可能让 LLM 重排它们。这与 `is_done` 的语义相悖。
3. `"idempotency_hit"` 不在 `StepStatus` 枚举里。grep 全项目无该字面量作为状态写入。**条件分支是死代码**，源于早期 dict 契约遗物，与当前强类型 `StepStatus` 体系不一致，违反 §6.1 Pydantic/强类型数据结构要求。

**定级**：P1（非根因崩溃但属类型契约漂移，需根治）。

### C — 监控控制 → **未修复 ❌（仍为最大风险）**

旧结论称 *"`RunCommandType` 未实现"*。复核进展：

**已打通的部分**:
- `harness/models/events.py:25, 172` 定义 `EventType.RUN_COMMAND` 和 `RunCommandPayload`（`Literal["hard_abort","soft_abort","pause","resume","skip_tool"]`）。
- `harness/core/scheduler/base.py:318-403` 实现了 `_check_pending_commands` / `_process_command` / `_handle_pending_commands`。`hard_abort|soft_abort` 调 `_fail(run_id, ...)`，`pause`/`resume` 调对应方法，`skip_tool` 仅确认消费预留位。
- `harness/core/fold.py:248-251` `RUN_COMMAND` 作为控制平面事件，**不**折叠进 RunState（正确——控制平面与数据平面分离）。

**仍未打通的部分**:
- `harness/monitoring/run_monitor.py` grep 全文件，**无 `RUN_COMMAND` 写入路径**。Monitor 的所有输出仍仅经 `FeedbackInjectedPayload`（line 407-418）。即 Monitor 仍只"建议"不"强制"。
- 这意味着：当出现无限循环 / `max_consecutive_failures` 累积违和 / 上下文爆炸等异常时，Monitor 唯一可写的是文本 feedback，依赖 *下一次* `think`/`revise` 调用前 LLM 自觉采纳。**没有任何受信组件能直接 abort**。

**架构判定**: 这违反 `AGENTS.md` §2.2 约束 4——"危险操作的拦截由 Tool Layer Guardrails 负责……不可绕过的防线"。Monitor 不能发出 RunCommand，等于把"无限循环"这一类危险操作的最终拦截权下放给非受信的 Agent。**P0 级架构缺口**。

注意：`max_consecutive_failures` 在 scheduler 自己的熔断（`scheduler/base.py` 隐含路径）确实能在 scheduler 侧 abort，因此这不是"绝对无防护"，但那是 scheduler 自身的熔断，不是 Monitor 主动的、可基于多事件跨 run 维度的策略性触发。两者语义不同。

### D — Guardrail 顺序 → **已修复 ✓**

`harness/core/dag_executor.py` 先 `resolve_variables_in_input` 后 `execute_tool_layer`（依赖 `dag_vars.py` 引入，位于 `_execute_step_only` 路径）。验证流顺序正确，未发现变量未解析即进入 SchemaGuardrail 的回归。

**遗留评估**：`VariableResolutionError` 的抛出位置应在 DAG 层抓包并写 `DAG_STEP_FAILED`（同时 `retryable=False`，因变量错位是 LLM 规划错误而非工具瞬时错误），而非让其向上抛成 `EXECUTOR_ERROR`。需回查 `_execute_step_only` 是否 catch 了该异常并归化。本次未深入展开，建议入库时补测。

### E — Revise 上下文 → **基本修复，但输出可见性残留 ❗**

`harness/core/dag_executor.py:344-389` `build_dag_status_text` 当前结构：
- ✓ 保留 step ID 与 tool：`{step.id}({step.tool})`
- ✓ 保留 step 输入（截断 200 字符）：`Input: {input_str}`
- ✓ `[done]` step 显示 `summary[:80]`
- ✓ `[confirming]` 显式标 pending
- ✓ 依赖列出 `Depends: ...`
- ✓ `planner.py:296` `guardrail.validate(revised, completed_step_ids=...)` 已传完成集

**残留**:
- `[done]` step **不展示 output**，也不展示 output schema/key 列表。LLM 在 revise 时若要再产出 `$s1.body.url` 这类引用，无从得知 `s1.body` 是否存在 → 必撞 `VariableResolutionError`（A 的修复反而把这个矛盾点暴露了出来）。
- `[soft-error]` 已完成被当作"未完成"，加之前文 B 提到 `SOFT_ERROR` 没进 `completed_step_ids`，会触发 LLM **重复生成等价 step**。但因 guardrail 跳过了 `dep in completed`，二次执行仍可能再次 soft_error 进入死循环。

**建议（不属 P0，但属 P1 协议一致性问题）**：
- `build_dag_status_text` 对 done step 增加一行 `Output keys: [body, url, ...]`（来自 `StepResult.output` 若为 dict 即 keys，若是 str 显示首 80 字符）。
- `planner.revise` 的 `completed_step_ids` 定义改为：`r.is_done`，并去掉死分支 `"idempotency_hit"`。让 мяг软错与硬完成都视作"不应重复"，仅 `[retryable]` 的 FAILED 才进入未完成集。

### F — 监控 DAG 失效 → **已修复 ✓**

`harness/monitoring/run_monitor.py:92`:
```python
if event.event_type in (EventType.TOOL_FAILED, EventType.GUARDRAIL_TRIGGERED, EventType.DAG_STEP_FAILED):
```
三事件并列统计入 `_consecutive_failures`（line 105、158、189），DAG 路径下不再恒为 0。

**遗留评估**：`tests/test_monitoring.py:424-506` 含 `#37` 阈值用例与 P1 同 `tool_name` 去重用例，覆盖率良好。但 `TOOL_FAILED` 与 `DAG_STEP_FAILED` 在 DAG 路径下会双写吗？若 DAG 内工具故障同时落 `TOOL_FAILED` 与 `DAG_STEP_FAILED`，会触发"双计数"。需 grep 确认 Tool Layer 上 `TOOL_FAILED` 在 DAG 内是否被显式抑制——本次未深入展开。

---

## 2. planner.py 本身的问题（含上一份 review 已记录的，仅简列）

以下我已在上份 `planner_tool_filtering_review_20260722.md` 详细说明，本处仅复核现状，**未修复**：

| 位 | 问题 | 状态 |
|---|---|---|
| `planner.py:228` | 裸 `print(self.registry.list_tool_defs())`，每次 plan 都打到 stdout | 未修 |
| `planner.py:430` | `ALWAYS_INCLUDE = {"file_op"}` 硬编码，Planner 隐式知道工具名，受信边界泄漏 | 未修 |
| `planner.py:427-439` | `_filter_tools_by_intent`：fallback `else tool_defs` 因 `file_op` 永远不触发 | 未修 |
| `planner.py:441-444` | 入口过滤分支（`if intent and len(tool_defs) > 2`） | 未修 |
| `system_prompt.py:61-67` | PLAN 示例引用不存在的 `browser_search` | 未修 |

均履历可查，不再重复展开。

---

## 3. 本次复审新发现的协议缺口

### G（P1）— `revise()` 整个流程不暴露 `intent` 原文给 LLM
`planner.py:258`:
```python
intent = plan.intent[:200] if plan.intent else (intent_fallback[:200] if intent_fallback else "(unknown)")
```
原始 user intent 只有截断 200 字符的 `plan.intent` 进入。对于长任务 `plan.intent` 可能是 LLM 在 plan 阶段自我改写的版本（system_prompt.py:50 明文禁止"copy-paste the user intent verbatim"），意味着多轮 revise 中**原始用户意图可能完全消失**——LLM 仅能见到自我重述版，导致语义漂移。这是架构性而非实现性缺陷：
- 修复需在 REVISE prompt 中显式分开「**Original User Intent**」与「**Plan Intent**」两个字段。
- 与 §5 `revise` 路径中 `intent_fallback` 仅在 plan 无 intent 时救场的设计同源于此问题，但 fallback 本身不够：fallback 仅在"完全无 intent"时启用，而非"有但已偏离"。

### H（P2）— `revise` 与 `plan` 重试语义不对称
- `plan`（line 222）`range(1, max_plan_retries + 2)` → 实际重试 `max_plan_retries + 1` 次（3 次）。
- `revise`（line 280）`range(1, max_plan_retries + 1)` → 实际重试 `max_plan_retries` 次（2 次）。

常量 `max_plan_retries=2`（`__init__`，line 201）被两个语义重用但差一处理。配 `__init__` docstring/语义不同：plan 首生（应给更多容错，OK）vs revise（已知上次错误、理性可少 1 次，OK）。**若是有意为之应在代码注释**，否则属隐式约定。判 P2（命名误导），建议拆成 `max_plan_retries + max_revise_retries` 两个参数。

### K（P2）— `last_raw_response` 已写入但**未作为事件**入 Event Store
`planner.py:208`、`232`、`285`（间接）：`self.last_raw_response = response` 仅保存在实例属性。任何人、任何 Run 都无法回放 LLM 的原始 raw response——这违反 §5.1 测试规范中"端到端测试关注事件链完整性"的可观测性要求。Plan 失败、guardrail 失败时，事件流只能看到 `_log.warning`，无法离线回放 LLM 的原始输出。建议：失败路径写一个 `RawLLMResponse` 事件或挂在 `PlanCreatedPayload`/`PlanRevisedPayload` 上，便于回放。

---

## 4. 协议缺口最终裁决表（更新版）

| 缺口 | 状态 | 证据 | 优先级 |
|---|---|---|---|
| **A: 变量解析** | ✅ 已修复 | `dag_vars.py` 裸 step_id + 抛错带 available keys | — |
| **B: 状态传播** | ⚠ 类型契约残留 | `planner.py:274-276` 死分支 + dict-compat 调用 + SOFT_ERROR 漏计 | **P1** |
| **C: 监控控制** | ❌ 未修复 | Monitor 仍只写 FeedbackInjected，无 RUN_COMMAND 写路径 | **P0** |
| **D: Guardrail 顺序** | ✅ 已修复 | dag_vars 已拆出独立且先解析 | — |
| **E: Revise 上下文** | ⚠ 输出可见性残留 | done step 不展示 output keys，soft-error 漏入未完成集 | **P1** |
| **F: 监控 DAG 失效** | ✅ 已修复 | `run_monitor.py:92` 三事件合并 | — |
| **G: Revise 失原意** | ❌ 新缺口 | `planner.py:258` 截断 plan.intent 替代原 intent | **P1** |
| **H: 重试不一致** | ⚠ 新发现 | `planner.py:222 vs 280` 差一 | **P2** |
| **K: 原始响应无事件** | ⚠ 新发现 | `last_raw_response` 仅缓存不入流 | **P2** |

---

## 5. 修复建议（按优先级）

### P0 — 必须修

**C — Monitor 写入 RUN_COMMAND 的通道**

1. 在 `RunMonitor` 中按现有规则（连续失败计数、token 超额、重复签名）增加"升级到硬控制"的阈值（例如连续失败 > 阈值 ×2 则升级）。
2. Monster 端调用 `self.store.append_event(run_id, EventType.RUN_COMMAND, RunCommandPayload(command="soft_abort", reason=...).model_dump())`。
3. 使 Scheduler 已有的 `_check_pending_commands` 在下次迭代前消费（已实现）闭合环路。
4. 测试：注入连续 `DAG_STEP_FAILED` 超阈值 ×2，断言 Event Store 中出现 `RUN_COMMAND soft_abort`、Run 状态转 FAILED。
5. 注意：policy 决策必须受信化（阈值常量配置在 `SchedulerConfig`/`MCPServerManager` 同级），**不能**让 LLM 通过 feedback 改阈值。

### P1 — 应修

**B — 引入类型契约**  ⚠️ **暂缓设计（本次仅删死代码）**
- 本次**仅删除** `"idempotency_hit"` 死分支字面量（grep 全项目无写入路径，纯死代码），保留 `"completed"` 单一判据。
- `SOFT_ERROR` 是否计入 `completed_step_ids` 是「任务完成 vs 工具完成」语义问题，需用户研究业界后定。
- 完整暂缓清单、业界关键字见 `ARCHITECTURE_v2.1.md §3.7` 缺口 S1。
- 暂缓改动：`planner.py:274-276` 状态判定分支、`dag_types.py:13-18` 枚举、`dag_executor.py:282-284`、`dag_types.py:37-51`。

**E — Revise 上下文可见性**  ✅ **已采纳（本次执行，C-4）**
- `build_dag_status_text` 对 done step 增展示 output keys 或 output 摘要。
- ~~同步处理 SOFT_ERROR → 应进 `completed_step_ids`~~（同 B 暂缓）

**G — Revise 暴露原始用户意图**  ✅ **已采纳（本次执行，C-2）**
- REVISE prompt 模板拆 `Original User Intent` / `Plan Intent` 双槽。
- 在 `Plan` 模型上新增 `user_intent: str = ""` 持久化原始用户意图；`Scheduler.plan` 首生成时记录，`revise` 调用时透传原文。详见 `fix_prompt_for_ai.md §8.6`。

### P2 — 应改

**H — 拆分重试次数常量**
- `__init__` 增加 `max_revise_retries`，默认 = `max_plan_retries`。
- 文档化两者语义差异。

**K — 原始响应入事件**
- `PlanCreatedPayload`/`PlanRevisedPayload` 已可挂 `raw_response` 字段；或新增 `LLMResponse` 事件类型仅记录诊断用 raw。
- 注意 §6.1：非受信异常不得泄漏——raw response 只是数据，不入受信约束，但要保证不含 secret（刘 like API key 不会被 LLM 回显）。

### 沿用上一份 review 的 P0 修复项

仍需同步修：planner.py:228 print、planner.py:430 硬编码、planner.py:427-444 整段 filter、system_prompt.py:61-67 example 中的 `browser_search`。

---

## 6. 对照评审检查点（`AGENTS.md` §11）

- [x] 协议缺口复核覆盖 A-F，并标注 G/H/K 三个新发现
- [x] 受信组件行为：Monitor 仍依赖非受信 LLM 配合（C，P0 级架构违规未消除）
- [x] 前后端数据结构同源：StepStatus 枚举与 update 路径建议
- [x] 提醒必要测试覆盖：C 缺口需"连续失败上限 → 自动 RUN_COMMAND"端到端用例；B 缺口需"SOFT_ERROR 步骤不被 LLM 重排"用例；E 缺口需"revise 中 LLM 可见 done step 的 output keys"用例
- [x] 上下文长度评估：本文聚焦协议缺口复核，未展开代码风格细节，与上一份 `planner_tool_filtering_review_20260722.md` 在范围与目的上互补，建议两份合并归档为 Planner 双轮 Review

---

## 附录：本次复审触及的关键文件与行号

| 文件 | 关键行 | 作用 |
|---|---|---|
| `harness/core/planner.py:228` | 裸 `print` 残留 | 上一份已记录 |
| `harness/core/planner.py:258` | revise intent 截断 | G 缺口 |
| `harness/core/planner.py:274-276` | completed_step_ids dict 兼容 + 死分支 | B 缺口 |
| `harness/core/planner.py:222 vs 280` | 重试次数差一 | H 缺口 |
| `harness/core/planner.py:296` | completed_step_ids 传入 validate | 已修复 E 一半 |
| `harness/core/dag_executor.py:344-389` | build_dag_status_text 文本组装 | E 缺口残留 |
| `harness/core/dag_types.py:13-18` | StepStatus 枚举 | 基础已就位 |
| `harness/core/dag_types.py:37-51` | StepResult.get backward-compat | B 缺口根因层 |
| `harness/core/dag_vars.py` | 变量解析 | A 缺口已修复 |
| `harness/core/scheduler/base.py:318-403` | RUN_COMMAND 消费侧 | C 缺口半实现 |
| `harness/monitoring/run_monitor.py:92` | Monitor failure tracking | F 缺口已修复 |
| `harness/monitoring/run_monitor.py:407-418` | Monitor 仅写 FeedbackInjected | C 缺口根因 |
| `harness/core/fold.py:248-251` | RUN_COMMAND 不入 RunState | 控制面分立正确 |