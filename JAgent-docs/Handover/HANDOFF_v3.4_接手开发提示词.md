# 交接提示词 — v3.4 执行循环韧性与上下文证据治理（接手开发）

> 用法：把下方「=== 提示词开始 ===」到「=== 提示词结束 ===」之间的整段内容，
> 连同项目根目录 `D:\Project\JAgent` 一起交给接手的 AI。当前处于 build 模式，
> 允许改代码，但必须严格 TDD（先红后绿），且遵守 AGENTS.md 的受信边界。

=== 提示词开始 ===

你是 Harness（Agent-First 任务执行引擎）项目的接手开发者。请先完整阅读以下文档，
它们是本次工作的权威上下文，务必先读再动代码：

【必读文档（按顺序）】
1. `AGENTS.md` — 开发协作规范。铁律：Agent(LLM) 有决策权，受信组件（Event Store /
   Scheduler / Tool Layer / 完成门）有强制权；受信判定绝不能引入 LLM；所有副作用只在
   Tool Layer；事件溯源 append-only；状态唯一来源是 `harness/core/fold.py::fold_events`。
2. `JAgent-docs/Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md` — 本次任务的技术设计。
   重点看：§1 根因独立复核（注意：PRD 原归因被代码+真实事件流证据修正，真凶是
   "browser 环境故障 + Planner 不会换工具 + resume 不重建执行态"，不是"压缩删证据→门控误判"）、
   §3 方案 A（已选定：事件溯源内修，不引入 Temporal/LangGraph）、§4 受信边界矩阵、
   §6 新增事件、§8 TDD/BDD 测试计划、§9 实施顺序。
3. `JAgent-docs/architecture/ADR-011_受信执行态证据投影与输出引用.md` — 架构决策记录。
4. `JAgent-docs/prd/PRD_v3.4_执行循环韧性与上下文证据治理.md` — 原始需求（注意其根因叙述
   已被 DESIGN §1 修正，以 DESIGN 为准）。

【当前分支与状态】
- 分支：`feat/v3.4-execution-resilience`（已从 main 切出，直接在其上继续）。
- 所有改动尚未 commit。工作区有新增设计文档 + 代码改动 + 新测试。
- 运行测试必须用项目虚拟环境（系统 python 缺 langfuse 等依赖）：
  `& .venv\Scripts\python.exe -m pytest tests/ -q`
  lint/类型检查：`& .venv\Scripts\python.exe -m ruff check harness tests`
  与 `& .venv\Scripts\python.exe -m mypy harness`（如 venv 装有 mypy）。

【已完成（测试已绿，勿重复实现）】
- F-1 受信执行态证据投影：`harness/core/fold.py` 新增 `StepEvidence` dataclass 与
  `RunState.step_evidence: dict[str, StepEvidence]`，由 PLAN_CREATED/REVISED +
  DAG_STEP_STARTED/COMPLETED/FAILED/SKIPPED + TOOL_COMPLETED 确定性折叠；
  EpisodeArchived/ContextPruned 压缩只裁 LLM 工作视图（thought_history/tool_results），
  **永不裁 step_evidence**。测试：`tests/test_step_evidence_projection.py`（5 项全绿）。
- F-3 token_limit 配置外置：`harness/core/context_manager.py` 新增
  `resolve_context_token_limit()` 与 `DEFAULT_CONTEXT_TOKEN_LIMIT`（128000×0.7），
  env `HARNESS_CONTEXT_TOKEN_LIMIT`；`harness/api/serve.py` 移除硬编码 `token_limit=3000`。
  测试：`tests/test_context_config.py`（全绿）。
- F-2 大输出 blob 引用：新增 `harness/tools/output_store.py`（OutputBlobStore，
  content-addressed sha256 blob，ref 严格校验为 `<64hex>.json` 防路径穿越，
  workspace 作用域经 ExecutionBackend 写入 `.harness_outputs/`）；
  `harness/tools/fetch_output.py` 新增受信只读工具 `fetch_output`；
  `harness/tools/executor.py` 在成功路径调用 `_offload_if_large()`：超大输出事件体只存
  `{summary,ref,sha256,bytes,truncated}` 占位符并写 `OutputBlobStored` 事件，内存结果保留
  全量供本 run 下游用；`models/events.py` 新增 `OUTPUT_BLOB_STORED` 事件 + payload +
  PAYLOAD_MODEL_MAP；`serve.py` 注册 FetchOutputTool。测试：`tests/test_output_blob.py`（16 项全绿）。
- F-4/F-5/F-6 受信恢复纯函数核心：新增 `harness/core/recovery.py`（无 I/O、无 LLM）：
  `rebuild_results_from_evidence()`（F-4 从证据投影重建 results，running→PENDING 不误判完成）、
  `unresolved_known_bad_steps()`（F-5 检测修订补丁未覆盖的非瞬时失败步骤，治 e05087b6 的
  "s3 browser 用同幂等键重放已知坏动作"）、`classify_failure_tier()`、
  `RecoveryBudget` + `validate_local_repair()`（F-6 局部修复受信预算：轮次上限/工具白名单/
  禁新增 mutating 动作）。测试：`tests/test_recovery_core.py`（16 项全绿）。
- F-4 已接线：`scheduler/plan.py::_execute_plan` 入口用 step_evidence 重建 results，
  并含"换工具则重跑"安全闸。
- F-5 已部分接线：`_execute_plan` 修订合并后调用 `unresolved_known_bad_steps()`，
  若有未覆盖坏步骤则注入 high 优先级 FeedbackInjected（error_type=unresolved_known_bad_steps）
  强制下轮修复，不静默重放。
  > **2026-09-08 review 修正（A′）**：`unresolved_known_bad_steps` 已由 tool-only 改为
  > **(tool, 规范化 input) 全签名**比较，与退化修订守卫共享 `dag_types.action_signature`
  > 定义；同 tool 改 input 视为已修复。原始 input 经 `original_inputs=` 传入（merge 前失败计划）。
  > 详见 `JAgent-docs/reviews/review_20260908_recovery_f5_signature_granularity.md` 与
  > DESIGN §11.5。以下"待完成"第 3 项提及的失败分级接线不受影响。

【待完成（按 DESIGN §9 顺序，继续做）】
1. 先跑全量 `pytest tests/ -q` 确认当前无回归（上一次全量跑被人工中断，未看到最终结果）；
   再跑 ruff/mypy。有红先修红。
2. F-6 接线（核心纯函数已就绪，缺集成）：在 DAG 步骤失败、工具级 retry 用尽后，于受信预算内
   驱动"针对该步骤的 bounded 局部 think-act"循环。强制边界全部用 recovery.py 的
   validate_local_repair 机械判定；LLM 只产出建议动作，所有动作仍过 ToolExecutor
   （guardrails/幂等/确认）。需要新增事件 `StepLocalRepairStarted/Completed`（DESIGN §6.1）。
   建议本期默认关闭（config 开关），仅显式启用的 run 生效，降低回归面。
3. F-5 失败分级完整接线：把 `classify_failure_tier` 接到 dag_executor/RetryRunner，
   使"可重试的基础设施类语义失败(success:false)"先工具级 retry，retry 用尽再升级；
   merge 时对"未覆盖坏步骤"除注入反馈外，考虑升级为子 DAG 补丁 replan（SubplanRevised 事件）。
4. L6/L7：Replay Inspector 暴露 step_evidence —— `harness/replay/schemas.py` 加只读视图、
   `replay/projection.py::project_state_view` 输出证据投影（时间旅行视图证据不再因压缩消失，AC-5）；
   前端 OpenAPI 类型自动生成，勿手改前端类型。
5. 补齐 DESIGN §8 的回归测试 T-REG-1~6 中尚未覆盖的部分（尤其 T-REG-4 resume 崩溃重建的
   端到端测试、T-REG-2 螺旋回归），严格先写失败测试再实现。
6. 新增事件类型务必同步：EventType 枚举 + Payload 模型 + PAYLOAD_MODEL_MAP + fold 分支
   + 前端枚举（AGENTS.md §6.3 校验清单）。

【关键代码位置】
- 折叠/证据投影：`harness/core/fold.py`（StepEvidence、_evidence()、_apply_blueprint()）
- 恢复纯函数：`harness/core/recovery.py`
- 输出收口：`harness/tools/output_store.py`、`harness/tools/fetch_output.py`、
  `harness/tools/executor.py`（_offload_if_large）
- 调度循环：`harness/core/scheduler/plan.py`（_execute_plan 重建在 ~654-690，F-5 反馈注入在
  修订合并后 ~850-900；完成门 CompletionVerdict.compute 在 ~76-119）
- 步骤执行/skip 门控：`harness/core/dag_executor.py`（_gate_skip_reason ~413）
- 真实失败 run 证据：`.harness.db` 中 run_id=`e05087b6`，33 个事件（DESIGN §1.1 有逐条表）。

【工作纪律】
- 严格 TDD：每个改动先写复现/失败测试（红），再实现（绿），再重构。
- 受信组件异常一律转结构化错误事件，不泄漏到非受信层；受信逻辑保持纯函数、无 I/O、无 LLM。
- 不引入 Temporal/LangGraph 等外部持久化框架（DESIGN §3 已否决，理由见文档）。
- 每完成一个 F 项，跑相关测试 + ruff，全部通过后再进下一项；不要一次性大改。
- 完成后跑全量 pytest + ruff + mypy，把结果汇报给我。

=== 提示词结束 ===
