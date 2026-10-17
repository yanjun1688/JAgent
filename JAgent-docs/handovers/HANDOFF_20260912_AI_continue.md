# HANDOFF — JAgent/Harness 后续工作交接提示词

> 交接日期：2026-09-12 · 交接人：项目负责人 · 接收方：下一个 AI 工程师
> 请把本文件当作**你接手这个项目的第一份、也是唯一一份入场说明**。读完再动手。

---

## 0. 你的角色

你是 **JAgent / Harness Agent-First 任务执行引擎**的**架构导师 + 实现工程师**。

- 第一身份是**架构守护者**：任何一行代码都必须符合 Harness 的受信边界设计，而不是图开发方便。
- 在不确定时**必须提问，禁止猜测**（见 §7）。
- 语言：与 owner 用**中文**沟通；代码注释保持仓库既有风格（中英混排，勿新增无关注释）。

项目根：`D:\Project\JAgent`（Windows 10/11，Shell = **PowerShell 5.1**，非 bash）。
仓库规范：**先读 `AGENTS.md`（根目录）**，它是最高约束；本文件是它的快速落地版。

---

## 1. 项目一句话与架构铁律（不可违背）

**核心范式：Agent 决策 + 系统强制。** Agent Kernel(LLM) 决定"做什么"，受信组件（Event Store / Tool Layer / Scheduler）决定"不允许做什么"。状态由 Event Store 事件流 `fold` 得出，**不写 Workflow Engine / DAG 调度器 / 预定义步骤编排**。

四条铁律（AGENTS.md 约束 1–4）：

1. 所有副作用只能发生在 **Tool Layer**；Agent 不直接碰 IO/网络/文件系统。
2. 幂等键由 **Tool Layer** 按工具契约自动计算，Agent 不感知。
3. 每次 think-act-observe 循环后，**系统自动**写事件，不依赖 Agent 主动触发。
4. 危险操作拦截由 **Tool Layer Guardrails** 负责，**System Prompt 提不提醒都拦**。

分层（禁止跨层跳跃）：L1 Event Store → L2 Tool Layer → L3 Scheduler → L4 Agent Kernel → L5 工具注册/MCP/SKILL → L6 接口层 → L7 前端可观测。

**Derive, don't duplicate**：状态从 `fold` 出、TS 类型从 Pydantic 出、OpenAPI 进 CI diff、prompt 从注册表出、阈值一个 config。每次想"再写一份"的地方就是未来的漂移点。

---

## 2. 环境、命令与"必踩的坑"

### 2.1 校验命令（必须用项目解释器，禁止全局 `python -m ...`）

```powershell
# 后端（在 D:\Project\JAgent）
uv run ruff check harness tests scripts evaluation
uv run mypy harness                      # 全局 mypy 会卡死 >5min；项目 venv 内仅 ~1-2s
uv run pytest -q                         # 完整回归
uv run python scripts/generate_openapi.py --check
uv run python scripts/check_dead_code.py harness evaluation scripts

# 前端
npm --prefix frontend run build          # tsc && vite build
npm --prefix frontend run test           # vitest run
```

Windows 若没有 `uv`，用项目 venv 显式调用：`.venv\Scripts\python.exe -m mypy harness` 等。
这些命令已与 CI（`.github/workflows/ci.yml`）和 pre-commit 对齐。

### 2.2 当前绿色基线（你接手时应复现）

| 检查 | 基线 |
|---|---|
| `pytest -q` | **1423 passed**（2026-10-02 复现；原文 1522/2 skipped 为 9-12 中间快照） |
| `ruff` / `mypy` | 全绿 |
| `generate_openapi.py --check` | OK |
| `check_dead_code.py` | 112 files 无死符号 |
| 前端 `build` + `vitest` | 通过 / **70 passed** |

**任何改动后必须回到这个基线。** 若某步失败，先定位是回归还是预期契约变更。

### 2.3 环境坑（血泪，务必遵守）

1. **绝对不要**用 `Get-Content -Raw | Set-Content -Encoding UTF8` 改文件——PS 5.1 会把 UTF-8 中文按 GBK 读坏，直接损坏源码（本仓库已发生过两次）。**改文件只用编辑工具（edit / write）**。
2. `rg`（ripgrep）**不在 PATH**；搜索用编辑器内置 grep 或 `Select-String`。
3. PowerShell 里不要用 `&&`，用 `cmd; if ($?) { cmd2 }` 或 `;`。
4. 用 Python 脚本走管道时记得 `-u`（防块缓冲刷屏）。
5. 未跟踪文件很多：**未经明确指示不要 `git commit`**（见 §7）。

### 2.4 运行中的服务

```powershell
.venv\Scripts\python.exe -m uvicorn harness.api.serve:app --host 127.0.0.1 --port 8123 --loop harness.api.loop:event_loop_factory
```
- 无 `--reload`；改了代码需手动重启。
- pid/日志在临时目录 `C:\Users\1\AppData\Local\Temp\opencode\`（`server.pid` / `server_out.log` / `server_err.log`）。
- `.env` 里有真实 LLM key，**不要读取/打印/提交**；只引用变量名（`LLM_API_KEY` / `LLM_MODEL_NAME` / `LLM_BASE_URL` / `LANGFUSE_*`）。

---

## 3. 当前状态（接手时的真实画面）

### 3.1 最近这一轮已完成（都在工作区，**未提交**）

1. **根因 A 修复（引用契约）**：字段角色收为单一真源——`models/tools.py` 的 `FieldRole` + `FIELD_ROLE_KEY="x-role"`，默认 `AUTHORITY` fail-closed，删 `ref_allowed_fields`，`ref_allowed()` 由字段角色派生。E2E run `f83e98ee` 已 `completed`。文档已同步 `ADR-008/010/013`、`plans/completion-semantics/*`。
2. **根因 B 修复（诚实失败）**：删除生产 serial fallback（`scheduler/plan.py` 的 `_get_or_fallback`）；planner 产不出合法 DAG → 直接结构化 `RunFailed`；`local_repair_enabled` 默认 `True`；删除 `local_repair_allowed_tools` 与 `HARNESS_LOCAL_REPAIR*` env。`AgentLoopScheduler` 仅评测/脚本显式使用。
3. **失败预算收敛**：`max_iterations` + `max_consecutive_failures` 合并为单一 `SchedulerConfig.run_attempt_budget`（默认 20）；保留 `max_revise_retries` / `max_confirm_retries` / `local_repair_max_rounds`（语义不同）。
4. **Tier 0 死代码清理**：删前端孤儿页 10 + 旧组件 11 + `api/ops-client.ts` + 6 废弃测试；后端修 `harness/__init__.__all__`、删 `run_monitor` 无副作用表达式、清 `.env` 过期项。
5. **重复字段/未抽象收敛（§5.3，详见审计文档）**：terminal 集合单常量、`0.25` 系数单常量、`_row_to_event` 三份合一（`storage.event_store.event_from_row`）、`step_normal` 单谓词（`dag_types.exec_state_is_normal`）、`SchedulerConfig.to_public_dict()`、query 工具分桶/pending 复用、`ToolDefinition`/`OperationContract` 公共基类 `OperationAttributes` + `effective_*` 单点优先级、blob 单一模型 `OutputBlobStoredPayload`、事件再导出单一来源（`harness.models.__all__` 补全 41 payload，顶层派生）、删 `RunCompletedPayload.deliverable_met`（由 `deliverable_status` 派生）。
6. **Bug 修复**：`frontend/src/components/chat/ThinkingPanel.tsx` 原先 switch 六个后端不存在的事件名，导致思考面板恒空；已改为规范 `EventType`。

### 3.2 权威跟踪文档（先读这三份）

- 审计与状态总表：`JAgent-docs/reviews/review_20260909_accidental_complexity_and_trusted_seam_audit.md`（**§5.1 R1–R12 状态 / §5.2 Tier0 / §5.3 重复字段收敛**）
- 问题定义 PRD：`JAgent-docs/prd/PRD_v3.5_复杂度收敛与契约闭环_问题定义与参照.md`（A–F 六大结构性问题）
- 方案与裁决：`JAgent-docs/plans/PLAN_20260910_R8b_R1_R2_LLM封装收敛与调度器接缝方案.md`

---

## 4. 未完成工作（按优先级给你排好）

### P1 — 审计 R 项（仍未做，原文见审计 §5.1）

| # | 问题 | 现状证据 |
|---|---|---|
| R3 | 压缩三档阈值"算了不用"，构造参数是假配置 | `harness/core/context_manager.py:149/163/168` 仍硬编码 `<=0.5` / `>0.7` / `>0.9`；`__init__` 算出的 threshold 只进日志 |
| R4 | **已关闭（2026-10-04）** | 全部 token 记账删除（Run 记账 + 压缩遥测 + run_monitor 检测块），估算器保留；阈值 200K×0.8=160K，比例 0.6/0.8/0.95；估算补算 summary；SUMMARIZE 提示词重写 |
| R5 | 执行入口计划级复检生产不走 | 生产调 `dag_executor.execute_layer()`；`execute()` 的 `validate` 仅测试路径 |
| R6 | **已完成（2026-10-02）** | 五处拒绝接缝统一落 `Diagnostic/plan_rejected`；回归 `tests/test_plan_rejection_events.py` + 真实 LLM BDD 脚本 |
| R7 | "工具是否存在/允许"仍有 8 份判定、规则/文案重叠 | registry / planner.guardrail / dag_executor / revision_invariants / contract_extractor / intent / scheduler.base |
| R9 | 后端读模型重复 | REST `api/routes.py` 与 `api/query.py` 的 run 列表/详情各一份；前端侧已在 Tier0 删净 |
| R10 | 事件未进 OpenAPI；前端靠硬编码表 | `scripts/generate_openapi.py` 仅取 `app.openapi()`；前端 `event_type: string` |
| R12 | `pyproject.toml` mypy 大面积 `disable_error_code` 豁免 | `pyproject.toml:61-116` |

（R1/R2 已解决；R8 基本完成；R11 已完成；R6 于 2026-10-02 完成。）

### P2 — PRD v3.5 结构性问题 A–F 中未闭环者

A 同语义多实现（=R7/R9/R10）、B 概念/状态空间、C 启发性度量（=R3/R4）、D 契约同源（=R10）、E Windows/编码、F 元流程。建议**冻结新功能、只做删除/收拢**，每个迭代砍一个非内核概念，砍完跑全量。

### P3 — 已知明确隐患（改动要单独立项）

- 事件 payload 里 `PlanCreatedPayload.steps` / `PlanRevisedPayload.steps` 是 `list[dict]`，内部键是 `step_id`/`tool_name`，而 `models/plan.py:DagStep` 用 `id`/`tool`——键名不一致，`fold._apply_blueprint` 按字符串读。类型化为 `DagStep` 涉及事件 schema + fold，**列为独立项**。
- 前端已无消费者：`/api/v1/query` 查询门面（`api/query.py` 的 `type=runs|run|events|...`）在 Tier0 删掉前端后**没有前端调用方**。保留还是裁剪，需 owner 决策（裁剪属公开 API 契约变更，不算纯清理）。

### P4 — 明确"不要动"的（经评审判定为刻意设计，非重复）

- **状态枚举** `RunStatus`/`ExecState`/`TaskState`/`ToolResultStatus`/`ExecutionStatus`：**正交状态机**，v2.1 起刻意分层，**不合并**。
- `RunFailedPayload.final_error`/`result_summary`/`user_facing_message`：内部原因/摘要/用户面三受众。
- `RunStartedPayload.intent`/`intent_raw`/`current_request`：处理后/原始/当前请求。
- `Event.payload`、`RunState.latest_plan/plan_history/step_tasks/completion_evidence`、`QueryResponse.data` 的动态 dict：读路径/纯函数折叠的刻意设计；写入侧已由 `PAYLOAD_MODEL_MAP` 校验。
- 前端 event color/label/terminal 多表：badge/category/status 不同视觉语义。

---

## 5. 关键文件地图

| 关注点 | 文件 |
|---|---|
| 事件类型/载荷/注册表（41 种） | `harness/models/events.py`（`EventType` / `PAYLOAD_MODEL_MAP` / `TERMINAL_EVENT_TYPES`） |
| 唯一状态推导 | `harness/core/fold.py`（`fold_events` / `RunState` / `StepEvidence`） |
| 调度（planning 主路径） | `harness/core/scheduler/plan.py`、`base.py`（`SchedulerConfig`）、`completion.py`、`local_repair.py`、`revision_guard.py` |
| 串行调度（仅评测） | `harness/core/scheduler/loop.py`（`AgentLoopScheduler`） |
| 工具层契约/执行/护栏 | `harness/models/tools.py`、`harness/tools/{executor,guardrails,registry,idempotency,output_store}.py` |
| DAG 执行 | `harness/core/dag_executor.py`、`dag_types.py`（`ExecState`/`exec_state_is_normal`） |
| 接口层 | `harness/api/{routes,query,schemas,serve,deps}.py` |
| 读模型 | `harness/api/schemas.py`、`harness/replay/schemas.py`、`harness/analysis/schemas.py` |
| 前端类型 | `frontend/src/api/schema.ts`(生成) 、`types.ts`、`analysis-styles.ts`；生成器 `scripts/generate_openapi.py` |
| 测试工厂/规范 | `tests/conftest.py`；`tests/test_event_contract_completeness.py` 保证 enum=payload map 1:1 |

---

## 6. 工作方法（AGENTS.md 落地版）

1. **动手前三步**：先报告差异（列出偏离/超前/滞后，说明影响）→ 得到 owner 确认 → 才改代码。
2. **先分层、后实现**：确认需求属于哪一层，前置依赖是否满足；**禁止跨层跳跃**（例如 L4 未完成不能上 L6）。
3. **Bug 修复要根治**（禁止打补丁）：回答①根因②为什么现有机制没拦住③如何在**受信组件层**加通用防护。修完必须追加该根因场景的回归测试。
4. **TDD / 契约同源**：事件 payload、API 响应结构一律以后端 Pydantic 为唯一来源；改后端模型要同步跑 `generate_openapi.py`（`--check` 必须在 CI 绿）。
5. **不确定就问**：工具 `side_effects` 范围、`requires_confirmation` 是否危险、payload 字段未定义、前后端字段命名/类型/空值策略、沙盒隔离级别、压缩阈值口径——**这些一律禁止猜测**。
6. **不提交**：未经 owner 明确要求，不要 `git commit` / `push`。目前工作区有大量未提交改动，owner 会在功能与端点验证通过后自己推。

---

## 7. 交接给你的第一条行动建议

1. 先跑一遍 §2.1 的全部命令，确认 §2.2 基线（这一步也能让你熟悉工具链）。
2. 读 §3.2 的三份文档、并 `git status` / `git diff --stat` 看清当前未提交改动。
3. 向 owner 确认本轮目标（建议从 **R3 / R6 / R9** 中选一个——它们是"低风险、可独立闭环"的收拢项；R7/R10/R12 更大，需要单独方案）。
4. 严格按 §6 流程推进，每个 PR/提交只做单一主题，改完回到绿色基线。

---

*本文件可随工作推进更新。约束细节以 `AGENTS.md` 与 `JAgent-docs/` 为准；有冲突时以 AGENTS.md 与 owner 裁决为准。*
