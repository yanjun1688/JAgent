# DESIGN v3.4 — 执行循环韧性与上下文证据治理

> **状态**：**已实现**（TDD 全绿：`1395 passed / 2 skipped` · ruff clean）。实现与本文部分草案存在**已知差异**（§6.1 事件清单、恢复分级"子 DAG replan"层、§8 若干端到端/故障注入测试缺口），已由产品确认**可接受**，详见 §11。
> **分支**：`feat/v3.4-execution-resilience`（与 `feat/playwright-mcp-convergence` 同 commit，功能改动均为同一份工作区未提交变更）
> **对应 PRD**：`JAgent-docs/prd/PRD_v3.4_执行循环韧性与上下文证据治理.md`
> **本文性质**：技术方案设计（根因独立复核 + 外部范式调研 + 候选方案 + 受信边界 + TDD/BDD 计划）
> **铁律**：所有结论以代码与真实事件流证据为准；**本文对 PRD 的根因归因有实质性修正**（见 §1）。

---

## 0. 阅读指引与门禁自查

PRD §0 要求本文必须包含：① 三条根因独立复核（可驳回，附证据）；② 外部范式调研；③ ≥2 候选方案对比 + 推荐 + 放弃理由；④ 受信/非受信边界分析；⑤ TDD+BDD 测试计划（含故障注入/回归）；⑥ 对事件溯源 / `fold_events` / Replay Inspector 的兼容性影响。对应章节：①→§1，②→§2，③→§3，④→§4，⑤→§8，⑥→§6/§7。

**证据来源**：
- 真实失败 run：`.harness.db` 中 `run_id=e05087b6…`，共 33 个事件（下文用 `seqN` 引用）。
- 代码引用格式：`file:line`。

---

## 1. 根因独立复核（可反驳 PRD）

复核方法：不采信 PRD 叙述，直接重建 run `e05087b6` 的完整事件流（33 事件）并逐条对照折叠/门控/调度代码。**结论：PRD 的三条根因方向部分成立，但 R1 的因果链与 R2 的"重跑/翻倍"描述在本 run 不成立；真凶与 PRD 叙述不同，且发现 PRD 未覆盖的更严重缺陷。**

### 1.1 事件流事实（run e05087b6）

| seq | 事件 | 关键事实 |
|---|---|---|
| 1 | RunStarted | 意图：查成都 15 天天气 + 成都→深圳机票，推荐出行日 |
| 2 | DeliveryContractsResolved | `contracts=[]`，**契约抽取超时**（`timed_out=true`）→ 本 run 无交付契约，门控只走机械维度 |
| 3 | AgentThought | Planner 产出 4 步 2 层 DAG：s1/s3 `browser navigate`（百度搜索天气/机票），s2/s4 `browser extract` 依赖 s1/s3 |
| 4 | PlanCreated | 4 steps / 2 layers |
| 5-8 | DagStepStarted×2, ToolCalled×2 | s1、s3 并行 browser |
| 9-10 | ToolCompleted | **browser 双双 `result_type=unsuccessful`**：`Browser unavailable on this event loop: asyncio subprocess is not supported (Windows SelectorEventLoop). Use ProactorEventLoopPolicy` |
| 11-12 | DagStepCompleted | s1/s3 `status=unsuccessful` |
| 13-14 | DagStepSkipped | s2/s4：`dep 's1'/'s3' not normal (exec_state=unsuccessful)` |
| 15 | PlanRevised | LLM 补丁：`remaining 2 steps`，**只把 s1（天气）换成 `http_request` open-meteo**；s2 换 http google |
| 16 | PlanCreated | 合并回 root 未完成步骤后 = 4 steps：s1/s2=http，**s3/s4 仍是 browser** |
| 17-22 | DagStepStarted×3, ToolCalled×3 | s1=http(open-meteo)、s2=http(google)、**s3=browser（同一幂等键，重放失败调用）** |
| 23 | ToolCompleted | s3 browser **再次 unsuccessful**（同一 Windows 事件循环错误） |
| 24 | FeedbackInjected | monitor：`browser failed 3 consecutive times`（high） |
| 25-26 | ToolCompleted | s2 google（200，大 HTML）、s1 open-meteo（200，**67796 字符 JSON**） |
| 27-29 | DagStepCompleted | s1=completed、s2=completed、**s3=unsuccessful** |
| 30 | EpisodeArchived | `original_tokens=31470 → compressed=309`，`archived_event_refs=[9,10]`（**裁的是第一次失败的 browser 调用**） |
| 31 | DagStepSkipped | s4：`dep 's3' not normal` |
| 32 | PlanRevised | LLM 报 `task complete`、`steps=[]`、`step_tasks={s1:achieved,s2:achieved}`（**谎称完成，无视 s3/s4**） |
| 33 | RunFailed | `final_error: Steps not achieved: s3, s4, declared_op#2:browser {...baidu 机票...}` |

### 1.2 R1（上下文压缩丢完成证据 → 门控误判 unmet）—— **部分驳回**

- ✅ **配置 bug 属实**：`harness/api/serve.py:218` 硬编码 `ContextManager(..., token_limit=3000, checkpoint_interval=10)`，而类默认 `token_limit=128_000`（`context_manager.py:46`）。阈值随之被压到压缩 2100 / 紧急 2700，导致 seq30 在仅 31470 token 估算时即触发 episode 归档、LLM 摘要耗时 18.2s。这是真实的配置地雷。
- ❌ **"压缩删证据 → 门控误判"的因果链在本 run 不成立**，有两处硬反证：
  1. **完成门不读折叠态 `state.tool_results`**。门控 `CompletionVerdict.compute`（`scheduler/plan.py:76-119`）与 `verify_deliverables`（`plan.py:122-150`）的输入是 `_execute_plan` 的**内存局部变量 `results: dict[str, StepResult]`**（`plan.py:656`，逐层由 `dag_executor.execute_layer(..., results)` 就地填充，`plan.py:714-722`）。压缩（`context_manager`）折叠/裁剪的是 `state.tool_results`，与门控用的 `results` 是**两套数据**。因此裁剪折叠态不可能让门控"看不见"已成功步骤。
  2. seq30 `EpisodeArchived.archived_event_refs=[9,10]` 裁掉的是**第一次失败的 browser ToolCompleted**，不是任何成功证据；s1/s2 的成功证据（seq25-28）在 seq30 之后才产生，根本不在被裁集合里。
- ✅ **seq33 判 unmet 是正确判定，不是误判**：s3（browser 机票）从头到尾不成功（seq10、seq23 两次 unsuccessful），s4 因依赖 s3 被 skip。门控报 `s3, s4, declared_op#2:browser` 未达成，与事实一致。"no fake-green"（C-02）在这里**正确拦截了 seq32 LLM 的"task complete"谎报**。
- ⚠️ **但"证据/上下文耦合"作为架构债真实存在（潜在，非本次死因）**：`fold.py:418-434`（EpisodeArchived）与 `fold.py:436-440`（ContextPruned）会把归档/裁剪事件从 `state.tool_results` / `state.thought_history` 中移除。这会影响：
  - **Answer 生成**：`planner` 生成最终答案遍历 `state.tool_results`，压缩后只剩 episode 摘要，细节证据丢失；
  - **Replay Inspector 时间旅行**：`replay/projection.py` 基于 `fold_events` 切片，归档时点之后的投影里证据消失 → 直接伤害 PRD AC-5（可观测/可重放）。
  - 结论：R1 应重述为"**受信证据与喂 LLM 的工作上下文被耦合在同一份折叠态里，压缩为救 LLM 上下文而误伤了证据投影**"，而非"压缩导致门控误判"。

### 1.3 R2（DAG 失败整份重规划、已完成步骤被重跑、事件量翻倍）—— **方向成立，现象描述不准确**

- ✅ 失败处理颗粒度粗：任一层 `not ok` 即走 `_revise_with_degenerate_guard` → 整份 `PlanRevised`（`plan.py:751-799`）；无"工具 retry 用尽 → 单步局部修复 → 失败子 DAG 重规划"的分级。
- ✅ `_gate_skip_reason`（`dag_executor.py:413`）任一依赖非 normal 即 skip 下游——但这是**正确的受信门控**（不向下游传坏数据），不是缺陷。
- ❌ **"已完成步骤被重跑 / 事件量翻倍"在本 run 不准确**：
  - seq15 `PlanRevised(steps=2)` 是 LLM 给的**补丁**（s1/s2 换 http），`_merge_revised_plan`（`plan.py` merge 逻辑）把 root 中未完成的 s3/s4 **合并回**，seq16 才是 4 步。
  - 换成 http 的 s1/s2 **首次执行即 seq17-28**，并未重跑（`StepResult.should_not_rerun` / `completed_ids` 跳过已完成，`plan.py:677`）。
  - 被重复执行的是 **s3（browser）**：seq22 的 ToolCalled 与 seq8 **同一幂等键** `5e3a7bdd…`，是对"仍留在计划里的失败 browser 步骤"的重放，且再次失败——这是"**LLM 补丁没覆盖失败步骤 + merge 把坏步骤原样加回**"，不是"已完成步骤被重跑"。
- 真正缺陷：① browser 的失败是**语义 unsuccessful（`success:false`）而非异常**，`RetryRunner` 只重试异常/超时，不重试语义失败 → 直接升级到全局 revise；② merge 不区分"失败步骤是否被补丁修复"，未被修复的坏步骤原样回到新计划；③ 无步骤级 bounded 局部修复。

### 1.4 R3（计划-执行强耦合，无局部 think-act）—— **成立**

- 全 run 仅 seq3 一条 `AgentThought`（规划时）。工具失败后没有"针对该步骤的局部 think-act"，只能：skip 下游 → 整份 revise（seq15、seq32，实测 LLM revise 28-34s）→ 重新 PlanCreated。弱模型在全局 revise 下表现不稳定：seq15 只修了一半（漏 s3/s4），seq32 干脆谎报完成。

### 1.5 PRD 未覆盖的新发现（更严重）

- 🔴 **N1：checkpoint/resume 名存实亡（易失执行态的根）**。`base.py:901-904` `_ensure_run_started` 调 `find_resume_seq(events)` 找到 checkpoint 后**只打一条日志**，不从事件流重建任何执行态。`_execute_plan` 的 `results: dict[str, StepResult]`（`plan.py:656`）是**纯内存**，进程崩溃/重启即全部丢失；resume（`base.py:848-884`）只处理 PAUSED→RUNNING 的事件置位与 event 唤醒，**不重建 DAG 执行进度**。这才是"易失执行态 vs 持久证据"最该根治的点，也是 durable execution 调研要解决的核心。
- 🟡 **N2："大输出撑爆 LLM 上下文"被夸大**。LLM 侧已有多处截断：revise 反馈 `truncate_output`（~200 字符摘要）、answer 上下文 5000 字符、episode 活动行 2000 字符（`context_manager.py:434`）。全量 output（seq26 open-meteo 67796 字符）撑大的是 **① Event Store 落库体积 ② fold 内存 ③ token 估算（`context_manager.py:70-72` 对 `str(tr.output)` 全量计数，从而误触发紧急压缩）**，并非直接进入 prompt。F-2 仍有价值，但收口点主要在 **Tool Layer 落库/折叠处**，而非 LLM 调用前。
- 🟡 **N3：输出上限不统一**。`http_request` 有 `max_response_bytes=65536` 截断（`tools/http_request.py:21,211`），但 `headers` 全量落库；MCP 工具 `tools/mcp_call.py:122-135` 返回 `{"success":true,"content":content_parts}` **无任何大小上限**。

### 1.6 复核结论汇总

| PRD 根因 | 复核结论 | 修正后定性 |
|---|---|---|
| R1 压缩丢证据→门控误判 | **驳回因果链**；配置 bug 与"证据/上下文耦合"架构债属实 | 门控读内存 `results` 不读折叠态；seq33 判定正确。真问题是证据投影被压缩误伤（伤 answer/replay） |
| R2 整份重规划/重跑/翻倍 | **方向成立，现象不准** | 无分级恢复；坏步骤被 merge 原样加回并重放；语义失败不被 retry |
| R3 无局部 think-act | 成立 | 弱模型全局 revise 下漏修/谎报 |
| （新）N1 | **PRD 遗漏，最高优先** | resume 不重建执行态，崩溃即丢进度 |
| （新）N2/N3 | 补充 | 大输出伤存储/折叠/token 估算；MCP 无上限 |

**本 run 真凶链**：browser 在 Windows SelectorEventLoop 下基础设施不可用（环境故障，注：最新提交 `a40e057` 已含 ProactorEventLoop 修复，需回归确认触发面是否消除）→ s1/s3 语义 unsuccessful → s2/s4 被正确 skip → 全局 revise 产出**只修一半**的补丁（天气换 http、机票仍 browser）→ merge 把坏的 s3/s4 原样加回 → s3 重放再败 → 门控正确判 unmet（拦截了 LLM 的"task complete"谎报）。叠加 `token_limit=3000` 配置地雷与证据/上下文耦合，放大了观测与恢复成本。

---

## 2. 外部范式调研（受信边界下的适用性）

| 范式 | 核心机制 | 对 Harness 的借鉴 | 代价 / 不采纳点 |
|---|---|---|---|
| **Temporal / durable execution** | Workflow 代码确定性重放；事件历史（event history）是唯一事实；activity 是副作用单元，靠幂等键重放；崩溃后从历史重建 workflow 栈 | **借鉴思想**：执行进度必须能从事件历史**确定性重建**（直接对应 N1）；副作用只在 activity/Tool Layer；重放时跳过已完成 activity。不引入 Temporal 本身 | Temporal 自有 history 事件模型 + 专有 worker runtime，与本仓库 `fold_events`/ScopedEventStore/多租户双轨；强制权移出受信组件，违反约束 4 |
| **LangGraph persistence** | 图节点 + checkpointer（线程状态快照），断点续跑、human-in-the-loop interrupt | **借鉴**：checkpoint 应承载**可恢复的状态快照**而非仅一个 seq 号（对应 N1：当前 `ContextCheckpointed` 只记 `checkpoint_seq`） | LangGraph 的 checkpoint 是黑盒序列化状态，非 append-only 事件；破坏时间旅行"任意时刻由本仓库事件重建" |
| **Airflow trigger rules** | 任务实例状态机；`all_success`/`all_done`/`one_failed` 等触发规则决定下游是否跑/跳 | **借鉴**：把"skip 下游"从单一 `dep not normal` 细化为可表达的触发规则，并支持**失败子 DAG** 局部重跑（对应 R2） | Airflow 是静态 DAG + 调度器，与"Agent 动态决策 DAG"范式不同；不引入 |
| **MemGPT / Letta（记忆层级）** | 主存（LLM 上下文）/ 外部存储分层；LLM 通过显式 `core_memory_append`/`archival_memory_insert`/分页调用在层级间移动数据 | **借鉴**：大输出进"外部存储（blob 引用）"，LLM 需要时用**受信只读工具** `fetch_output(ref)` 显式 page-in（对应 F-2）；上下文压缩=page-out，证据投影永不 page-out | MemGPT 让 LLM 自主管理记忆；Harness 中**分页触发与引用完整性必须受信**，LLM 只决定"要不要取回" |
| **ReWOO** | Planner 一次出计划（带占位符）→ Worker 解耦执行 → Solver 汇总；规划与执行分离省 token | **佐证**：plan-once + 局部执行的方向正确；失败应局部补规划而非全局重规划 | ReWOO 无失败恢复/重规划机制 |
| **Reflexion** | 执行后用自然语言"自我反思"写入记忆，下一轮据此改进 | **借鉴（非受信）**：F-6 局部修复时，可把 LLM 的反思文本作为**建议**注入，但**不得参与受信判定** | 反思是 LLM 输出，受信边界禁止其决定门控/skip/恢复边界 |
| **LLM+P** | 用 PDDL 经典规划器做计划，LLM 只做自然语言↔形式化转换 | 不采纳：需引入 PDDL 建模与规划器，过重；且 DAG 由 Agent 动态生成 | 成本高，与 MVE 节奏不符 |
| **HuggingGPT** | LLM 控制器 + 专家模型调度，任务规划/模型选择/执行/响应生成四阶段 | 部分借鉴"控制器决策、专家执行"的分层，但不引入专用模型编排 | 面向多模型调度，与本问题（执行韧性）相关性弱 |

**调研结论**：采用"**durable execution 的重建思想 + MemGPT 的证据分层 + Airflow 的失败分级触发**"，全部以**本仓库事件溯源**为载体实现，不引入 Temporal/LangGraph 等外部持久化框架（理由见 §3）。

---

## 3. 候选方案对比与推荐

### 方案 A（推荐）：事件溯源内修 —— 受信执行态证据投影 + 输出引用收口 + 分级恢复 + 可重建执行态

1. **受信执行态证据投影（F-1）**：在 `fold_events` 中新增由 `DAG_STEP_STARTED/COMPLETED/FAILED/SKIPPED` + `TOOL_COMPLETED/FAILED/TIMEOUT` + `PLAN_*` 折叠出的**受信执行态投影**（每步骤的 `exec_state`、产出引用、失败原因、所属 plan）。该投影**独立于**喂 LLM 的 `thought_history`/`tool_results` 工作视图；压缩（EpisodeArchived/ContextPruned）只裁剪 LLM 工作视图，**永不裁剪证据投影**。完成门 / Replay / resume 统一读证据投影。
2. **大输出引用收口（F-2）**：Tool Layer 对超阈值 output 落"摘要 + blob 引用"（content-addressed，存 workspace 作用域），事件 payload 只存 `{summary, ref, sha256, bytes, truncated}`；新增**受信只读工具** `fetch_output(ref)` 供 Agent 按需取回（过 guardrails + 幂等）。MCP/http 统一上限。
3. **token_limit 配置外置（F-3）**：移除 `serve.py:218` 硬编码，改 env `HARNESS_CONTEXT_TOKEN_LIMIT`，默认按模型窗口×安全系数。
4. **可重建执行态 / resume（F-4，N1）**：让 `_execute_plan` 的 `results` 能在（重）启动时由**证据投影**确定性重建；checkpoint 事件承载可恢复锚点；resume 后跳过已完成步骤、对未完成步骤继续，不重放副作用（幂等键兜底）。
5. **恢复分级（F-5，R2）**：工具级 retry（含**可重试的语义失败**白名单）→ 步骤级 bounded 局部修复 → 失败**子 DAG** 补丁 replan → 全局 revise 兜底。merge 时区分"被补丁修复的失败步骤"与"未覆盖的失败步骤"，后者不得静默原样回到计划。
6. **bounded 局部 think-act（F-6，R3）**：步骤失败且工具 retry 用尽后，在受信预算（最大轮次、工具白名单、禁止新增 mutating 步骤、禁止改交付契约）内允许 LLM 对**该步骤**做局部 think-act；所有动作过 Tool Layer（guardrails/幂等/确认），决策落事件、可重放。

### 方案 B：引入外部 durable 框架（Temporal / LangGraph）替换自研调度循环

用 Temporal workflow history/replay 或 LangGraph checkpointer 承载执行态与恢复。

| 维度 | 方案 A（推荐） | 方案 B |
|---|---|---|
| 事件溯源 / append-only | ✅ 完全沿用，`fold_events` 唯一事实源 | ❌ 框架自有 history/checkpoint，双轨 |
| Replay Inspector 时间旅行 | ✅ 任意时刻由本仓库事件重建 | ❌ 黑盒序列化状态，破坏 R-X3/R-X4 |
| 受信边界（约束 4） | ✅ 门控/证据/压缩/skip/恢复边界全在本仓库受信机械逻辑 | ⚠️ 强制权移入框架，需大量适配且难审计 |
| 多租户 / 工作区 / 沙盒 / 确认流程 | ✅ 现有 ScopedEventStore/确认机制不变 | ❌ 需重新对接 |
| 改造成本 / 风险 | 中，分步可 TDD，无新重依赖（符合 N-3） | 高，重写 L3 调度核心 |
| 与架构哲学（AGENTS.md §2.1/§8） | ✅ "状态由事件折叠、不写 Workflow Engine" | ❌ 显式冲突（明令不推荐 Workflow Engine/可变状态表） |

**推荐：方案 A。放弃 B 的理由**：B 与"Agent 决策 + 受信组件强制、状态唯一由 `fold_events` 折叠、不写 Workflow Engine"的核心范式直接冲突，且外部持久化是黑盒历史，会摧毁时间旅行调试器"任意时刻可由本仓库事件重建"的根本能力。

---

## 4. 受信 / 非受信边界分析

**总原则（AGENTS.md §2.2）**：决策权归 Agent，强制权归受信组件；受信判定**绝不引入 LLM**。

| 能力 | 受信（机械、确定性、可重放） | 非受信（LLM 建议，可被否决） |
|---|---|---|
| 步骤执行状态 `exec_state` | 由 DAG_STEP_* / TOOL_* 事件折叠（证据投影） | — |
| 完成门 / 交付门 | `CompletionVerdict`/`verify_deliverables` 读证据投影，机械判定 | LLM 可报 `declared_operations`/`step_tasks`，但**仅审计**，不决定达成 |
| 压缩触发与裁剪范围 | token 估算 + 阈值机械触发；**证据投影永不裁剪**，只裁 LLM 工作视图 | episode 摘要文本由 LLM 生成（非受信，仅供 LLM 阅读） |
| 大输出 blob 化 | 阈值判定、sha256/ref 生成、payload 收口在 Tool Layer | LLM 决定是否 `fetch_output` 取回 |
| `fetch_output` 读取 | 工具契约（只读、workspace 作用域、guardrails、幂等）受信 | — |
| 下游 skip | `_gate_skip_reason` 机械触发规则 | — |
| 恢复分级 | 层级升级、预算计数、补丁是否覆盖失败步骤、merge 合法性 = 机械 | LLM 提议"局部修复动作/补丁步骤"，受信预算校验后才可执行 |
| 局部 think-act（F-6） | 轮次上限、工具白名单、禁新增 mutating 步骤、禁改契约、动作过 Tool Layer | LLM 产出局部 thought / 工具选择 / 反思文本 |
| resume 重建 | 由证据投影确定性重建 `results`，幂等键防重 | — |

**红线**：`TaskState`/`step_tasks`/episode 摘要/反思文本等 LLM 输出**严禁**进入门控、skip、恢复边界的判定（沿用 `dag_types.py` 既有约束）。

---

## 5. PRD §6 开放问题结论

1. **持久证据落点**：用**事件流折叠出的受信证据投影**（不新增可变 store、不扩展易失 RunState 语义），完整 output 走 blob 引用。满足 R-X3。
2. **大输出引用形态**：超阈值 output 体写 workspace 作用域 content-addressed blob（本地走 FileOp backend 沙盒目录；远端/沙盒载体用引用句柄）。事件存 `{summary, ref, sha256, bytes, truncated}`；受信只读工具 `fetch_output(ref)` 按引用取回；生命周期绑定 run/workspace。
3. **token_limit**：移除硬编码，`HARNESS_CONTEXT_TOKEN_LIMIT` env，默认按模型窗口（128000）×0.7 安全系数；压缩/紧急阈值据此自洽。
4. **步骤级局部修复决策**：LLM **建议**，受信 Scheduler 用机械预算（次数、工具白名单、不得新增 mutating 步骤、不得改交付契约）强制；全程落事件可重放。
5. **压缩是否按 run 形态区分**：是。DAG/plan run 的**证据投影永不裁**；压缩只作用 LLM 工作视图。对话型 run 维持现有 episode 机制。
6. **plan-once vs 局部 ReAct**：默认 plan-execute（省 token）；仅在步骤失败且工具 retry 用尽后开启 **bounded 局部 think-act（F-6）**；不按任务类型动态切换调度器（避免不可重放的隐式分支）。

---

## 6. 对事件溯源 / fold_events / Replay Inspector 的兼容性

- **append-only 不变**：所有新增能力通过**新增事件类型 / 新增 payload 字段**实现，不修改、不删除既有事件；旧事件流折叠行为保持向后兼容（新字段均有默认值）。
- **`fold_events` 纯函数 seam 不变**：新增证据投影折叠仍为**无 I/O 纯函数**；blob 读取**不在 fold 内发生**（投影只存 ref，取回走 `fetch_output` 工具/查询服务）。
- **Replay Inspector**：`replay/projection.py::reconstruct_state` 切片后调 `fold_events` 自动获得证据投影 → 时间旅行视图中**证据不再因压缩消失**（修复 AC-5）。投影对象需在 replay API 序列化中暴露（新增只读字段，前端 OpenAPI 自动生成）。
- **多租户**：blob 与事件都受 workspace/tenant 作用域约束；`fetch_output` 经 ScopedEventStore/工作区校验，禁止跨 run/跨租户引用。

### 6.1 拟新增事件类型（名称待架构导师定稿）

| 事件 | 用途 | 受信 | 关键字段（草案） |
|---|---|---|---|
| `OutputBlobStored` | 大输出落 blob | 是（Tool Layer 写） | `tool_call_id, step_id?, ref, sha256, bytes, summary, truncated, workspace_id` |
| `StepEvidenceAttached` | （可选）步骤产出与 blob 引用绑定 | 是 | `plan_id, step_id, exec_state, output_ref, output_summary, error` |
| `StepRetryScheduled` | 工具/步骤级 retry（含可重试语义失败） | 是 | `step_id, attempt, reason, retry_kind` |
| `StepLocalRepairStarted` / `StepLocalRepairCompleted` | F-6 局部 think-act 边界 | 是（边界）/LLM 内容非受信 | `step_id, repair_round, budget_remaining, tool_whitelist` |
| `SubplanRevised` | 失败子 DAG 补丁 replan（区别于全局 PlanRevised） | 是 | `plan_id, failed_step_ids, patched_steps, dropped_step_ids, merge_decision` |
| `ExecutionStateRebuilt` | resume 时从证据投影重建执行态 | 是 | `from_checkpoint_seq, rebuilt_step_ids, skipped_completed_ids` |

> 现有 `DagStepCompleted` 等已含 `output_summary/error/tool_call_id`，证据投影主要靠折叠现有事件得到；`OutputBlobStored` 是 F-2 的必需新事件。最终事件清单与 payload schema 在实现前与架构导师对齐，并同步前端枚举（AGENTS.md §6.3）。

---

## 7. 具体改动点（实现期，按层）

- **L1 Event Store / 模型**：`models/events.py` 新增上述事件 + payload（Pydantic v2）；blob 存储抽象（workspace 作用域，content-addressed）。
- **L2 Tool Layer**：输出收口装饰器（阈值→blob→`OutputBlobStored`），统一 http/MCP 截断；新增 `fetch_output` 只读工具契约（guardrail：仅本 run/workspace ref、只读、幂等）；RetryRunner 支持"可重试语义失败"策略（受信白名单，如基础设施类 `success:false`）。
- **fold（受信投影）**：新增 `step_evidence: dict[step_id, StepEvidence]` 折叠（由 PLAN_CREATED/REVISED + DAG_STEP_* + TOOL_* 构建）；EpisodeArchived/ContextPruned 分支**不再裁剪证据投影**，仅裁剪 `thought_history/tool_results` 工作视图。
- **L3 Scheduler**：`_execute_plan` 的 `results` 支持从证据投影重建（F-4）；恢复分级状态机（F-5）；merge 合法性校验（未覆盖的失败步骤不得静默回计划）；F-6 局部 think-act 循环 + 受信预算。
- **配置**：`serve.py` token_limit env 化（F-3）。
- **L6/L7**：replay/查询 API 暴露证据投影与 blob 引用；前端类型自动生成。

---

## 8. TDD + BDD 测试计划

**铁律**：实现期严格 TDD —— 先写**复现 run e05087b6 螺旋的失败测试**（红），再实现（绿），再重构。受信组件按 AGENTS.md §5.2 要求 100% 分支覆盖 + 故障注入 + 并发。

### 8.1 回归测试（复现 e05087b6，最高优先）

- **T-REG-1（证据不被压缩误伤，AC-1/AC-5）**
  - Given 一个 plan run，s1/s2 工具成功且产出大 output，随后触发 EpisodeArchived 归档了早期工具事件；
  - When 折叠归档后的事件流；
  - Then 受信证据投影中 s1/s2 仍为 `completed` 且 output_ref 可解析；Replay 投影在归档时点后仍能看到 s1/s2 证据；完成门读证据投影判定 s1/s2 达成。
  - （红：当前 `state.tool_results` 被裁、replay 证据消失。）
- **T-REG-2（失败步骤不被静默 merge 回计划，R2）**
  - Given s3 browser 语义失败、LLM 补丁只修 s1/s2（未覆盖 s3）；
  - When merge 修订计划；
  - Then 系统**不得**把未修复的 s3 以原 browser 动作静默加回并重放；要么标记为需局部修复/子 DAG replan，要么显式失败并说明；断言不出现 seq22 那种"同幂等键重放已知坏动作"。
- **T-REG-3（语义失败触发分级恢复而非直接全局 revise，R2/F-5）**
  - Given 工具返回基础设施类 `success:false`（可重试语义失败）；
  - When 步骤执行；
  - Then 先走工具级 retry（受信预算内），retry 用尽才升级；断言不立刻产生全局 PlanRevised。
- **T-REG-4（resume 重建执行态，N1/AC-4）**
  - Given s1/s2 已完成、s3 运行中进程崩溃（已有完整事件流含 checkpoint）；
  - When 重新启动并 resume；
  - Then `results` 从证据投影重建，s1/s2 不重放（幂等键不产生重复副作用），s3/s4 继续执行；断言重建后拓扑与终态与"未崩溃"一致。
- **T-REG-5（大输出不污染 token 估算/存储，N2/F-2）**
  - Given 工具返回 200KB output；
  - When 落库与折叠；
  - Then 事件 payload 中 output 体被替换为 `{summary,ref,sha256,bytes,truncated}`，blob 可经 `fetch_output` 取回；token 估算基于摘要而非全量，不因此误触发紧急压缩。
- **T-REG-6（配置地雷，F-3）**
  - Given 未设置 env；When 装配 ContextManager；Then token_limit=默认模型窗口×0.7（非 3000）；设置 env 时按 env。

### 8.2 受信组件单元测试（纯函数，无 I/O）

- fold 证据投影：PLAN_CREATED/REVISED/DAG_STEP_*/TOOL_* 各分支；压缩后证据投影不变；旧事件流（无新事件）折叠兼容。
- 完成门 / 交付门读证据投影：met/unmet/unverified；LLM `step_tasks=achieved` 但证据失败 → 仍 unmet（守住 seq32 谎报）。
- 恢复分级状态机：retry→局部修复→子 DAG replan→全局 revise 的升级与预算耗尽；merge 合法性校验各分支。
- F-6 受信预算：超轮次/越权工具/新增 mutating 步骤/改契约 → 一律机械拒绝。
- 幂等键：blob 写入、fetch_output、retry 的碰撞/去重。

### 8.3 故障注入 / 并发（AGENTS.md §5.2）

- Event Store 写入冲突 / 重复 append（LateEventRejected）；blob 写入失败 → 结构化错误事件，不泄漏异常到非受信层。
- Tool Layer 超时 / 沙盒崩溃 / MCP 返回超大 content。
- 多 worker 并发写同一 run：seq 唯一、证据投影一致、幂等键一致。
- resume 与确认流程并发：PAUSED+confirmation 下重建不丢确认态。

### 8.4 BDD（Given-When-Then，验收标准映射）

| AC | BDD 要点 |
|---|---|
| AC-1 证据持久 | Given 大输出+紧急压缩；When run 继续/重放；Then 完成证据与门控结论不被压缩改变 |
| AC-2 失败不扩散 | Given 单步失败；When 恢复；Then 仅失败子 DAG 重试，已完成步骤与事件不重复 |
| AC-3 局部 think-act | Given 步骤失败且 retry 用尽；When 受信预算内局部修复；Then 仅该步骤被 bounded 处理，超预算即升级全局 |
| AC-4 断点续传 | Given 崩溃；When resume；Then 从事件重建进度，副作用不重复，终态一致 |
| AC-5 时间旅行 | Given 任意历史 seq；When replay；Then 证据/状态可由事件流完整重建（含被压缩步骤的 output_ref） |
| AC-6 成本可控 | Given 弱模型；When 失败恢复；Then LLM 调用轮次/token 受预算上限约束，无全局 revise 螺旋 |

### 8.5 测试落地位置（实际实现映射；与建议文件名的差异见 §11）

已落地：

- `tests/test_step_evidence_projection.py` — fold 证据投影 + 压缩不裁剪（T-REG-1）
- `tests/test_recovery_core.py` — F-4/F-5/F-6 受信恢复纯函数（rebuild / unresolved / classify / 预算，合并承载原 `test_recovery_tiers.py`、`test_local_repair_budget.py` 意图）
- `tests/test_local_repair_wiring.py` — F-6 候选过滤 + 局部修复接线 + e05087b6 模式回归
- `tests/test_output_blob.py` — F-2 收口 + fetch_output + MCP 截断（T-REG-5）
- `tests/test_context_config.py` — F-3 env（T-REG-6）
- `tests/test_tool_semantic_retry.py` — F-5 工具级语义失败重试（T-REG-3，受信白名单 + 只读门控）
- `tests/test_replay_projection.py` / `tests/test_replay_api.py` — L6/L7：Replay 证据投影暴露 + 压缩存活（AC-5）
- 回归：e05087b6 螺旋相关断言落在 `test_local_repair_wiring.py` / `test_recovery_core.py` / `test_output_blob.py` / `test_step_evidence_projection.py` 的注释与场景；`test_dag_self_heal.py` 本期**未扩展**（建议项，未做）。

缺口（未建文件 / 未覆盖，见 §11 项 3）：

- `tests/test_execution_state_rebuild.py`（T-REG-4 resume 崩溃重建 **端到端**）— 仅纯函数级覆盖
- T-REG-2 的 **完整 scheduler 端到端螺旋**、§8.3 故障注入 / 并发用例

---

## 9. 实施顺序（严格分层，AGENTS.md §3.1）

1. **L1**：事件/payload 模型 + blob 存储抽象（先写 T-REG-5 模型层测试）。
2. **fold 证据投影**（受信纯函数，T-REG-1 + 8.2）—— 后续所有层的地基。
3. **L2**：输出收口 + `fetch_output` + 统一截断 + 语义失败 retry 策略。
4. **F-3** 配置外置（低风险先行）。
5. **L3**：resume 重建（F-4，T-REG-4）→ 恢复分级（F-5，T-REG-2/3）→ 局部 think-act（F-6）。
6. **L6/L7**：replay/API 暴露证据投影与 blob 引用，前端类型同步。
7. 全量 `pytest` + `ruff` + `mypy` 回归；故障注入/并发测试通过。

> 每一步先红后绿；受信组件异常一律转结构化错误事件，不泄漏到非受信层。

---

## 10. 待评审决策点

1. 新增事件类型命名与 payload schema（§6.1）需架构导师定稿并同步前端。
2. blob 存储载体：本地 FileOp 沙盒目录 vs 独立 content store 接口（远端/沙盒执行载体时的抽象）——建议先定接口、本地实现。
3. "可重试语义失败"白名单范围（哪些 `success:false` 视为基础设施可重试）需保守界定，避免对业务性不成功盲目重试。
4. F-6 默认开关：建议本期实现但**默认关闭**（config 开启），仅在显式启用的 run 生效，降低对现有 plan-execute 行为的回归面。

---

## 11. 实现状态归档（2026-09-05）

> 本节记录"实现 vs 本文草案"的**已确认差异**（产品确认可接受），供下一位开发者避免误判为遗漏。

### 11.1 已实现（TDD 全绿）

| 项 | 代码落点 | 测试 |
|---|---|---|
| F-1 受信证据投影 | `harness/core/fold.py`（`StepEvidence` / `RunState.step_evidence`，压缩只裁工作视图） | `test_step_evidence_projection.py` |
| F-2 blob 收口 | `output_store.py`、`fetch_output.py`、`executor._offload_if_large`、`OUTPUT_BLOB_STORED`、`serve.py` 注册 `FetchOutputTool` | `test_output_blob.py` |
| F-3 token env 外置 | `context_manager.resolve_context_token_limit()` + `serve.py` | `test_context_config.py` |
| F-4 resume 重建 | `recovery.rebuild_results_from_evidence()` + `scheduler/plan.py` `_execute_plan` 入口重建（含换工具则重跑闸） | `test_recovery_core.py`（纯函数） |
| F-5 分级/语义重试 | `classify_failure_tier` 接入 `tools/executor.py` Step7（语义失败与异常共享 retry 预算）；merge 后 `unresolved_known_bad_steps` 注入高优 `FeedbackInjected` | `test_tool_semantic_retry.py`、`test_recovery_core.py` |
| F-6 局部修复接线 | `scheduler/plan.py` `_local_repair_candidates` / `_attempt_step_local_repair`；`SchedulerConfig.local_repair_*` 默认关闭 | `test_local_repair_wiring.py`、`test_recovery_core.py` |
| L6/L7 Replay 证据 | `replay/schemas.py`（`StepEvidenceView` + `RunStateView.step_evidence`）、`replay/projection.py`；前端 TS/OpenAPI 自动再生成 | `test_replay_projection.py`、`test_replay_api.py` |

### 11.2 §6.1 事件清单差异（草案 6 项，实现 3 项）

- ✅ `OutputBlobStored`、`StepLocalRepairStarted`、`StepLocalRepairCompleted`
- ❌ `SubplanRevised` — **未实现**。merge 后未覆盖坏步骤的升级维持既有高优 `FeedbackInjected`（`error_type=unresolved_known_bad_steps`）驱动下轮修复，不做子 DAG 补丁 replan。
- ❌ `ExecutionStateRebuilt` — **未实现**。resume 时从证据投影重建 `results`，但**不落该事件**（重建属确定性纯计算，无审计事件）。
- ❌ `StepRetryScheduled` — **未实现**。语义/异常重试完全内聚 Tool Layer 且受 `retry_policy` 预算约束，无受信组件消费该事件；改为静默对齐异常重试（`retry_attempts` / trace 承载可观测性），避免无效事件链 + 前端同步负担。
- ❌ `StepEvidenceAttached` — 未做（原标注可选；证据由既有 `DAG_STEP_*/TOOL_*` 事件折叠获得）。

### 11.3 恢复分级链与测试缺口（可接受）

1. 分级链落地为「工具级 retry（异常 + 受信白名单内的瞬态只读语义失败）→ F-6 步骤级局部修复（预算内）→ 全局 revise 兜底」；**"失败子 DAG 补丁 replan"层未实现**（见 11.2 SubplanRevised）。
2. F-5 语义重试白名单按 §10 决策点 3 **保守界定**：仅 `classify_failure_tier == "tool_retry"`（瞬态/5xx 文案）**且** `is_read_only_action`（受信只读操作，与 F-6 共用白名单，未知工具 fail-closed）才自动重试；业务性不成功与副作用操作一律不自动重试，维持直接升级。§6.1 中 `StepRetryScheduled` 因此无需落地。
3. §8.5 建议的若干测试文件名未逐一照建（覆盖并入 `test_recovery_core.py` / `test_local_repair_wiring.py`）；仍缺：**T-REG-4 resume 崩溃重建端到端**、**T-REG-2 完整 scheduler 端到端螺旋**、**§8.3 故障注入 / 并发用例**。均为后续增量，不影响本期已完成范围。

### 11.4 文档其余待办

- §10 决策点 1（新事件命名/白名单定稿）已随本节归档收敛；决策点 2（blob 载体：远端/沙盒抽象）本期仅本地实现，接口预留。
- mypy 未装入 venv，静态门禁以 `ruff` 为准（`pytest` 全量 + `ruff` 双绿）。
