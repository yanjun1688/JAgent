# 提示词：让另一个 AI 基于 PRD v3.4 产出技术开发文档（先设计、评审，后开工）

> 用法：把「==== 复制以下提示词 ====”」之间的整段贴给另一个 AI session（建议在本仓库 `D:\Project\JAgent` 下工作）。
> 配套阅读：`JAgent-docs/Prd/PRD_v3.4_执行循环韧性与上下文证据治理.md`、`AGENTS.md`、`JAgent-docs/Dev/ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md`。

====================================================================
## 复制以下提示词 ↓↓↓

你是 Harness(JAgent) 项目的**技术方案设计方**。项目是一个基于事件溯源（Event Sourcing）的
Agent-First 执行引擎：Agent(LLM) 有决策权，受信组件（Event Store / Scheduler / Tool Layer /
完成门）有强制权；系统状态由 append-only 事件流经 `harness/core/fold.py::fold_events` 唯一折叠得到。
协作规范见仓库根目录 `AGENTS.md`（务必先读，尤其是受信边界、分层、TDD 要求）。

### 你的任务（本阶段只做"设计与评审"，禁止写产品代码）

阅读 `JAgent-docs/Prd/PRD_v3.4_执行循环韧性与上下文证据治理.md`，理解其记录的一个真实失败 run
（`e05087b6`）暴露的**工程叠加问题**，然后产出一份**技术开发文档**：
`JAgent-docs/Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md`（重大架构取舍另立 `Prd/ADR-011_*.md`）。

PRD 归纳了三条根因（R1 上下文配置错误+证据/上下文耦合；R2 失败恢复粒度过粗导致整层跳过+整份重规划；
R3 执行层缺少便宜的局部决策）。**你不要直接照单全收。** 你必须：

1. **独立复核根因**：亲自读相关代码（至少：`harness/api/serve.py` 上下文装配、
   `harness/core/context_manager.py`、`harness/core/fold.py` 的压缩/裁剪分支、
   `harness/core/dag_executor.py` 的 `_gate_skip_reason`、`harness/core/scheduler/plan.py` 的
   失败→revise 路径、`harness/core/planner.py`、`harness/tools/executor.py`），
   用 file:line 和数据证实或**反驳/修正** PRD 的判断。PRD 可能有错，你要指出来。
2. **调研更优方法（必须有外部依据，不要空谈）**：结合论文与成熟工程实践，至少覆盖——
   - Durable execution / 持久工作流：Temporal、LangGraph、Airflow（task retry、trigger rules）
   - 记忆层级 / 上下文治理：MemGPT/Letta（Packer et al. 2023）
   - 规划-执行解耦与最小重规划：ReWOO（Xu et al. 2023）、LLM+P、HuggingGPT（Shen et al. 2023）、
     Reflexion（Shinn et al. 2023）、Plan-and-Execute
   说明每个范式在**本仓库受信边界约束下**是否适用、怎么落地、代价是什么。
3. **给 ≥2 个候选方案对比**，列出优劣（对事件溯源、可重放性、受信边界、存储、改造成本、风险），
   明确**推荐方向**以及**放弃了哪些方案、为什么**。你可以驳回 PRD 的修改方向，但必须给出依据。
4. **逐条回答 PRD §6 的开放问题**。
5. **受信/非受信边界分析**：明确哪些组件绝不能引入 LLM（完成门、证据有效性、压缩触发、skip/replan
   的强制边界），哪些地方 LLM 只能"建议"。任何方案都不得违背 PRD §3.3 红线 R-X1~R-X4。
6. **TDD+BDD 测试计划**：把 PRD §4 的 AC-1~AC-6 展开为 Given-When-Then 用例，包含故障注入
   （超大工具输出、工具失败、依赖失败、紧急压缩触发）与回归用例；说明如何在测试里复现 `e05087b6`
   类螺旋失败并断言"不再螺旋失败"。
7. **兼容性**：评估对 `fold_events`、事件类型、时间旅行调试器（`harness/replay/*`）、多租户、
   历史事件流（append-only 向后兼容）的影响；需要新增哪些事件/字段。

### 交付与评审流程（严格遵守）

- 本阶段**只产出文档，不改产品代码**（可以写一次性调研脚本/读数，但不要提交实现）。
- 文档写完后，用一段话向我（用户）汇报：根因复核结论、推荐方案与理由、主要权衡、你驳回了 PRD 的哪些点。
- **等我确认方向**后，我会把你的文档交给另一位"架构导师 AI"做**交叉审查**；审查通过我才会让你进入实现。
- 实现阶段将严格 **TDD + BDD**：先写失败测试复现问题，再改代码转绿，且不回归。

### 输出要求

- 技术文档放在 `JAgent-docs/Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md`，结构清晰、结论明确、
  每个关键决策标注代码位置与外部依据（论文/系统名）。
- 不要堆砌名词；每个推荐都要能回答"在这个代码库里具体改哪里、为什么这样改、怎么验证"。

## 复制以上提示词 ↑↑↑
====================================================================
