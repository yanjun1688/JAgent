"""v2.2 (Phase B) tests — step_normal 机械口径 + 下游门控 + SKIPPED 记录.

Covers D3 (step_normal pure function), D8 (output_available narrowing),
D7/D9 (gate condition unique step_normal; SKIPPED recorded via DagStepSkipped),
and P0-03 (dependency health check blocks downstream).

Constraint 4: step_normal / gate never read task_state.
"""

from __future__ import annotations

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState, StepResult, TaskState
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.http_request import HTTP_REQUEST_DEF
from harness.tools.registry import ToolRegistry

_ALL_TASK_STATES = [s for s in TaskState]

_UNSUCCESSFUL_TOOL_FN = lambda i: {"status_code": 429, "headers": {}, "body": "", "elapsed_ms": 10}  # noqa: E731
_OK_TOOL_FN = lambda i: {"status_code": 200, "headers": {}, "body": '{"ok":1}', "elapsed_ms": 10}  # noqa: E731


# ── 1. step_normal 纯函数全分支（D3）────────────────────────────────


@pytest.mark.parametrize(
    "exec_state,probe,expected",
    [
        (ExecState.COMPLETED, False, True),
        (ExecState.IDEMPOTENT, False, True),
        (ExecState.UNSUCCESSFUL, False, False),
        (ExecState.UNSUCCESSFUL, True, True),
        (ExecState.FAILED, False, False),
        (ExecState.SKIPPED, False, False),
        (ExecState.PENDING, False, False),
        (ExecState.RUNNING, False, False),
        (ExecState.CANCELLED, False, False),
    ],
)
def test_step_normal_all_branches(exec_state, probe, expected):
    sr = StepResult(step_id="s1", exec_state=exec_state, probe=probe)
    assert sr.step_normal is expected


@pytest.mark.parametrize("task_state", _ALL_TASK_STATES)
def test_step_normal_ignores_task_state(task_state):
    """Constraint 4: step_normal is (exec_state, probe) → bool, no task_state read."""
    sr = StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL, task_state=task_state, probe=False)
    assert sr.step_normal is False
    sr2 = StepResult(step_id="s2", exec_state=ExecState.UNSUCCESSFUL, task_state=task_state, probe=True)
    assert sr2.step_normal is True


def test_step_normal_unsuccessful_probe_is_normal():
    """D4/D7: probe 探测型步骤的否定答案（UNSUCCESSFUL）算 normal。"""
    sr = StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL, probe=True)
    assert sr.step_normal is True


# ── 2. output_available 与 step_normal 正交（D8）─────────────────────


def test_output_available_is_data_availability_not_normal():
    """output_available (含 UNSUCCESSFUL，不含 SKIPPED) 只回答"输出可用"；
    step_normal 回答"步骤正常"。二者不可互相替代。"""
    uns = StepResult(step_id="s1", exec_state=ExecState.UNSUCCESSFUL)
    assert uns.output_available is True
    assert uns.step_normal is False

    skipped = StepResult(step_id="s2", exec_state=ExecState.SKIPPED)
    assert skipped.output_available is False  # v2.2 (P2): SKIPPED 无产出
    assert skipped.step_normal is False


# ── 3. 下游门控：依赖非 normal → SKIP + DagStepSkipped 落事件（D7/D9/P0-03）──


@pytest.fixture
async def gate_store():
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _run_plan(plan: DagPlan, fn, store: EventStore) -> dict[str, StepResult]:
    executor = ToolExecutor(store=store)
    registry = ToolRegistry()
    registry._register(HTTP_REQUEST_DEF, fn)
    dag = DagExecutor(executor=executor, store=store, registry=registry, max_parallel=1)
    results = await dag.execute(run_id="run-gate", plan=plan)
    return results


@pytest.mark.asyncio
async def test_gate_skips_downstream_when_dep_unsuccessful(gate_store):
    """P0-03: s1 UNSUCCESSFUL → s2 (depends on s1) SKIPPED，不携带坏数据。"""
    plan = DagPlan(
        intent="gate",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"]),
        ],
    )
    results = await _run_plan(plan, _UNSUCCESSFUL_TOOL_FN, gate_store)
    assert results["s1"].exec_state == ExecState.UNSUCCESSFUL
    assert results["s1"].step_normal is False
    assert results["s2"].exec_state == ExecState.SKIPPED
    assert results["s2"].step_normal is False

    events = await gate_store.get_events("run-gate")
    assert any(e.event_type == EventType.DAG_STEP_SKIPPED for e in events), (
        "D9: gate-produced SKIPPED must be recorded as DagStepSkipped"
    )
    skipped = next(e for e in events if e.event_type == EventType.DAG_STEP_SKIPPED)
    assert skipped.payload["step_id"] == "s2"
    assert "dep 's1' not normal" in skipped.payload["reason"]


@pytest.mark.asyncio
async def test_gate_skips_chain_of_dependents(gate_store):
    """s1 UNSUCCESSFUL → s2 SKIPPED → s3 (depends s2) also SKIPPED. No partial carry."""
    plan = DagPlan(
        intent="gate-chain",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"]),
            DagStep(id="s3", tool="http_request", input={"url": "http://c"}, depends_on=["s2"]),
        ],
    )
    results = await _run_plan(plan, _UNSUCCESSFUL_TOOL_FN, gate_store)
    assert results["s2"].exec_state == ExecState.SKIPPED
    assert results["s3"].exec_state == ExecState.SKIPPED

    events = await gate_store.get_events("run-gate")
    skipped_ids = [e.payload["step_id"] for e in events if e.event_type == EventType.DAG_STEP_SKIPPED]
    assert skipped_ids == ["s2", "s3"]


@pytest.mark.asyncio
async def test_gate_does_not_skip_when_dep_normal(gate_store):
    """依赖 normal（COMPLETED）→ 下游正常执行。"""
    plan = DagPlan(
        intent="gate-ok",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"]),
        ],
    )
    results = await _run_plan(plan, _OK_TOOL_FN, gate_store)
    assert results["s1"].exec_state == ExecState.COMPLETED
    assert results["s2"].exec_state == ExecState.COMPLETED

    events = await gate_store.get_events("run-gate")
    assert not any(e.event_type == EventType.DAG_STEP_SKIPPED for e in events)


@pytest.mark.asyncio
async def test_gate_does_not_skip_when_dep_probe_unsuccessful(gate_store):
    """D7: probe 步骤 UNSUCCESSFUL 算 normal → 下游照常执行。"""
    plan = DagPlan(
        intent="gate-probe",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}, probe=True),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"]),
        ],
    )
    results = await _run_plan(plan, _UNSUCCESSFUL_TOOL_FN, gate_store)
    assert results["s1"].exec_state == ExecState.UNSUCCESSFUL
    assert results["s1"].step_normal is True  # probe: "没有"就是正确答案
    # D7: probe 否定答案不阻断下游 — s2 被实际执行（非 SKIPPED），
    # 即使 s2 自身因同一 mock 函数也返回 UNSUCCESSFUL。
    assert results["s2"].exec_state != ExecState.SKIPPED
    assert results["s2"].exec_state == ExecState.UNSUCCESSFUL

    events = await gate_store.get_events("run-gate")
    assert not any(e.event_type == EventType.DAG_STEP_SKIPPED for e in events)


@pytest.mark.asyncio
async def test_gate_completion_count_uses_step_normal(gate_store):
    """D8: 完成计数用 step_normal — UNSUCCESSFUL(非 probe) 不再被算进完成数。"""
    plan = DagPlan(
        intent="gate-count",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}),
        ],
    )

    # s1 fails semantically, s2 succeeds
    async def _mixed_fn(input):
        if input.get("url", "").endswith("a"):
            return {"status_code": 429, "headers": {}, "body": "", "elapsed_ms": 10}
        return {"status_code": 200, "headers": {}, "body": '{"ok":1}', "elapsed_ms": 10}

    results = await _run_plan(plan, _mixed_fn, gate_store)
    normal_count = sum(1 for r in results.values() if r.step_normal)
    assert normal_count == 1  # only s2 normal; s1 UNSUCCESSFUL not counted

    events = await gate_store.get_events("run-gate")
    completed = next(e for e in events if e.event_type == EventType.PLAN_COMPLETED)
    assert completed.payload["completed_steps"] == 1


# ── 4. fold 折叠：DagStepSkipped 反映到 RunState.latest_plan（D9 可观测）──


@pytest.mark.asyncio
async def test_fold_records_skipped_in_latest_plan(gate_store):
    """fold_events 后 latest_plan.steps 中 SKIPPED 步骤显示 status='skipped' + reason。"""
    from harness.core.fold import fold_events

    plan = DagPlan(
        intent="gate-fold",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"]),
        ],
    )
    await _run_plan(plan, _UNSUCCESSFUL_TOOL_FN, gate_store)
    events = await gate_store.get_events("run-gate")

    state = fold_events(events)
    assert state.latest_plan is not None
    s2 = next(s for s in state.latest_plan["steps"] if s["step_id"] == "s2")
    assert s2["status"] == "skipped"
    assert "dep 's1' not normal" in s2["reason"]

    # 无 DagStepSkipped 事件时不应出现 skipped 记录
    s1 = next(s for s in state.latest_plan["steps"] if s["step_id"] == "s1")
    assert s1["status"] == "unsuccessful"
