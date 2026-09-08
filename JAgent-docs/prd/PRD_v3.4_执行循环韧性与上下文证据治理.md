# PRD v3.4: 执行循环韧性与上下文证据治理（Execution Resilience & Evidence Durability）

> **版本**: v3.4
> **状态**: 待技术方案评审（**评审通过前禁止动工**，见 §0 门禁）
> **日期**: 2026-09-05
> **产品经理**: （待定）
> **触发来源**: 真实 run `e05087b6`（4 交付物任务，33 个事件后 RunFailed）的根因复盘
> **适用组件**: `harness/core/context_manager.py`, `harness/core/fold.py`, `harness/core/dag_executor.py`, `harness/core/scheduler/plan.py`, `harness/core/planner.py`, `harness/tools/executor.py`, `harness/api/serve.py`（及对应的只读调试视图）
> **前置依赖**: Harness v3.3（Workspace 多租户）+ S1（交付契约完成门）+ 时间旅行调试器（Replay Inspector）
> **决策记录**: 待技术开发文档（`Dev/DESIGN_v3.4_*`）+ 必要时 `Prd/ADR-011_*`

---

## 0. 流程门禁（务必先读）

本 PRD **只定义问题、约束与验收标准，不规定实现方案**。任何代码改动必须满足：

1. **先产出技术开发文档**：由设计方基于本 PRD 产出 `JAgent-docs/Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md`（重大架构取舍另立 `ADR-011`）。
2. **技术文档必须包含**：
   - 对本 PRD 三条根因的独立复核（可**驳回/修正**本 PRD 的判断，但必须给出代码/数据证据）；
   - **竞品/论文/工程范式调研**：至少调研 durable execution（Temporal/LangGraph）、记忆层级（MemGPT/Letta）、工作流恢复分级（Airflow task retry / trigger rules）、规划-执行解耦与最小重规划（ReWOO、LLM+P、HuggingGPT、Reflexion）等，说明各方案在**本仓库受信边界约束下**的适用性与优劣；
   - 候选方案对比（≥2 个），明确推荐方向与**放弃的方案及原因**；
   - 受信/非受信边界影响分析（哪些改动绝不能把 LLM 引入受信判定）；
   - TDD+BDD 测试计划（Given-When-Then），含故障注入与回归用例；
   - 对事件溯源 / 时间旅行调试器（`fold_events`、Replay Inspector）的兼容性影响。
3. **评审顺序**：设计方提交文档 → **产品/用户确认方向** → **架构导师（另一个 AI）交叉审查** → 通过后方可进入实现。
4. 实现阶段严格 **TDD + BDD**：先写失败测试（复现 `e05087b6` 类场景），再改代码，测试转绿且不回归。

> 设计方**有权驳回**本 PRD 的任何修改方向，但必须：先 review/复核 → 调研更优方法 → 告知各方案优劣 → 再给结论。不接受"不调研直接照做"或"不调研直接否决"。

---

## 1. 执行摘要

### 1.1 当前问题（一句话）

一个本应"并行查 4 个交付物"的任务，没有在合理步数内收敛，而是陷入 **"工具大输出撑爆上下文 → 紧急压缩丢失完成证据 → 完成门误判 → 整层跳过 + 整份重规划 → 再撑爆"** 的失控螺旋，最终 `RunFailed`，并产生了两倍于必要的事件量。

### 1.2 可复现证据（真实 run）

run_id = `e05087b6`（`data/logs/harness.log`，真 LLM `qwen3.7-flash`）：

| 信号 | 数据 |
|---|---|
| 任务 | 多交付物（天气 / 航班 / 夜宵 / 行程），Planner 排出 4 步 2 层 DAG |
| 上下文 | `token_estimate=31470 token_limit=3000 ratio=1049%` → `strategy=emergency_compact` |
| 压缩 | `EpisodeArchived`（episode 生成耗时 **18.2s**），压缩率 99%，`keep_recent=3` |
| 恢复行为 | seq13/14/31 三次 `DagStepSkipped`；seq15 `PlanRevised` 后 seq16 **全新 `PlanCreated`**（整份重规划） |
| 结局 | seq32 `PlanRevised` → seq33 `RunFailed` |
| 事件量 | 33 条（同类理想并行任务约 12–16 条） |

### 1.3 产品目标

让 Agent 在"工具返回大输出、单步失败、多交付物"这类常见场景下：

1. **不再因上下文压缩丢失完成判定证据**而误判失败 / 反复重跑；
2. **单步失败就地恢复**，不再"一步失败 → 全层跳过 → 整份重规划"；
3. 事件量、LLM 调用次数、墙钟时间显著下降，且行为**确定性、可重放、可被时间旅行调试器解释**。

---

## 2. 根因（已定位到代码，供设计方复核）

### R1：上下文窗口配置错误 + "证据"与"上下文"耦合（最严重）

- **配置 bug**：`harness/api/serve.py:218` 硬编码 `ContextManager(..., token_limit=3000, checkpoint_interval=10)`；而类默认 `token_limit=128_000`（`harness/core/context_manager.py:46`）。MCP（天气/航班）单次 JSON 即可达数万 token，两次并行调用即超限。
- **架构耦合（真正的根）**：压缩是为"给 LLM 省 token"服务的，但 `fold_events` 在 `EPISODE_ARCHIVED` / `CONTEXT_PRUNED` 分支会把 `tool_results`、`thought_history` 从**折叠态**裁掉（`harness/core/fold.py`）。而 S1 完成门 / Reviser 的判定依赖这些 step 输出作为"证据"。
- **后果链**：压缩（基础设施行为，Agent 无感知）删掉了受信完成门赖以判定的证据 → 门控误判 unmet / Planner 看不到真实工具结果（只剩摘要）→ 修订决策质量崩溃 → 重跑 → 再爆 → 再压。

> 这是"易失上下文窗口"与"持久完成证据"两个本应分离的关注点被焊在一起。

### R2：失败恢复粒度过粗 —— 整层跳过 + 整份重规划

- `harness/core/dag_executor.py:413 _gate_skip_reason`：某 step 的任一 `depends_on` 非 normal，下游 step 直接 `DAG_STEP_SKIPPED`。
- `harness/core/scheduler/plan.py:751-806`：一层内只要有 `layer_failures`，即调用 `_revise_with_degenerate_guard`；实测退化为 seq16 的**全新 `PlanCreated`**（而非在原 DAG 上打最小补丁），导致已完成步骤被重跑、事件量翻倍。
- 单步失败的恢复阶梯缺失：没有"工具级 retry 用满 → 步骤级 bounded 局部修复 → 仅失败子 DAG replan"的分级，任何异常都直接升级到全局 Planner。

### R3：计划-执行解耦后，执行层缺少"便宜的局部决策"

- plan-execute 模式下全 run 仅 1 条 `AgentThought`（seq3），模型推理被收敛进计划 JSON。
- 副作用：工具一旦失败，系统没有低成本的局部 think-act 手段（换工具/改参数），只能调用全局 Planner 做 revise（实测单次 28–34s），既贵又容易过度反应。

---

## 3. 需求（What，不是 How）

> 以下是产品要求与约束；**具体技术方案由技术开发文档决定**，设计方可提出更优解。

### 3.1 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| F-1 | **完成证据持久化**：完成门 / 交付判定所依赖的 step 结果、deliverable 证据，必须存放在**不受上下文压缩影响**的受信结构中；压缩只作用于"喂给 LLM 的工作上下文"，不得删除判定证据。 | P0 |
| F-2 | **大输出收口**：工具（尤其 MCP/HTTP/浏览器）返回的大 payload，在 Tool Layer 经 `output_schema` 投影为"摘要 + 可回溯引用"，原始内容不整体进入 LLM 上下文；需要细节时可按引用取回。 | P0 |
| F-3 | **合理且可配置的上下文预算**：移除硬编码 `token_limit=3000`，改为模型感知 / 环境变量配置的合理默认；压缩阈值与预算自洽。 | P0 |
| F-4 | **分级失败恢复**：工具级 retry（用满现有 `retry_policy`）→ 步骤级 bounded 局部修复（限次）→ 仅针对"失败前沿 / 失败子 DAG"的 plan patch；已 normal 的子图不重跑。 | P1 |
| F-5 | **最小重规划**：升级到 Planner 时，revise 契约为"补丁"（add/replace 失败子图，复用 `depends_on` 接回已完成节点）；degenerate guard 必须拦截"整体重排 / 重复失败动作"。 | P1 |
| F-6 | **执行层局部智能（可选/受控）**：允许 step 内 bounded 的局部 think-act（非受信），产出仍过 Tool Layer guardrails / 幂等；超限再升级 Planner。 | P2 |
| F-7 | **可观测/可重放**：以上所有恢复决策（retry / 局部修复 / skip / replan / 压缩）都要有结构化事件或既有事件可表达，时间旅行调试器能解释"为什么这一步被跳过/重跑"。 | P1 |

### 3.2 非功能需求

- N-1：恢复行为必须**确定性、可重放**（相同事件流折叠出相同判定），不得引入不可重放的隐式状态。
- N-2：多租户 / Workspace 边界不受影响；所有读写仍经 `ScopedEventStore`。
- N-3：不引入新的重型三方依赖；新增依赖须在技术文档中论证。
- N-4：改动对历史事件流**向后兼容**（append-only，不 UPDATE/DELETE；旧 run 仍可 fold / replay）。

### 3.3 红线（不可违背）

- **R-X1**：不得把 LLM 引入受信判定。完成门、证据有效性、压缩是否触发、skip/replan 的**强制边界**必须是机械/受信逻辑；LLM 只能"建议"，受信组件"决定"。
- **R-X2**：不得为了"让 Agent 多思考"而绕过 Tool Layer 直接产生副作用（约束 1）。
- **R-X3**：不得用"可变状态表"替代事件溯源；证据持久化也必须以事件/可折叠结构表达，保持 `fold_events` 唯一事实来源。
- **R-X4**：压缩 / 恢复不得破坏时间旅行调试器的"任意时刻状态可重建"。

---

## 4. 验收标准（BDD 场景摘要）

> 完整 Given-When-Then 用例在技术文档测试计划中细化；以下为产品级验收。

- **AC-1（证据不被压缩抹掉）**：Given 一个工具返回超大输出触发 emergency_compact 的 run，When 压缩发生后，Then 完成门仍能读到该 step 的持久证据并正确判定，**不因压缩而误判 unmet**；`fold_events` 重建的证据视图完整。
- **AC-2（大输出不撑爆）**：Given MCP 返回 ≥ 数万 token 的 JSON，When 该结果进入工作上下文，Then LLM 上下文只含摘要+引用，预算占用下降一个数量级，原始内容可按引用取回。
- **AC-3（单步失败不整层重跑）**：Given 2 层 DAG 中某 step 失败，When 触发恢复，Then 已 normal 的兄弟/上游步骤**不重跑**，事件量与 LLM 调用数显著少于"整份重规划"基线。
- **AC-4（重规划是补丁）**：Given 需要 Planner 介入，When 产出修订计划，Then 已完成 step 不产生新的重复 `PlanCreated`/重复执行；degenerate guard 能拦截整体重排。
- **AC-5（可解释）**：Given 一次含 skip/replan/压缩的 run，When 在时间旅行调试器中查看，Then 每个 skip/重跑/压缩决策都能定位到对应事件与原因。
- **AC-6（回归基线）**：现有测试全绿；新增故障注入（大输出、工具失败、依赖失败、压缩触发）用例通过；`e05087b6` 类场景在重放/仿真下端到端不再螺旋失败。

---

## 5. 非目标（本期不做）

- 不做跨 run 的长期记忆 / 向量检索改造（属另一 PRD 范畴）。
- 不改多租户/鉴权模型。
- 不追求"零重规划"——合理的全局重规划在真无解时仍允许，但必须是最后手段且可解释。
- 不在本期实现回滚/分叉（Replay Inspector 的未来方向）。

---

## 6. 开放问题（设计方必须在技术文档中给出结论）

1. "持久证据"落在哪个结构？（新增受信 evidence store / 复用事件流投影 / 扩展 RunState 折叠字段）各自对事件溯源、调试器、存储膨胀的影响？
2. 大输出"引用"如何在 append-only + 沙盒/远端载体下落地？引用的生命周期与 Workspace 边界？
3. `token_limit` 的合理默认如何随模型上下文窗口自适应？env 命名与默认值？
4. 分级恢复中"步骤级局部修复"是否允许 LLM 参与？如何保证它是非受信、bounded、可重放的？
5. 压缩策略是否应区分"对话型 run"与"多工具 DAG run"？证据保护是否应按 run 形态调整？
6. R2/R3 的取舍：plan-once（省 token）vs 执行层局部 ReAct（更鲁棒）在本架构的平衡点在哪？是否按任务类型动态选择调度器？

---

## 7. 交付物清单

- [ ] 技术开发文档 `Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md`（含调研、方案对比、边界分析、TDD/BDD 计划）
- [ ] 必要的 ADR（如证据持久化结构、恢复分级模型）
- [ ] 用户确认 + 架构导师交叉审查记录
- [ ] TDD 实现（先红后绿）+ 故障注入 / 回归测试
- [ ] 文档 / 代码 / 行为三者一致性核对
