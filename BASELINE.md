# 基线快照 — 架构收敛与漂移治理（阶段零起点）

| 属性 | 值 |
|---|---|
| **记录日期** | 2026-09-09 |
| **分支** | `guardrail/phase-zero`（基于 `main` @ 678e23e） |
| **用途** | 本次"复杂度收敛 / 重复实现合并"工作的回归对比基线；每个阶段结束时核对"通过数不低于基线"（见执行指令 DoD） |
| **依据** | `JAgent-docs/prd/PRD_v3.5_复杂度收敛与契约闭环_问题定义与参照.md`、`JAgent-docs/reviews/review_20260909_accidental_complexity_and_trusted_seam_audit.md` |

---

## 1. 测试基线

- 全量 `uv run pytest`：**1433 passed, 2 skipped**（耗时约 61s，Python 3.11 / Windows）。
- 该数字与 review 20260909 记载的基线一致。
- 唯一 warning：`test_api.py:345` 的 Starlette/httpx `TestClient` deprecation，与本次工作无关。

## 2. Git 工作流护栏基线

- **无任何 CI 配置**：仓库不存在 `.github/workflows/`。
- `.pre-commit-config.yaml` 仅有 3 个 local hook：
  1. `ruff check harness tests scripts`
  2. `pytest tests/test_bug_summary.py -q`
  3. 全量 `pytest`
- 无 mypy 钩子；前端无 tsc / build / vitest 钩子。

## 3. 类型检查豁免清单（mypy，阶段零显式记录）

mypy 在阶段零之前**从未进入任何门禁**。清掉 `.mypy_cache` 冷跑
`uv run mypy harness` 实测 **122 errors / 23 files**（历史增量缓存曾只暴露 52，
属假象）。阶段零不修任何一个，只做显式、可移除即红的记录，让 CI 从第一天就是绿的。
豁免全部位于 `pyproject.toml [tool.mypy.overrides]`。

### 3.1 历史宽豁免块（阶段零之前已存在，逐字保留，6 个错误码）

关闭码：`arg-type, assignment, attr-defined, call-arg, no-redef, union-attr`。

| 模块 | 备注 |
|---|---|
| `harness.core.dag_executor` | 冷跑错误最多（含 22 个 union-attr） |
| `harness.tools.executor` | |
| `harness.monitoring.run_monitor` | |
| `harness.core.scheduler.base` | |
| `harness.tools.mcp_manager` | |
| `harness.api.deps` | |
| `harness.api.analysis_routes` | |
| `harness.api.app` | |
| `harness.api.serve` | |

`harness.api.routes` 与 `harness.tools.mcp_call` 原在本块内；因还各自需要一个
额外错误码（`misc`），已拆成独立 override 块（同一模块在两个 override 中给出
冲突的 `disable_error_code` 会导致 mypy **作废全部 overrides**，已实测踩中并修正）。

### 3.2 阶段零新记录的窄豁免（文件级 49 errors / 12 files，按 文件 × 错误码 最小化）

| 模块 | 关闭码 | 冷跑数量 |
|---|---|---|
| `harness.api.replay_routes` | arg-type | 1 |
| `harness.core.lifecycle` | arg-type | 1（`max(key=...)` 重载类型） |
| `harness.core.scheduler.plan` | arg-type | 1（dict 不变性，建议 Mapping） |
| `harness.core.scheduler.classify` | attr-defined | 9（mixin 动态属性） |
| `harness.core.scheduler.local_repair` | attr-defined | 10（mixin 动态属性） |
| `harness.core.scheduler.revision_guard` | attr-defined | 6（mixin 动态属性 `planner`/`config`） |
| `harness.monitoring.langfuse_tracer` | assignment | 1 |
| `harness.tools.browser_mcp` | misc | 7 |
| `harness.tools.fetch_output` | misc | 1 |
| `harness.tools.skill` | misc | 6 |
| `harness.tools.mcp_call` | 历史 6 码 + misc | 5（misc） |
| `harness.api.routes` | 历史 6 码 + misc | 1（misc） |

收敛规则：**修掉一个底层错误就必须删掉对应窄豁免并让 mypy 仍为绿**；
禁止借无关改动新增豁免。移除任一窄豁免的变异自检已验证（mypy 立即变红）。

> 原列于此的 2 个内核模块（`revision_invariants` 2 处 arg-type、`event_store`
> 1 处 index）经 §3.3 分诊确认为假阳性，已改用**行内 `# type: ignore[code]` +
> 原因注释**，从文件级豁免块移除（豁免面从 14 files 收窄到 12 files）。

### 3.3 内核 mypy 分诊结论（2026-09-09，3 处全部判定假阳性，无真实 bug）

- `revision_invariants.py:51,76` — `RequiredOperation.step_satisfies(step, contract)`
  把 `DeliveryContract` 传给声明 `RequiredOperation` 的形参。
  **分诊**：`step_satisfies` 仅读取 `.tool` / `.input`（plan.py:77-90），
  `DeliveryContract` 在 C-01 收敛后是含这两个字段的受信超集（另带 contract_id/source），
  运行时不可能触发属性错误。**结论：注解过窄的假阳性**，非空值/逻辑路径问题。
  处置：行内 `# type: ignore[arg-type]` + 注释；根治（给 step_satisfies 的 req
  引入 `Protocol`/`Union[RequiredOperation, DeliveryContract]`）归入 mypy 清理任务，
  根治时不得改变 DeliveryContract 覆盖判定语义。
- `storage/event_store.py:417` — `Row | None` 未判空即 `row[0]`。
  **分诊**：该语句是 seq 分配的**写路径**（非读路径），SQL 为无 GROUP BY 的标量
  聚合 `SELECT COALESCE(MAX(seq),0)+1 ...`，标量聚合即使在空表上也**保证恰好返回
  一行**，故 `fetchone()` 不可能为 None，无真实可触发的 NoneType 路径。
  **结论：mypy 不理解 SQL 聚合行数语义的假阳性**，不是潜伏 bug。
  处置：行内 `# type: ignore[index]` + 注释；不改动任何写路径/触发器逻辑。
- 两处行内豁免均以 `mypy --warn-unused-ignores` 单独验证：无 "unused ignore"，
  证明错误真实存在且豁免必要（非多余压制）。


## 4. 内核契约基线（本次工作禁止改动的锚点）

- append-only：`storage/event_store.py` 的 `trg_prevent_update_events` /
  `trg_prevent_delete_events` 物理触发器 + `(run_id, event_type, idempotency_key)` 唯一索引。
- 唯一投影：`core/fold.py:158` `fold_events(events) -> RunState`（纯函数、确定性）。
- 证据豁免：`step_evidence` 不被任何压缩事件（EpisodeArchived / ContextPruned）裁剪。
- 完成门 fail-safe：空契约 → `deliverable_status="unverified"`，永不 `met`
  （`core/scheduler/completion.py`）。
- 受信分类器未知输入方向（Q-06 对齐后）：`step_is_mutating` 未知工具 → True（mutating）；
  `is_read_only_action` 未知工具 → False（非只读）。两者同为 fail-closed。
- 特别注意：`core/planner/revision_invariants.py` 与 `core/scheduler/revision_guard.py`
  虽位于 planner 路径，执行的是 DeliveryContract 在"修订"动作上的内核级强制
  （弱化用户硬性交付），合并重复实现时按内核谨慎度对待。

## 5. 事件面基线（R10）

- `len(EventType) == 41`，`len(PAYLOAD_MODEL_MAP) == 41`（运行时一致，但无任何断言强制）。
- `fold_events` 的 `match` 无 default，未覆盖的新事件类型被静默忽略。
- README 已漂移：标注"38 种"，实际 41 种（记录在案，按独立任务处理，不在阶段零修）。

## 6. 已知但本阶段不处理的事项

- 所有 R1–R12 观察项以 review 20260909 为准，阶段一只处理其排定的重复实现合并。
- mypy 存量错误（§3）只显式记录，不在阶段零修复。
- README 事件类型"38 种 vs 实际 41 种"的计数漂移（R10）不在阶段零修复，按独立任务处理。

## 7. 受信纯函数边界覆盖审计（阶段零第 5 项）

阶段零逐条核对"受信判定函数"的**未知输入 fail 方向**是否有测试 pin。
阶段一合并重复实现（R7/R8）时，必须以此表对账，不得反转任一方向。

| 受信判定函数 | 未知输入方向 | 覆盖位置 | 状态 |
|---|---|---|---|
| `PlanGuardrail.validate`（工具存在性，R7 #2） | **fail-closed**（errors 非空即拒，:51-53 短路） | test_planner.py:135；**新增** test_plan_guardrail_structure.py 方向 pin ×2 | 已补强 |
| `step_is_mutating`（revision_invariants） | **fail-closed**（未知→True=mutating） | test_reviser_restriction.py:159,171,179 | 已覆盖 |
| `is_read_only_action` / `_is_read_only_action`（recovery） | **fail-closed**（未知→False=非只读） | test_tool_semantic_retry.py:102 | 已覆盖 |
| `validate_local_repair`（预算/白名单/只读） | **fail-closed**（超预算/非白名单/mutating/空提案全拒） | test_recovery_core.py:178-211 | 已覆盖 |
| `verify_deliverables` / `CompletionVerdict.compute` | 空契约→**unverified**（非 met，fail-safe 不假绿）；不匹配→unmet；UNSUCCESSFUL→unmet | test_deliverable_gate.py:42-124；test_completion_gate.py:320-352 | 已覆盖 |
| `validate_revision_invariants`（内核：弱化交付） | 未知工具未被契约覆盖→拒绝；无契约跳过反向覆盖（legacy unverified） | test_reviser_restriction.py:58-197 | 已覆盖 |
| `ToolWhitelistGuardrail`（R7 #5，workspace 白名单） | **fail-open**（scope 未声明 `allowed_tools`，即 None → 放行；声明后 fail-closed） | test_browser_policy.py:166-189；test_tool_guardrail_contract.py:77 | 已覆盖（**注意：这是唯一现存的 fail-open 点，属存量 workspace 语义，禁止顺手反转**） |
| 注册期契约自洽（R7 #1） | **fail-closed**（拒绝注册/启动） | test_tool_registry.py:137 起 | 已覆盖 |
| 执行时双查 `get_tool_def/get_tool_fn`（R7 #4） | **fail-closed**（StepResult FAILED） | test_dag_executor.py 未知工具用例 | 已覆盖 |

阶段零补的两条用例只 pin 方向、不改任何生产代码。

