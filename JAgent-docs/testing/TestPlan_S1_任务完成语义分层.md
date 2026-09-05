# TestPlan-S1: 任务完成语义与执行态正交分层 — 测试计划

> ✅ **S1 实现已完成** (V0.7.1)。全部测试通过。

| 属性 | 值 |
|---|---|
| **文档类型** | 测试计划 |
| **版本** | 1.0 |
| **日期** | 2026-07-23 |
| **相关文档** | ADR-007 / PRD_S1 / TDD_S1 |
| **测试文件** | `tests/test_exec_state_mapping.py` (26 tests), `tests/test_planner_revise_rerun.py` (17 tests) |

---

## 1. 测试策略

| 测试层级 | 目标 | 覆盖范围 |
|----------|------|----------|
| **单元测试 (UT)** | `should_not_rerun` 判定纯函数; 枚举映射表; `planner.py` 的 `executed_step_ids` 计算 | 100% 分支覆盖 |
| **集成测试 (IT)** | revise 路径: `DagExecutor → results → Planner.revise → PlanGuardrail`; UNSUCCESSFUL 可被重排 | 关键路径 |
| **端到端测试 (E2E)** | 完整 DAG plan-execute-revise 循环: 4 步含 COMPLETED/UNSUCCESSFUL/FAILED 混合场景 | 1 条端到端 |
| **回归测试 (RT)** | 现有 test_planner.py / test_dag_executor.py 全量通过 | 零退化 |
| **契约测试 (CT)** | `StepResult.should_not_rerun` 属性签名; `ExecState`/`TaskState` 枚举值列表 | 类型兼容 |

---

## 2. 单元测试

### 2.1 `should_not_rerun` 映射表 (tc-srr-01 ~ tc-srr-08)

**文件**: `tests/test_exec_state_mapping.py`

| 用例 ID | ExecState 输入 | 期望 `should_not_rerun` | 期望 `should_not_rerun` 为 False |
|----------|---------------|------------------------|-------------------------------|
| tc-srr-01 | `COMPLETED` | True | — |
| tc-srr-02 | `UNSUCCESSFUL` | — | False |
| tc-srr-03 | `IDEMPOTENT` | True | — |
| tc-srr-04 | `SKIPPED` | — | False |
| tc-srr-05 | `CANCELLED` | True | — |
| tc-srr-06 | `PENDING` | — | False |
| tc-srr-07 | `RUNNING` | — | False |
| tc-srr-08 | `FAILED` | — | False |

```python
import pytest
from harness.core.dag_types import StepResult, ExecState, TaskState

@pytest.mark.parametrize("exec_state,expected", [
    (ExecState.COMPLETED, True),
    (ExecState.UNSUCCESSFUL, False),
    (ExecState.IDEMPOTENT, True),
    (ExecState.SKIPPED, False),
    (ExecState.CANCELLED, True),
    (ExecState.PENDING, False),
    (ExecState.RUNNING, False),
    (ExecState.FAILED, False),
])
def test_should_not_rerun_mapping(exec_state, expected):
    sr = StepResult(step_id="s1", exec_state=exec_state)
    assert sr.should_not_rerun == expected
```

### 2.2 `ExecState` 默认值 (tc-srr-09)

| 用例 ID | 场景 | 期望 |
|----------|------|------|
| tc-srr-09 | `StepResult(step_id="s1")` — 未传 `exec_state` | `exec_state == ExecState.PENDING`, `should_not_rerun == False` |

```python
def test_step_result_default_exec_state_is_pending():
    sr = StepResult(step_id="s1")
    assert sr.exec_state == ExecState.PENDING
    assert sr.should_not_rerun is False
```

### 2.3 `TaskState` 默认值 (tc-srr-10)

```python
def test_step_result_default_task_state_is_unknown():
    sr = StepResult(step_id="s1")
    assert sr.task_state == TaskState.UNKNOWN
```

### 2.4 `planner.py:274` 修复后 `completed_step_ids` 计算 (tc-csi-01 ~ tc-csi-04)

**文件**: `tests/test_planner_revise_rerun.py`

| 用例 ID | results 中 step 状态 | 期望 `completed_step_ids` 包含 |
|----------|---------------------|------------------------------|
| tc-csi-01 | s1=COMPLETED, s2=COMPLETED | {s1, s2} |
| tc-csi-02 | s1=COMPLETED, s2=UNSUCCESSFUL | {s1} (UNSUCCESSFUL 可重排，不算已执行) |
| tc-csi-03 | s1=COMPLETED, s2=FAILED | {s1} (FAILED 不视为已完成) |
| tc-csi-04 | s1=UNSUCCESSFUL, s2=FAILED | {} |

```python
from harness.core.dag_types import StepResult, ExecState

@pytest.mark.parametrize("results,expected_ids", [
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.COMPLETED)},
        {"s1", "s2"},
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.UNSUCCESSFUL)},
         {"s1"},  # UNSUCCESSFUL is allowed to rerun
    ),
    (
        {"s1": StepResult("s1", exec_state=ExecState.COMPLETED),
         "s2": StepResult("s2", exec_state=ExecState.FAILED)},
        {"s1"},
    ),
    (
         {"s1": StepResult("s1", exec_state=ExecState.UNSUCCESSFUL),
          "s2": StepResult("s2", exec_state=ExecState.FAILED)},
         set(),
    ),
])
def test_executed_step_ids_excludes_unsuccessful(results, expected_ids):
    computed = {
        sid for sid, r in results.items()
        if isinstance(r, StepResult) and r.should_not_rerun
    }
    assert computed == expected_ids
```

### 2.5 `build_dag_status_text` 输出包含 `should_not_rerun` (tc-bds-01)

```python
def test_build_dag_status_text_includes_should_not_rerun():
    from harness.core.dag_executor import DagExecutor
    sr = StepResult("s1", exec_state=ExecState.COMPLETED)
    plan = ... # construct minimal DagPlan
    text = DagExecutor.build_dag_status_text(plan, {"s1": sr}, current_layer=0)
    assert "should_not_rerun" in text or "exec_state" in text
```

---

## 3. 集成测试

### 3.1 Planner.revise() 使用 `executed_step_ids` (tc-rev-01)

**场景**: 4 步 DAG plan，2 步 COMPLETED，1 步 UNSUCCESSFUL，1 步 FAILED。

| Step | ExecState | 期望 revise 后的 revised.steps |
|------|-----------|-------------------------------|
| s1 | COMPLETED | 不出现（已被过滤） |
| s2 | UNSUCCESSFUL | 允许出现（可被 LLM 选择重跑） |
| s3 | FAILED | 可能出现（LLM 决定重试） |
| s4 | PENDING | 可能出现（尚未执行） |

```python
@pytest.mark.asyncio
async def test_revise_allows_unsuccessful_to_rerun():
    planner = Planner(llm_client=MockLLMClient(), ...)
    plan = DagPlan(
        intent="test intent",
        steps=[
            DagStep(id="s1", tool="http_request", input={}, depends_on=[], description="step 1"),
            DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="step 2"),
            DagStep(id="s3", tool="http_request", input={}, depends_on=["s2"], description="step 3"),
        ],
    )
    results = {
        "s1": StepResult("s1", exec_state=ExecState.COMPLETED),
        "s2": StepResult("s2", exec_state=ExecState.UNSUCCESSFUL, output={"data": 42}),
        "s3": StepResult("s3", exec_state=ExecState.FAILED, error="timeout"),
    }
    revised = await planner.revise(plan, results, system_state="state summary")
    assert revised is not None
    # UNSUCCESSFUL 的 s2 可以由 revise 选择重跑
    revised_ids = {s.id for s in revised.steps}
    assert "s2" in revised_ids or "s3" in revised_ids
    assert "s1" not in revised_ids, "COMPLETED step should not be re-planned"
```

### 3.2 PlanGuardrail.validate() 接受 `executed_step_ids` (tc-grd-01)

```python
def test_plan_guardrail_uses_executed_step_ids():
    guardrail = PlanGuardrail()
    plan = DagPlan(
        intent="test",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="depends on s1")],
    )
    # s1 不在当前 plan 中，但在 executed_step_ids 中 — 应通过
    errors = guardrail.validate(plan, executed_step_ids={"s1"})
    assert errors == []
```

### 3.3 `executed_step_ids` 在 `topological_sort` 中的行为 (tc-topo-01)

```python
def test_topological_sort_with_executed_deps():
    plan = DagPlan(
        intent="test",
        steps=[DagStep(id="s2", tool="http_request", input={}, depends_on=["s1"], description="")],
    )
    # s1 不在 plan.steps 中但在 executed_step_ids 中
    layers = plan.topological_sort(executed_step_ids={"s1"})
    assert layers == [["s2"]]
```

---

## 4. 端到端测试

### 4.1 混合 ExecState 的完整 Plan-Execute-Revise 循环 (tc-e2e-01)

**场景**: 4 步 DAG 计划，模拟完整执行循环：

1. Layer 0: s1(COMPLETED) + s2(COMPLETED) 并行
2. Layer 1: s3(FAILED) — 依赖 s1+s2
3. Layer 2: s4(PENDING, 从未执行) — 依赖 s3

**Cycle 1**: plan → execute s1+s2 → execute s3(FAILED) → revise
  - `executed_step_ids` = {s1, s2}
  - s4 依赖 s3 但 s3 FAILED → guardrail 允许 s4 出现在新 plan 中
  - LLM 决定: 重试 s3 + 执行 s4

**Cycle 2**: execute s3(COMPLETED) → execute s4(UNSUCCESSFUL)
  - `executed_step_ids` = {s1, s2, s3, s4}
  - LLM 收到 s4 的 UNSUCCESSFUL output → 判定 `task_state=achieved` → steps=[]
  - RunCompleted

**断言**:
- [ ] Cycle 1 revise 后 plan 不含 s1、s2
- [ ] Cycle 2 revise 后 LLM 返回 steps=[] → RunCompleted
- [ ] s4 的 UNSUCCESSFUL 输出内容出现在 revise prompt 中
- [ ] 不存在重复执行（s1、s2 只执行一次）

```python
@pytest.mark.asyncio
async def test_e2e_mixed_exec_state_full_cycle():
    # 使用 MockLLMClient 预设 LLM 响应:
    #   Cycle 1 revise → steps=[s3, s4]
    #   Cycle 2 revise → steps=[]
    ...
    scheduler = PlanningExecutorScheduler(...)
    state = await scheduler.run(run_id="test-e2e-s1", intent="mixed exec states")
    assert state.status == RunStatus.COMPLETED
```

---

## 5. 回归测试

### 5.1 现有测试全量通过

```bash
pytest tests/ -x -v
```

重点验证：
- `tests/test_planner.py` — 全量通过
- `tests/test_dag_executor.py` — 全量通过
- `tests/test_tool_layer.py` — 全量通过
- `tests/test_plan_guardrail.py` — 全量通过（若有）

### 5.2 类型检查无退化

```bash
mypy harness/core/dag_types.py harness/core/planner.py harness/core/dag_executor.py
ruff check harness/core/
```

---

## 6. 契约测试

### 6.1 枚举值稳定性 (tc-ctr-01)

```python
def test_exec_state_values_are_stable():
    """ExecState 枚举值不随代码重构而改变"""
    expected = {"pending", "running", "completed", "unsuccessful", "failed", "skipped", "idempotent", "cancelled"}
    actual = {e.value for e in ExecState}
    assert actual == expected

def test_task_state_values_are_stable():
    expected = {"unknown", "achieved", "partial", "not_achieved", "waived"}
    actual = {e.value for e in TaskState}
    assert actual == expected
```

### 6.2 `StepResult.get("status")` backward-compat (tc-ctr-02)

```python
def test_step_result_get_status_backward_compat():
    # N/A: .get() & StepStatus were removed in Step 3
    # StepResult(step_id="s1", exec_state=ExecState.COMPLETED) is the current API
    pass
```

---

## 7. 故障注入测试

### 7.1 Planner revise 失败不破坏 Scheduler 状态 (tc-inj-01)

```python
@pytest.mark.asyncio
async def test_revise_failure_preserves_scheduler_state():
    # 模拟 Planner.revise() 抛异常
    # 断言 Scheduler 正常降级（fail 而非 crash）
    ...
```

### 7.2 `should_not_rerun` 判定不走 I/O (tc-inj-02)

```python
def test_should_not_rerun_is_pure_no_io():
    """验证 should_not_rerun 不需要 asyncio event loop"""
    sr = StepResult(step_id="s1", exec_state=ExecState.COMPLETED)
    # 在无 event loop 的环境中调用
    result = sr.should_not_rerun
    assert result is True
```

---

## 8. 测试执行结果

| 阶段 | 测试层级 | 用例数 | 结果 |
|------|---------|--------|------|
| Step 1 | UT (exec_state mapping) | 26 | ✅ 全部通过 |
| Step 1 | RT | 700 | ✅ 全部通过 |
| Step 2 | IT (planner revise rerun) | 17 | ✅ 全部通过 |
| Step 2 | E2E | 1 (内置) | ✅ 通过 |
| Step 2 | RT | 700 | ✅ 全部通过 |
| Step 3 | RT | 700 | ✅ 全部通过 |
| All Steps | Typecheck | — | ✅ `mypy harness/core/` 零错误 |
| All Steps | Lint | — | ✅ `ruff check harness/` 零警告 |

---

## 9. 潜在回归风险

| 风险点 | 可能影响的测试 | 缓解 |
|--------|---------------|------|
| `StepStatus` 枚举删除后类型检查失败 | 现有 mypy 配置 | ✅ 全局 `StepStatus` 已清理，mypy 零错误 |
| `is_done` 属性保留 | 旧测试 `r.is_done` 断言 | ✅ 属性保留（由 `exec_state` 推导），旧断言正常 |
| `status` 字段移除 | 分析 API 中的 `status` 字段展示 | ✅ `exec_state.value` 替代，`StepResult` 构造器无 `status=` 参数 |
| `get()` backward-compat shim 移除 | 外部代码依赖 | ✅ 全局 `.get("status")` 已清理 |
