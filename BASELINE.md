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

## 3. 类型检查豁免基线（mypy）

`pyproject.toml` 对以下 **11 个模块**整体关闭 6 类错误
（`arg-type, assignment, attr-defined, call-arg, no-redef, union-attr`）：

1. `harness.core.dag_executor`
2. `harness.tools.executor`
3. `harness.monitoring.run_monitor`
4. `harness.core.scheduler.base`
5. `harness.tools.mcp_manager`
6. `harness.tools.mcp_call`
7. `harness.api.deps`
8. `harness.api.analysis_routes`
9. `harness.api.routes`
10. `harness.api.app`
11. `harness.api.serve`

本阶段只记录、不要求修复。阶段零另有显式清单文档跟踪。

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
