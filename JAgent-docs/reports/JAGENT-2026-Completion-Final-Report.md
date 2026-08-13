# JAGENT-2026 Completion Follow-up Report

> 日期：2026-08-13
> 范围：审查反馈后的 P0/P1/H/M/P2-1 修复 + ADR-009 Q-01~Q-06 落地
> 说明：`DeliveryContract.after` 已按 ADR-009 Q-05 删除，时序归 `DagStep.depends_on`（Q-03）。
> 历史评审 `JAgent-docs/Reviews/completion_alignment_followup_20260813.md` 记录的 L-01 已由 Q-03/Q-05 替代关闭。

## 结论

本轮修复已关闭空 Plan fake-green、非法 caller 契约、Extractor 粗校验、Executor 入口绕过、合并后 Plan 未校验、Reviser 空计划丢约束、阶段任务未追踪、step ID 引用不一致和 reload 文档偏差。

内容级匹配与时序匹配仍不是当前生产保证：内容匹配继续遵循 D-03/L-02；`after` 继续遵循 L-01，未伪称已实现。

本轮第一批审查修复另关闭了确认伪造、EventStore 返回 seq 竞态、feedback 相对过期计算、run_code 子字符串白名单、外部取消回收、file_op 无 backend fallback、Conversation 契约绕过和 API response schema 缺口。

## 修复证据

| 问题 | 修复位置 | 回归证据 |
|---|---|---|
| 空 Plan + DeliveryContract 假绿 | `harness/core/scheduler/plan.py:531` | `tests/test_scheduler.py:970` `test_initial_empty_plan_with_delivery_contract_fails` |
| caller 契约未知工具/缺字段 | `harness/models/intent.py:44`、`harness/api/routes.py:247` | `tests/test_api_contract_submission.py:198` `test_unknown_tool_contract_rejected`、`:215` `test_missing_operation_key_contract_rejected` |
| Extractor 工具级校验 | `harness/core/contract_extractor.py:60` | `tests/test_api_contract_submission.py:129` `test_extraction_invalid_items_dropped`、`tests/test_intent_contract.py:31` `test_delivery_contract_validation_is_tool_specific` |
| RunStarted 前抽取阻塞 | `harness/api/routes.py:285`、`harness/models/events.py:15,74`、`harness/core/fold.py:145` | `tests/test_api_contract_submission.py::test_extraction_fallback_source_extracted`；新增 `DeliveryContractsResolved` 事件，抽取有 15 秒上限 |
| Executor 入口绕过 Guardrail | `harness/core/dag_executor.py:95` | `tests/test_dag_executor.py:137` `TestDagExecutorEdgeCases.test_unknown_tool_returns_error` |
| Reviser 空计划保留约束 | `harness/core/planner.py:647` | `tests/test_reviser_restriction.py:54-142`；空修订继续由完成门判定 |
| 合并后重新 Guardrail | `harness/core/scheduler/plan.py:744,956` | `tests/test_scheduler.py::test_layer_fails_revise_continues`、`test_revision_restores_skipped_downstream_and_global_completion_gate` |
| 阶段 Task 注册/取消回收 | `harness/core/scheduler/base.py:484,504` | `tests/test_lifecycle_cancellation.py:78`、`:110` |
| 连字符 step ID 引用 | `harness/core/planner.py:128`、`harness/core/dag_vars.py:142` | `tests/test_dag_executor.py:145` `test_hyphenated_step_reference_resolves` |
| 工具日志摘要/状态 | `harness/tools/executor.py:42,166` | 日志实现含 input/output bounded summary、status、duration，并脱敏 Authorization/Cookie/token/api_key |
| 确认接口受信校验 | `harness/api/routes.py:498` | 必须是当前 PAUSED Run 的 pending confirmation；未知 ID 返回结构化 409 |
| EventStore 返回实际 seq | `harness/storage/event_store.py:400` | 写事务内分配并保存 seq，避免 append 后重新读取最新 seq |
| feedback 相对过期 | `harness/api/routes.py:597` | `expires_at_seq=current_seq+expires_in_seqs` |
| run_code 白名单 | `harness/tools/guardrails.py:118` | argv 首项精确匹配并拒绝 shell operator |
| file_op fail-closed | `harness/tools/file_op.py:161` | 无 backend 默认抛出受信错误，legacy fallback 仅显式 test opt-in |
| Conversation 契约/幂等 | `harness/models/conversation.py:42`、`harness/api/routes.py:718` | Conversation caller 契约复用统一校验和首次 Run 事件 |
| API response schema | `harness/api/routes.py:268,435,468,498,564,633` | Run/control/confirm/feedback/delete 均声明 Pydantic response model |
| content 排除匹配 | `harness/models/plan.py:47` | `tests/test_deliverable_gate.py::test_content_is_stored_but_not_a_delivery_match_key` |
| README reload 命令 | `README.md:260` | 使用 `python -m harness.api.serve`，由 `serve.py:233` 限定源码目录 |

## 生命周期设计

caller 契约在 `RunStarted` 中写入。无 caller 契约时，先写空 contracts 的 `RunStarted`，再执行有界 Extractor，并追加 `DeliveryContractsResolved`。抽取超时或异常会写结构化 resolved 事件并使用空契约的 `unverified` 语义，不会让请求在无 Run 记录状态下无限等待。

阶段调用显式创建并注册 `run_id:phase` Task。取消时先 cancel、宽限期 await，超时记录 `TASK_CLEANUP_TIMEOUT`，再次 cancel 并有限等待；无法合作的任务不得阻塞 watchdog，但会被结构化记录为清理超时。

## 未实现项及后果

### 内容匹配（L-02）

仍只验证工具、操作和路径等结构化输入。错误写入内容仍可能通过交付门。实现内容级验证前必须定义工具范围、变量解析后值、append 语义、编码/换行和输出验证规则。

### 时序依赖（L-01 已由 Q-03/Q-05 关闭）

`DeliveryContract.after` 字段已按 ADR-009 Q-05 删除，契约不再承载时序职责；执行顺序/数据依赖唯一由 `DagStep.depends_on` 表达（Q-03）。通用外部工具、HTTP、数据库和多交付物场景仍可能出现错误顺序或并发——这是 `depends_on` 表达能力的固有边界，不属于契约字段。

## 架构收敛（ADR-009）

质量门禁与执行依赖分离设计已固化于 `JAgent-docs/Prd/ADR-009_质量门禁与执行依赖分离设计.md`：

- Q-01 DeliveryContract = 用户要求 + 最终验收（已确认）
- Q-02 `required_operations` → `declared_operations`（LLM 自检，已实施）
- Q-03 唯一执行依赖 = `DagStep.depends_on`（已确认）
- Q-04 完成门只信 DeliveryContract + StepResult（已确认）
- Q-05 `DeliveryContract.after` 删除（已实施）
- Q-06 mutating 覆盖只认 DeliveryContract（已实施）
- Q-07 **总计时器（已实现）**：移除 `phase_timeout_ms`，`run_timeout_ms` 为唯一总预算，各阶段/等待共享剩余时间
- Q-08 **无需确认过期（已实现）**：不引入 TTL，确认等待纳入总预算

本轮 clean-run 结果：**1109 passed, 2 skipped, 1 warning in 51.92s**（新增总计时器行为测试 `test_run_budget_caps_confirmation_wait`）。`ruff check`：**All checks passed**。

## 测试证据

最终验收使用不写 pytest 缓存的 clean-run 命令：

```text
python -m pytest -q -p no:cacheprovider
```

本轮 clean-run 结果：**1108 passed, 2 skipped, 1 warning in 48.47s**。`ruff check`：**All checks passed**。file_op self-heal 测试通过显式 `LocalDirectoryBackend` 验证真实 backend 注入链，不使用隐式 CWD fallback。此前 `.pytest_cache/v/cache/lastfailed` 的 38 条记录属于不可作为 clean-run 证据的历史缓存。

---

## 追加：ADR-009 Q-01~Q-06 落地（质量门禁与执行依赖分离）

> 会话：quality_gate_dependency_split（独立任务会话）
> 日期：2026-08-13
> 依据：`JAgent-docs/Handover/quality_gate_dependency_split_handover_20260813.md`；上游 Q-07/Q-08 已实现内容未改动。

### 本任务改动清单

| 决策 | 改动 |
|---|---|
| Q-01 | 核查符合现状，ADR-009 标注"已确认"，无代码改动 |
| Q-02 | `DagPlan.required_operations` → `declared_operations`（模型/解析/guardrail 自洽检查/完成门机械维度/测试全同步）；`RequiredOperation` 类名保留并注释为"LLM 自检声明" |
| Q-03 | 核查符合现状，ADR-009 标注"已确认"，无代码改动 |
| Q-04 | 核查 `verify_deliverables`/`CompletionVerdict.compute` 交付维度只信契约，ADR-009 标注"已确认"，无代码改动 |
| Q-05 | 删除 `DeliveryContract.after` 字段；历史含 `after` 的事件流回放测试通过（Pydantic 忽略未知字段，无迁移） |
| Q-06 | `validate_revision_invariants` 反向覆盖只认 `root_contracts`，删除 `declared_operations` 自报授权分支（堵 self-authorize）；无契约 legacy 运行保持跳过反向覆盖 |

### 涉及文件

- `harness/models/plan.py`（字段改名 + 注释）
- `harness/core/planner.py`（prompt schema / `_parse_plan` / guardrail 自洽检查 / revise 空计划 / `validate_revision_invariants`）
- `harness/core/scheduler/plan.py`（完成门机械维度 / 空计划判断 / 注释）
- `harness/models/intent.py`（删除 `after` 字段）
- 测试：`test_intent_contract.py`、`test_planner.py`、`test_completion_gate.py`、`test_reviser_restriction.py`、`test_scheduler.py`、`test_deliverable_gate.py`

### 新增测试

- `test_parse_plan_declared_operations` / `test_parse_plan_declared_operations_invalid_items_skipped`（Planner 解析）
- `test_validate_declared_operations_self_consistency_rejected` / `test_validate_declared_operations_matching_passes`（guardrail 自洽检查）
- `test_fold_replays_historical_contract_with_after_field`（Q-05 历史回放）
- `test_delivery_contract_after_field_removed`（Q-05 after 已移除）
- `test_mutating_step_not_authorized_by_declared_operations` / `test_mutating_step_covered_by_contract_passes`（Q-06 只认契约）

### 范围说明

- L6/L7（API/前端）未触碰：`CreateRunRequest.required_operations` 是 caller 契约输入（方案 A），语义与 LLM 自检无关，保持原名。
- `contract_extractor.py` 的 `required_operations` JSON key 属抽取器独立 schema（产出 DeliveryContract），不在 Q-02 改名范围。
- 完成门 deliverable 维度仍只由 DeliveryContract 驱动；`declared_operations` 仅停留机械维度。

### 验证结果

针对性测试全绿；全量 clean-run 与 ruff 结果见下。

```text
python -m pytest -q -p no:cacheprovider
python -m ruff check harness tests scripts
git diff --check
```
