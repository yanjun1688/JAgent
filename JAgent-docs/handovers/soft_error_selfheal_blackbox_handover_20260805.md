# Soft-Error 自愈分支（分支 A）黑盒验证 + Answer 接地修复 — 交接文档

## 【项目背景】

- **项目**: Harness v2.1 Agent-First 任务执行引擎
- **路径**: `D:\Project\JAgent`
- **分支**: `review/alignment-check`（大量未提交 WIP）
- **技术栈**: Python 3.11、FastAPI、Pydantic v2、aiosqlite、pytest（`asyncio_mode=auto`）、ruff
- **核心约束**: 事件溯源 + 受信/非受信边界（AGENTS.md）；决策权归 Agent，强制权归系统
- **测试方式**: 纯黑盒（真实 LLM qwen3.7-flash-2026-07-15），依据 `data/logs/harness.log` 判定
- **会话触发点**: 验证 soft-error 驱动的自愈分支 A（`[self-heal] Soft-error revise returned N steps → re-executing`）

## 【本文档定位】

给新会话的交接文档。读者需要恢复三类上下文：
1. 今晚黑盒验证的最终结论：分支 A 已可靠触发，但自愈循环无法收敛（核心未决问题）
2. 修复的 4 个问题：guardrail 依赖校验不一致、revise 系统态缺步骤目标、answer 阶段编造执行结果、logging fmtkv 占位符 arity 崩溃
3. Langfuse 自动上传已关闭；后续黑盒跑前需重启服务器

---

## 【一】黑盒验证时间线（按 run_id）

| run | 结果 | 关键事件 | 说明 |
|-----|------|----------|------|
| 42e573e8 | failed | `Cannot resolve '$s1.status'` | guardrail 死锁 → 触发依赖校验修复 |
| 09659299 | completed | 分支 B（empty→task complete） | guardrail 修复生效 |
| 994b0867 | completed | 层失败自愈 + 分支 B×2 | 依赖引用正确路径验证 |
| 02590e8d | completed | 分支 B + **answer 编造"已创建并成功读取"** | 发现 answer 幻觉（dataset.csv 实际未创建） |
| a0f8810f | failed | **分支 A×5**（round 1-5 全 `re-executing`） | revise 反复返回"重读"缺失文件，从不创建；round5 返回 3 步时被 `Self-heal exceeded 5 attempts` 拦停 |
| 163009eb | completed | 分支 B（empty），s1 标 `not_achieved` 却判完成 | answer 编造修订行为（"明确必须补充创建步骤"） |
| ed1df97b | completed | **分支 A×4** → round5 empty | 约束"禁止纯读取"生效但仍不创建；answer 首次诚实报告"交付物未达成" |

**结论**: 分支 A（soft-error 重执行）机制已验证可靠触发；系统侧强制（breaker 5 次上限、answer 接地）均按设计工作。

---

## 【二】本次修复的 4 个问题

### 问题 1: guardrail 依赖校验与执行时上游构建不一致（会会话早段）

- **现象**: 初始计划引用未执行步骤的输出（`$s1.status`）→ `VariableResolutionError` → 层失败；revise 又被 guardrail 以 "depends on unknown step" 拒绝 → 死锁。
- **根因**: guardrail 用 `should_not_rerun`（排除 SOFT_ERROR）校验依赖，执行时用 `is_done`（含 SOFT_ERROR）构建上游 → 语义不一致。
- **修复**: `models/plan.py::topological_sort(completed_step_ids, external_deps)` 支持 external 依赖（soft-error 输出可用、不产生调度边）；`planner.py::PlanGuardrail.validate(plan, completed_step_ids, available_step_ids)`；`scheduler/plan.py` 传 `external_deps=available_ids`。
- **测试**: `tests/test_planner_revise_rerun.py` +6 回归用例。

### 问题 2: revise 系统态缺步骤业务目标（分支 B 误判的推手之一）

- **现象**: revise 系统态渲染 `task={task_state}`（来自 LLM 的 step_tasks 合并），**从不渲染步骤 `description`**。schema 收集了 `description`（planner.py:526）但 build_dag_status_text 不显示 → revise LLM 只能靠 plan intent 猜步骤目标 → "尝试读取不存在文件"被判为 achieved。
- **修复**: `dag_executor.py::build_dag_status_text` 增加 `Task: <step.description>` 行（截断 80 字符，空则不渲染）。
- **测试**: `tests/test_planner_revise_rerun.py` +2（Task 渲染、空 description 不渲染）。

### 问题 3: answer 阶段编造执行结果与修订行为

- **现象**: (a) 02590e8d — answer 声称"已创建 dataset.csv 并重新读取成功"，实际沙箱无该文件，无任何工具调用；(b) 163009eb — answer 声称修订"明确必须补充创建步骤"，实际修订返回空步骤。
- **根因**: answer 是非受信组件，_ANSWER_PROMPT 无任何落地约束；上下文里 tool_results 未标权威、缺修订结果 → LLM 自由发挥。
- **修复**:
  - `_ANSWER_PROMPT` 加 4 条接地规则（执行陈述必须可溯源、禁止编造工具调用、失败未重跑须如实报告、`[Run outcome]` 不得矛盾）。
  - `planner.py::generate_answer`：tool_results 标为 AUTHORITATIVE 唯一记录 + 顶部 `[Execution digest]`；从 `state.latest_plan`（fold 自 PLAN_REVISED）注入 `[Run outcome]`（revision_reason / remaining_steps_summary / status）。
- **测试**: `tests/test_planner.py` +2（outcome 注入、无 plan 时不注入）。
- **效果**: ed1df97b 的 answer 首次诚实报告"dataset.csv 未创建、任务尚未彻底完成"，不再编造。

### 问题 4: logging fmtkv 占位符 arity 崩溃（致命）

- **现象**: run 执行中整个进程被异常打断——日志 handler 抛 `TypeError: not enough arguments for format string`，沿 `event_store.append_event → executor.execute → dag_executor` 冒泡，run 被杀。
- **根因**: `run_monitor.py` 3 处 `_log_*.info/debug("... %s %s ...", fmtkv(...))`——`fmtkv()` 返回**单个**字符串，格式串却含多个 `%s`。logging `msg % args` 参数不足即抛错。
- **修复**: 3 处格式串改为单 `%s`（run_monitor.py:122/167/286）。
- **测试**: 新增 `tests/test_log_fmtkv_arity.py`——静态扫描全库 `fmtkv` 单参配多占位符模式，再犯即红。

---

## 【三】未决问题（核心）

### U1: Soft-error 自愈循环无法收敛（模型行为问题，未修）

- **现象**: 初始 read 缺失文件 → soft-error → revise 每轮都返回"再次读取"（新 description："Attempt to read ... again"），从不创建文件。round 1-4 循环，round 5 要么放弃（返回空→completed 但交付物未达），要么被 breaker 5 次上限拦停（failed）。
- **已尝试**: 意图加"禁止返回纯读取步骤，必须包含创建步骤"的硬约束 → 反而让 revise 判"任务完成"返回空（两条指令在 LLM 眼里互相打架：初始"严禁创建" vs 修订"必须创建"）。
- **根因分析**:
  1. RERUN RULES 明确指示"soft_error 步骤要保留重试"，LLM 走最小改动路径——重读，而非创建前置缺失物。
  2. 163009eb/ed1df97b 中 LLM 自相矛盾：把 s1 标 `not_achieved` 却返回空步骤判"task complete"，系统照单全收 → run 以 `status=completed failures=0` 结束但硬性交付未达成。
- **候选方向（供决策，未实施）**:
  - A. 系统强制：若 end-of-plan soft-error 修订返回空但存在 `replan=MAYBE` 步骤，且步骤描述/意图含"必须成功"语义，则由受信层强制一轮创建型修订。**风险**: 无法语义化判断"soft-error 是否可接受"（09659299 的分支 B 是正确结局），会破坏该路径。
  - B. 只堵自相矛盾：修订返回空 + 自标 `not_achieved` 时拒绝接受，要求重新修订。**风险**: 仍是 LLM 判断，可靠性存疑。
  - C. 接受现状：分支 A 触发能力已验证，收敛性属于 LLM 策略质量，靠意图措辞优化（本轮已证可部分改善但不可靠）。
- **附注**: 每次循环约 1.5-3 分钟 LLM 调用，5 次上限是系统唯一兜底（约束 4 生效）。

### U2: run 完成状态与交付物达成脱钩

- 分支 B 判"task complete"后 run 标记 `status=completed failures=0`，即便意图声明了硬性交付且未达成。前端/操作员看到的是"成功"。是否在完成语义上区分"deliverable_met"需架构层决策（对应 ADR-007 完成语义分层）。

### D1（设计方向，未实施）: 计划期约束的边界 + failure_policy 声明式契约

延续 U1/U2 的讨论，关于"初版计划 DAG 能否加异常约束"：

**两层约束需区分**：
- **结构性契约（该管，已管）**: 依赖图合法、`$step_id.field` 引用可解析、引用对象可达（42e573e8 死锁已由 `available_step_ids` 修复）。纯机械校验，系统天然有权强制。
- **预测性判断（要谨慎）**: 计划期预判"该 read 必然失败"（路径当前不存在且无上游创建者）。**不建议系统武断拦截**——沙箱状态执行中会变；且合法探测型步骤（09659299 分支 B）恰恰需要"允许失败"，拦截等于替 Agent 判断"这步不该做"，越过受信边界。

**推荐的干净切入点——让"完成语义"变成声明式契约**（决策权归 Agent，强制权归系统）：
- 给步骤加 `failure_policy` 字段（planner LLM 声明）：`must_succeed`（必须成功 → 系统计划期强制该步有恢复路径或显式声明失败可接受，否则拒绝）或 `may_fail`（允许 soft-error → 分支 B 合法出口）。
- 系统不再猜"哪步是硬交付"，只强制 Agent 自己声明的规则。与 `side_effects` 声明模式同构，与 ADR-007 完成语义分层打通。
- 一条主线：**计划期约束从"系统判断计划好不好"转向"系统强制 Agent 声明过的规则"**，既守住受信边界，又让弱模型无处钻。

**闭环关系**: 收敛循环（U1）的候选方案 B + failure_policy 契约 + U2 的 deliverable_met 语义，三者应作为同一设计迭代推进，而非各自为战。

---

## 【四】环境变更

- `.env`：`LANGFUSE_ENABLED=true → false`（关闭 Langfuse 自动上传，next 启动生效）
- 服务器需重启才能加载代码改动：`uv run uvicorn harness.api.serve:app --host 127.0.0.1 --port 8000`

## 【五】测试与规范状态

- 全套 `uv run pytest`：**811 passed, 2 skipped**
- 会话新增测试 11 个：guardrail 拓扑 6、Task 渲染 2、answer outcome 2、fmtkv arity 1（其中 guardrail 6 个在早段已计入，因此测试总数仅从 806 增至 811）
- ruff：改动未引入新告警（基线 E501/import order 若干为预存）
- 已知基线风险：`llm_client.py` `httpx timeout=120.0` 硬超时、无重试（承诺修复，已推迟）
