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

### 3.2 阶段零新记录的窄豁免（52 errors / 14 files，按 文件 × 错误码 最小化）

| 模块 | 关闭码 | 冷跑数量 |
|---|---|---|
| `harness.api.replay_routes` | arg-type | 1 |
| `harness.core.lifecycle` | arg-type | 1（`max(key=...)` 重载类型） |
| `harness.core.planner.revision_invariants` | arg-type | 2（**内核相关**：`DeliveryContract` 传给声明 `RequiredOperation` 的参数，结构化兼容、运行时正常） |
| `harness.core.scheduler.plan` | arg-type | 1（dict 不变性，建议 Mapping） |
| `harness.core.scheduler.classify` | attr-defined | 9（mixin 动态属性） |
| `harness.core.scheduler.local_repair` | attr-defined | 10（mixin 动态属性） |
| `harness.core.scheduler.revision_guard` | attr-defined | 6（mixin 动态属性 `planner`/`config`） |
| `harness.monitoring.langfuse_tracer` | assignment | 1 |
| `harness.storage.event_store` | index | 1（**内核相关**：`Row | None` 未判空，`:417`） |
| `harness.tools.browser_mcp` | misc | 7 |
| `harness.tools.fetch_output` | misc | 1 |
| `harness.tools.skill` | misc | 6 |
| `harness.tools.mcp_call` | 历史 6 码 + misc | 5（misc） |
| `harness.api.routes` | 历史 6 码 + misc | 1（misc） |

收敛规则：**修掉一个底层错误就必须删掉对应窄豁免并让 mypy 仍为绿**；
禁止借无关改动新增豁免。移除任一窄豁免的变异自检已验证（mypy 立即变红）。

### 3.3 内核相关的两条类型债（合并阶段需额外留意，勿顺手改语义）

- `revision_invariants.py:51,76` — `RequiredOperation.step_satisfies(step, contract)`
  形参类型问题；改类型声明时不得改变 DeliveryContract 覆盖判定语义。
- `storage/event_store.py:417` — `Row | None` 可索引性；属读取路径类型债，
  与 append-only 触发器无关，修复时不得动写路径。


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
- PlanGuardrail 未知工具分支此前无专门测试用例（阶段零第 5 项补）。
