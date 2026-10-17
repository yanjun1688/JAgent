# Code Review — PRD v3.5 复杂度收敛与契约闭环（已完成代码全面评审）

| 属性 | 值 |
|---|---|
| **评审日期** | 2026-10-04 |
| **评审对象** | working tree 实际状态（分支 `docs/plan-r8b-r1r2`，含全部已提交与未提交改动） |
| **评审基准** | [`../prd/PRD_v3.5_复杂度收敛与契约闭环_问题定义与参照.md`](../prd/PRD_v3.5_复杂度收敛与契约闭环_问题定义与参照.md)、[`review_20260909_accidental_complexity_and_trusted_seam_audit.md`](review_20260909_accidental_complexity_and_trusted_seam_audit.md)（§5.1–§5.11）、[`../../BASELINE.md`](../../BASELINE.md)（§3.4）、[`../../AGENTS.md`](../../AGENTS.md) |
| **评审方式** | 只读；代码 + 测试断言逐行核对 + 全量验证命令。未改动任何生产文件 |
| **评审者** | 独立资深架构评审视角 |

## 验证命令结果（项目内工具链）

| 命令 | 结果 |
|---|---|
| `uv run ruff check harness tests scripts` | All checks passed |
| `uv run mypy harness` | Success: no issues found in 93 source files |
| `uv run pytest -q` | **1433 passed**, 1 warning（75.47s） |
| `uv run python scripts/check_dead_code.py harness evaluation scripts` | [OK] No dead private symbols across 114 file(s) |
| `uv run python scripts/generate_openapi.py --check` | [OK] OpenAPI artifacts are up to date |
| 前端 `npm run build`（`frontend/`） | tsc + vite build 通过 |
| 前端 `npx vitest run` | **70 passed**（13 test files） |

---

## 1. 总体结论：**有条件接受**

已完成的 R1/R2/R3/R5/R6/R7/R8/R9/R11/R12 整体兑现了 owner 裁决，受信边界在生产路径上**真实成立**——强制均由受信组件机械执行，未发现"靠 prompt / Agent 自觉"的规则；关键拒绝路径大多有真实触发用例；单一真源收敛质量高于一般水平。

未发现阻断级（安全绕过 / 假绿 / 完成门失效）问题。但有 **1 项 [重要]**：MCP 工具清单对 `mcp_call` 的描述 enrichment 是一处**死变异**（与 R3 同类的"算了不用"反模式，且现有全部门禁都照不出来），以及若干 [建议] 级文档/覆盖缺口。修复 [重要] 项并补齐 [建议] 中的机器强制后可视为完全接受。

---

## 2. 分级问题清单

### [重要]

#### I-1　MCP 工具清单 enrichment 是死变异，LLM 实际看不到 MCP 工具目录

- **所属**：R8 相邻（受信接缝可观测性）；溯源：**早于 v3.5**，自 ADR-010 声明式工具迁移提交 `7418ead` 起即存在
- **证据链**：
  1. `harness/api/app.py:119-121` 在 lifespan 中执行 `MCP_CALL_DEF.description += "\n\nAvailable MCP servers and tools:..."`；
  2. 但生产注册的不是 `MCP_CALL_DEF`：`harness/api/serve.py:213` 注册的是 `McpCallTool()` 类实例；
  3. `harness/tools/registry.py:59` 中 `td = tool.to_definition()` —— 注册定义由 `BaseTool.to_definition()`（`harness/tools/base.py:123-149`，读类属性）**新建**为另一个 `ToolDefinition` 对象；
  4. 因此对旧对象 `MCP_CALL_DEF`（`harness/tools/mcp_call.py:14-52`）的变异**零下游消费**，planner 看到的 mcp_call 描述永远是不含 MCP 目录的基础文本；
  5. `MCPConnectionConfig.auto_register_tools` 默认 **False**（`harness/models/mcp_config.py:17`），MCP 工具也不会作为一等工具注册——该描述是默认配置下 LLM 获知 MCP 工具的**唯一**通道。
- **为何现有机制没拦住**：`scripts/check_dead_code.py` 只查 `_` 私有符号，不查公有对象的属性变异；无测试断言 enrichment 到达注册定义。
- **影响（事实）**：默认配置下 LLM 只能靠猜测 server/tool 名使用 mcp_call，MCP 可用性退化；**不影响安全**（调用仍过 Tool Layer）。
- **修复方向**：enrichment 改为在注册前更新 `McpCallTool` 类描述，或在 lifespan 中经 registry 查到已注册的 mcp_call `ToolDefinition` 再追加；并加测试断言**注册定义**描述包含 `Available MCP servers and tools`。

### [建议]

#### S-1　R6"五处接缝"中 invariant 阶段的事件无真实触发测试

- **文件**：`harness/core/scheduler/revision_guard.py:367`（接线本身正确）
- **证据**：全仓仅 `tests/test_plan_rejection_events.py:177` 一处驱动 `_revise_with_degenerate_guard`，覆盖 degenerate；plan/revise/merge 有事件断言（4/5 接缝）。`validate_revision_invariants` 谓词本身在 `tests/test_reviser_restriction.py` 覆盖充分，但**经调度器落 `stage="invariant"` 事件**这条接线无任何测试 pin。审计 §5.8"8 例含五处接缝"的测试覆盖表述偏高。
- **修复方向**：补 1 例：构造交付不变量弱化的修订，驱动 guard，断言产生 `Diagnostic/plan_rejected` 且 `stage=="invariant"`、末次 recovered=False。

#### S-2　R1/R2 后两处过期文案仍宣称存在 fallback

- `harness/core/scheduler/plan.py:71` docstring："Falls back to the old serial path when Planner fails to produce a valid plan." —— fallback 已删，与 `plan.py:336-339` 的直接 `_fail` 矛盾；
- `README.md:169`："loop.py # AgentLoopScheduler — 串行循环 (降级路径)"，实际仅 evaluation/scripts 显式使用，已非"降级路径"。
- 对照：`harness/api/routes.py:306` 已正确同步。建议同步上述两处措辞。

#### S-3　"phase payload 类 ↔ 聚合 Union"无完备性断言

- **文件**：`tests/test_event_contract_completeness.py`
- 现有断言只保证 `EventType ↔ PAYLOAD_MODEL_MAP` 键集合相等与 fold 穷举；若有人新定义一个 `XxxPayload` 却忘记加进聚合 Union，无红灯，直到写时解析失败。
- **修复方向**：反射收集 events 模块全部具体 payload 类，断言其均可经 `PAYLOAD_MODEL_MAP` 的 Union args 到达（即 `PAYLOAD_CLASS_TO_TYPE` 覆盖全部 payload 类）。

#### S-4　判定路径仍有枚举/phase 字面量副本，AGENTS.md §6.5 规则 1 无对应机器门禁

- 证据：`harness/core/context_manager.py:417` `status_val in ("failed","timeout","guardrail_blocked")`、`:419` `"unsuccessful"`；`harness/core/scheduler/base.py:724` phase `"created"` 等。行为均正确（**主观判断**：一致性问题，非缺陷）。
- R7 的 AST 守卫只管 unknown-tool 文案，未覆盖此类。建议判定一律用 `ToolResultStatus` 枚举成员/谓词，或把 AST 规则扩到枚举字面量禁用。

#### S-5　前端 `isTerminalRunEvent` 语义/命名与后端不对齐

- `frontend/src/api/types.ts:33` `TERMINAL_RUN_PHASES = {'completed','failed','orphaned'}` —— 实现的是后端 `is_run_stopped`（含 orphaned），名字却对应 `is_terminal_event`（不含 orphaned）。当前消费方（`frontend/src/pages/ChatPage.tsx:414-415`）行为正确。建议改名或拆分两个谓词，防止后续误用。

#### S-6　`mcp_manager.connect_server` 的 `except BaseException` 会吞 CancelledError

- `harness/tools/mcp_manager.py:127-130`：取消被转成 `{"success": False}` 普通返回，与项目在 `harness/core/planner/agent.py:265-273` 刻意建立的"CancelledError 必须原样传播"模式不一致。建议 `except BaseException` 中对 CancelledError 清理后重抛。

#### S-7　episode 生成中 plan step 状态渲染恒为 "?"

- `harness/core/context_manager.py:528-537`：`st = step.get("status", "?")`，而 payload 中的 plan steps（`plan_steps_to_payload`，`harness/core/dag_executor.py:40-50`）根本不含 `status` 键 → 该渲染块恒输出 ": ?"。存量误导代码（非 v3.5 引入），建议删除该分支或改用真实字段。

#### S-8　一处近乎空转的测试断言

- `tests/test_local_repair_wiring.py:139-140`：对一个从未运行、事件为空的 run-id 断言无 STEP_REPAIR 事件，且元组写成 `(STEP_REPAIR, STEP_REPAIR)` 重复成员。真正有效的断言是上一行 `_local_repair_budget() is None`。建议清理。

---

## 3. 假绿 / 受信缺口专章

**发现 2 处"文档/直觉以为被防护、实际有缺口"的点（均非安全假绿）：**

1. **MCP 目录注入实际不生效** —— 见 I-1。所有门禁（ruff/mypy/pytest/dead_code/OpenAPI）全绿，但该功能自声明式工具迁移起即静默失效，是本次评审最实质的发现。
2. **invariant 阶段拒绝事件未被测试 pin** —— 见 S-1。受信**拒绝本身**是真的（谓词有测试、调度器有接线），缺的只是"事件可观测接线"的机器保护；若接线被误删不会变红。

**明确未发现的假绿模式（已逐一核实）：**

- R5 复检不在测试替身里：守卫位于生产方法 `_execute_plan` while 循环顶部（`harness/core/scheduler/plan.py:482`），测试直接调用该生产方法，且 local-repair/revise 重入均真实回到该点；
- R6 写入接缝被生产模块真实 import 与调用（`harness/core/planner/agent.py:122,208`、`harness/core/scheduler/revision_guard.py:348,367`、`harness/core/scheduler/plan.py:672,892`），非仅测试生效；
- 未发现"只断言不抛异常 / 断言恒真 / mock 掉全部被测逻辑"的关键路径测试（S-8 为边角例外）；degenerate/merge 场景中被 mock 的仅是 LLM 非受信路径，**受信被测逻辑全部真实执行**；
- classify 保守门（`harness/core/scheduler/classify.py:53-62,147-152`）先于 LLM 机械强制，LLM 超时/异常均 fail-closed（True）；
- 完成门 fail-safe 未见反转：空契约 unverified，机械/交付不过即 PlanFailed + RunFailed（`harness/core/scheduler/plan.py:1060-1098`）。

---

## 4. 删除 / 可达性核验

### R1 — 成立

- 弱完成代码仍在 `harness/core/scheduler/loop.py:184-190,195-200`（直写 RunCompleted），但按裁决这是**保留的非生产技术债**；
- 生产装配唯一调度器：`harness/api/deps.py:166` `PlanningExecutorScheduler`，`_schedulers` 类型也钉死（`harness/api/deps.py:61`）；
- 全仓 grep 确认 `AgentLoopScheduler` 的实例化仅在 `evaluation/run_eval.py`、`scripts/*`、`tests/*`，harness 内仅 re-export；ADR-013 的 grounding 装配期选调度器**尚未实现**（deps 无 grounding 代码），不存在第二条生产入口；
- 结论：弱完成路径在生产**确实不可达**，论证不依赖注释。残留仅 S-2 的两处过期文案。

### R2 — 成立

- `planner.plan()` 重试耗尽返回 None → `harness/core/scheduler/plan.py:336-339` 直接 `_fail("Planner failed to produce a valid plan")`，harness 内再无 `new AgentLoopScheduler`、无 `_get_or_fallback`；
- `tests/test_planner_failure_no_fallback.py:45-52` 真实驱动完整 `sched.run`，断言 Run/failed 且 final_error 含原文、无 Run/completed；
- **残留（已记录在案）**：`scripts/test_v07_integration.py:507,544` 仍有 fallback 陈旧测试文案，审计 §5.10 已明确列为脚本独立清理项（scripts 不被 pytest 收集，不影响 CI）。

### R9 — 删除干净

- `harness/api/query.py`、`tests/test_query.py`、前端 `api/ops-client.ts` 均已删除；grep 确认**代码中零引用**（剩余命中全在 docs 历史记录）；`harness/api/app.py:202-205` 不挂载 query 路由；
- OpenAPI 已重生（`/api/v1/query` 与 `QueryResponse/QueryMeta` 不存在，`--check` 通过）；
- 附带删除项 `SchedulerConfig.to_public_dict()` 无残留（grep 无命中）；REST `harness/api/routes.py` 成为唯一 run 读模型；
- 同理 `DagExecutor.execute()` 删除干净：公共面只剩 `execute_layer/retry_step`（`harness/core/dag_executor.py:101-134`），无 `_guardrail` 属性与相关 import，全仓无 `dag.execute(` 调用点。

---

## 5. 做得好的部分（请勿在后续误改）

1. **R5 复检位置设计正确**：循环顶部同时覆盖首次/修订/local-repair 重入，且复用同一个 `planner.guardrail` 实例（无第二份校验实现）；merge 后另有第二道复检（`harness/core/scheduler/plan.py:666-681,886-901`）。
2. **R6 接缝的 fail-safe 降级**：store/run_id 缺失时降级为日志（`harness/core/planner/rejections.py:45-53`），观测设施永不阻塞受信校验——按设计保留，勿改成抛异常。
3. **R7 的 AST 机器守卫**（`tests/test_tool_existence_convergence.py:65-77`）真实扫描全 harness 字符串，白名单仅留给真源与 MCP 概念，是 AGENTS.md §6.5"规则必须有机器强制"的正面样板。
4. **按设计保留的默认方向**：`ToolWhitelistGuardrail` 的 fail-open（workspace 未声明 allowed_tools 即放行）；serial 无 registry 的列表查找（文案仍走共享源）；未知事件行 read 容忍（`event_from_row` 跳过）——均为记录在案的例外，禁止顺手反转。
5. **R12 根治质量**：Mixin 全部以 `TYPE_CHECKING` stub 让 mypy 真实受检（`harness/core/scheduler/classify.py:72-83`、`revision_guard.py:240-245`、`local_repair.py:101-110`）；`BaseTool` 仅对需实例覆盖的字段去 ClassVar，其余保留 ClassVar，粒度准确；7 处行内 ignore 均为存量（无新增）。
6. **取消语义的审慎模式**：`harness/core/planner/agent.py:265-273`、`harness/core/context_manager.py:582-587` 明确只捕 Exception、放行 CancelledError，并有注释论证。

---

## 6. 未决项现状确认

| 项 | 文档声称 | 实际核验 | 结论 |
|---|---|---|---|
| **R4** | ~~owner 已裁决暂缓~~ **已于 2026-10-04 关闭** | 全部 token 记账功能删除（Run 记账 + 压缩遥测 + run_monitor 检测），驱动压缩的估算器保留；阈值提至 200K×0.8=160K；估算补算 summary；SUMMARIZE 提示词范式重写。详见 PRD §处置注记 | 已关闭 |
| **R10**（不在本次评审） | 部分：事件模型未进 OpenAPI，前端 `event_type: string` | 属实：`scripts/generate_openapi.py` 仍只取 `app.openapi()`，`schema.ts` 事件字段仍为 string；前端多处视觉表为刻意保留的不同视觉语义 | 与文档一致 |
| **键名隐患** | payload 用 `step_id/tool_name`，`DagStep` 用 `id/tool`；列为后续独立项 | 属实：`harness/core/dag_executor.py:42-43`、`harness/core/fold.py:182-188` 与 `harness/models/plan.py:16-17` 键名确实不一致，消费侧靠转换衔接；当前无功能错误 | 与文档一致，维持暂缓 |
| 超前实现 | 不应有 | ADR-013 grounding 调度器选择仅文档、未实现；未发现其他超前于分层的代码 | 无 |

---

## 7. 建议处理顺序

1. **I-1**（含补测试）；
2. **S-1 / S-3**（补齐机器强制）；
3. 其余文档性建议随近次提交顺手收口。

---

*本报告为只读评审产物，未改动任何生产代码、未提交、未重构。事实问题均可复现；标注"主观判断"处为评审者意见。*
