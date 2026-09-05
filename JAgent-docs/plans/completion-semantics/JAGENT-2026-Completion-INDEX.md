# JAGENT-2026-Completion-INDEX — 完成语义粒度对齐改造（主控索引）

> **版本**: v1.0
> **日期**: 2026-08-13
> **角色**: 方案统筹（Agent 导师）
> **关联**: [AGENTS.md](../../../AGENTS.md) · [bugs/JAGENT-2026-P1-13_*](../../bugs/JAGENT-2026-P1-13_Blackbox_Real_LLM_Workspace_Completion_and_Environment.md) · [reviews/structured_input_requirements_review_20260812](../../reviews/structured_input_requirements_review_20260812.md)
> **范围声明**: 本方案与 [plans/workspace-v3.3/TODO_v3.3_Workspace.md](../workspace-v3.3/TODO_v3.3_Workspace.md)（旧工作区历史方案）**无关**，是本会话独立的文档体系，不沿用旧约定。

---

## 0. 一句话目标

解决 Agent 执行链路中"用户目标、结构化需求、Planner、Reviser、DAG、Tool Contract、完成判定、生命周期、可观测性之间**粒度不对齐**"的架构问题。

**核心原则**（源自 AGENTS.md）：
- 决策权归 Agent，强制权归系统。
- 所有硬性交付要求必须成为**受信契约**，不依赖弱模型主动声明。
- 不引入 Workflow Engine，不替代 Agent 做业务决策。
- 所有结构化数据用 Pydantic 模型同源定义。

**总验收口径**：满足 `S12` 所列 13 项全局验收标准，才能宣称问题被修复。

---

## 1. 已冻结决策（后续步骤禁止再讨论，只能引用编号）

### 1.1 六项决策（D-01 ~ D-06）

| 编号 | 主题 | 冻结结论 |
|------|------|----------|
| D-01 | 步骤输出引用 `$step.output` | **支持，但受信化**：语法层保留 `$s1.result`（LLM 友好）；规范层由受信 PlanGuardrail 静态解析为 `OutputRef` 并校验；执行层由受信 Executor 只认结构化绑定解析。目标 step 必须存在于当前 plan、字段必须符合上游输出 schema、`file_op.path` 与 `file_op.content` 禁止引用。 |
| D-02 | 交付契约来源 | **方案 A + B 并存**：`POST /api/v1/runs` 增加可选 `required_operations`（来源 `caller`）；未提供时由独立抽取步骤从 intent 抽取（来源 `extracted`）。两种来源统一进入 `DeliveryContract`，双向覆盖检查对两者都生效。 |
| D-03 | deliverable_met 判定粒度 | **先做操作 + 路径级**（工具 + operation + 目标 path 匹配且对应 step_normal）；内容匹配（read 回读 == 写入内容）留待后续增强。`content` 字段必须存底但不参与匹配。 |
| D-04 | 旧请求无 required_operations | **回退机械完成语义**，但 `RunCompleted` 显式标记 `deliverable_met=false + deliverable_unverified`，前端如实展示，禁止宣称交付达成。 |
| D-05 | 多交付物 | **支持多条 DeliveryContract**，全部达成才 `deliverable_met`。契约为列表，与现有 `required_operations` 列表结构对齐。 |
| D-06 | Watchdog 超时回收 | 取消并 `await` 全部子任务（LLM/Tool/MCP/网络）；设置取消**宽限期**；宽限期后 `pending_calls` 仍 >0 时写结构化 WARNING 事件并强制 cleanup；终态后迟到事件必须被拦截。`pending_calls==0` 是**目标**而非阻塞条件（见 C-03）。 |

### 1.2 六条架构修正（C-01 ~ C-06）

| 编号 | 主题 | 修正结论 |
|------|------|----------|
| C-01 | 契约模型收敛 | 消除 `RequiredOperation` 与 `DeliveryContract` 概念撞车。收敛为**单一 `DeliveryContract`** 携带 `provenance`（`caller` / `extracted`）。权威性唯一；来源只用于审计与降级，不参与受信判定。 |
| C-02 | 双向覆盖定位 | 双向覆盖只保证"系统知道的全做了"，不保证"用户要的全被知道"。**定位为缓解手段**，不写进验收标准的达成依据。真正闭合漏洞靠 D-02（caller 显式契约）+ D-04（如实标记 unverified）。 |
| C-03 | Watchdog 回收语义 | 禁止无限等待。采用"取消 + 宽限期 `await`（建议 5s）+ 终态后迟到事件断言"。宽限期后仍 pending 则记录结构化 WARNING 并强制 cleanup，防止 watchdog 从"卡在长尾"变成"卡在清理"。 |
| C-04 | 引用校验粒度 | 引用合法性按 **per-input-field** 判定，由 OperationContract 的 `ref_allowed: bool` 声明；不能只按 per-operation output_schema 判定。 |
| C-05 | 终态幂等是前置 | `_fail`/`_complete`（`harness/core/scheduler/base.py`）必须先加**终态守卫**（fold 出 terminal 即拒绝再写），否则 watchdog/取消/熔断并发会写重复 `RunFailed`、终态后还可能跑 `RunCompleted`。S09 为 S10 前置。 |
| C-06 | 验证器边界 | `DeliverableVerification` 只**对照已存在的契约验证达成度**，绝不自行推断"用户要什么"。契约来源只有 `caller` / `extracted` 两个入口。 |

### 1.3 记录在案的限制与技术债（不阻塞，但必须写入文档）

- **L-01 时序语义**：契约暂不表达"read 必须在 write 之后"的时序要求。blackbox 场景靠工具语义兜底（先读不存在文件 → UNSUCCESSFUL → unmet），通用场景不保证。**已由 ADR-009 Q-03/Q-05 替代**：时序归 `DagStep.depends_on`，`DeliveryContract.after` 删除，不再由契约承载执行依赖。
- **L-02 内容匹配**：D-03 决定的 deferred 内容校验，历史契约的 `content` 已存底，未来可补。
- **L-03 晚到事件拦截范围**：只对 **run 事件流**（RUN_STARTED..终态）生效，**禁止**改为 EventStore 全局 append 拒绝——workspace 审计事件复用同一表（`run_id=workspace_id`），全局拦截会误伤。
- **L-04 真实 LLM 测试**：独立 opt-in 脚本（沿用 `scripts/test_real_llm_flow.py` 模式），不进 CI 确定性套件。

---

## 2. 步骤清单与依赖图（S01 ~ S12）

> 严格按序执行，禁止跳跃、禁止并行跨步骤。每个步骤 = 一个独立 AI 会话。

| 步骤 | 名称 | 依赖 | 核心交付物 | 关键验收 |
|------|------|------|-----------|----------|
| S01 | 架构决策固化 | 无 | ADR-007 扩展文档（决策编号可引用） | 文档一致、决策编号齐 |
| S02 | OperationContract 契约细化 | S01 | `tools.py` 新增 per-operation 契约 + 4 工具声明 + 单测 | per-op 副作用/确认/幂等/schema/`ref_allowed` |
| S03 | PlanGuardrail DAG 结构校验 | S01 | 结构校验纯函数 + 单测 | 唯一/依赖存在/自依赖/循环/层级 |
| S04 | 步骤输出引用契约 | S02 | `OutputRef` 模型 + 静态校验 + 单测 | 引用存在性/字段 schema/禁引字段 |
| S05 | DeliveryContract 收敛 | S01 | `intent.py` 模型 + 事件类型 + fold | 单一模型 + provenance + after + content 存底 |
| S06 | 覆盖检查 + 完成门升级 | S05 | forward/reverse 覆盖 + 完成门升级 + DeliverableVerification | deliverable_met / unverified 分离 |
| S07 | API 契约层 | S05 | `CreateRunRequest` 可选字段透传 + 抽取兜底 | caller/extracted 双来源 |
| S08 | Reviser 限权 | S06 | revise 不可变字段强制 | 不删/不改契约与目标路径 |
| S09 | 终态幂等守卫 | S05 | `_fail`/`_complete` 终态守卫 + 迟到事件拦截 | 无重复终态、无终态后 RunCompleted |
| S10 | 生命周期与取消 | S09 | 分阶段超时 + watchdog 取消 + 宽限期 + pending 断言 | pending==0 目标、宽限期、迟到拦截 |
| S11 | 可观测性 | S09 | 结构化日志/UTF-8/失败计数折叠/reload 目录 | run_id+phase+耗时、计数一致 |
| S12 | 回归与真实 LLM | 全部 | 确定性回归全量 + opt-in 真实 LLM 脚本 | 全量绿 + 13 项验收 |

**关键路径**：S01 → S02 → S04（引用校验依赖 S02 的 schema 与 `ref_allowed`）；S01 → S05 → S06 → S07/S08；S05 → S09 → S10 → S11 → S12。

---

## 3. 全局纪律（每个步骤的硬性约束）

1. **受信边界**：Planner/Reviser/classify/answer 是非受信组件，输出只是候选；PlanGuardrail / ToolExecutor / BaseScheduler / fold / EventStore / watchdog / 迟到拦截是受信组件，负责强制。受信组件不读 LLM 的 `task_state` 做判定（AGENTS.md 约束 4）。
2. **分层约束**：每步必须先完成前置步骤；禁止跨步骤实现未定义的内容（AGENTS.md §3.1）。
3. **同源契约**：所有新 Schema 用 Pydantic；新增/修改事件类型必须同步 `PAYLOAD_MODEL_MAP`、fold 分支、前端 OpenAPI 再生成（AGENTS.md §4.1/§6.3）。
4. **历史数据**：本方案**不做数据迁移**。旧 `.db` 不兼容时删除后重建，不猜测旧数据归属（沿用 v3.3 基线口径，但不沿用其文档约定）。
5. **异常结构化**：受信组件内部异常必须转成结构化事件写入 Event Store，禁止只写普通日志或让后台 task 静默结束（AGENTS.md §6.1）。
6. **测试纪律**：每个受信组件的新规则必须配套单元/集成/故障注入测试；真实 LLM 测试走 opt-in 脚本。
7. **异步约束**：新增 I/O 全部 `async`，禁止同步阻塞。

---

## 4. 状态跟踪

| 步骤 | 状态 | 执行会话 | 完成日期 | 备注 |
|------|------|----------|----------|------|
| S01 | 已完成 | opencode（S01 独立会话） | 2026-08-13 | 新建 ADR-008，D/C/L 与 §1 逐字一致 |
| S02 | 已完成 | opencode（S02 会话） | 2026-08-13 | OperationContract + 4 工具 operations 声明 + resolve 函数 |
| S03 | 已完成 | opencode（S03 会话） | 2026-08-13 | validate_dag_structure 纯函数 + 环路径 + 外部依赖不误报 |
| S04 | 已完成 | opencode（S04 会话） | 2026-08-13 | OutputRef 静态校验 + ref_allowed（file_op.path/content 禁引用） |
| S05 | 已完成 | opencode（S05 会话） | 2026-08-13 | intent.py + RunStarted 扩展 intent_raw/contracts + fold |
| S06 | 已完成 | opencode（S06 会话） | 2026-08-13 | CompletionVerdict 双维 + DeliverableVerification + 完成门升级 |
| S07 | 已完成 | opencode（S07 会话） | 2026-08-13 | CreateRunRequest.required_operations + ContractExtractor 抽取兜底 + OpenAPI 同步 |
| S08 | 已完成 | opencode（S08 会话） | 2026-08-13 | validate_revision_invariants + 合并副本校验 + C-02 反向覆盖 |
| S09 | 已完成 | opencode（S09 会话） | 2026-08-13 | _fail/_complete 终态守卫 + LATE_EVENT_REJECTED + 并发安全 |
| S10 | 已完成 | opencode（S10 会话） | 2026-08-13 | 分阶段超时 + _cancel_and_reap 宽限期 + TASK_CLEANUP_TIMEOUT |
| S11 | 已完成 | opencode（S11 会话） | 2026-08-13 | UTF-8 console + LLM/tool 结构化日志 + 失败计数折叠 + reload_dirs |
| S12 | 已完成（审查修复） | opencode（S12 follow-up） | 2026-08-13 | 隔离 cache clean-run 1101 passed / 2 skipped；内容匹配 L-02、时序 after L-01 明确延期 |

---

## 5. 步骤交接摘要（会话间通过文档 + 代码交接，不靠记忆）

| 步骤 | 读入（上游交付物） | 产出（写给下游） |
|------|--------------------|------------------|
| S01 | — | `ADR-007` 扩展文档 + 决策编号 |
| S02 | S01 决策编号 | `OperationContract`（含 `ref_allowed`）+ 工具声明 + 单测 |
| S03 | S01 | `validate_dag_structure()` 纯函数 + 单测 |
| S04 | S02 的 output_schema/ref_allowed | `OutputRef` + 静态校验 + 单测 |
| S05 | S01 | `DeliveryContract`/`UserIntent` 模型 + 事件 + fold 分支 |
| S06 | S05 契约模型 | 双向覆盖 + 完成门升级 + `DeliverableVerification` + 单测 |
| S07 | S05 | API 可选字段透传 + 抽取兜底 + 契约测试 |
| S08 | S06 | Reviser 不可变字段强制 + 修订重检 + 单测 |
| S09 | S05 | 终态守卫 + 迟到拦截 + 故障注入测试 |
| S10 | S09 | 分阶段超时 + 取消宽限期 + pending 断言 + 并发测试 |
| S11 | S09 | 结构化日志/UTF-8/计数折叠/reload 限制 |
| S12 | 全部 | 回归全量 + opt-in 真实 LLM 脚本 + 13 项验收报告 |

---

## 6. 本方案步骤文档模板（各步骤文档固定字段）

1. 步骤编号 / 名称 / 所属层 / 关联 Bug·Review·ADR
2. 前置依赖（必须已完成的步骤 + 交付物快照）
3. 问题背景（为什么做这一步 + 代码/日志证据）
4. 为什么这么做（架构决策依据，引用 D-xx / C-xx 编号）
5. 做之前先检查影响范围（必查清单）
6. 期望达到的目标（完成后可验证的状态）
7. 实现要点（schema/事件/函数签名/受信边界/禁止事项）
8. 验收标准（可执行验证清单）
9. 这么做的后果（对后续步骤影响、已知限制、技术债）
10. 收尾自检清单（跑测试 → 查事件链 → 查受信边界 → 更新文档 → 报告未决风险）
11. 完成状态栏（执行会话填写）

---

*本文档是本方案的唯一主控。步骤文档见本目录 `S0X_*.md`。*
*方案统筹：Agent 导师 · 2026-08-13*
