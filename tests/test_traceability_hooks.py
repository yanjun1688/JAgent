"""v2.2 (Phase C) tests — 可溯源挂钩 (D6): step_id↔tool_call_id JOIN + 计划结构落事件.

Covers 洞 1 (工具事件带 step_id，DAG 事件带 tool_call_id) and 洞 2
(PlanCreated/PlanRevised 携带完整 steps 结构，可从事件流重建 DAG 蓝图)。
"""

from __future__ import annotations

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.http_request import HTTP_REQUEST_DEF
from harness.tools.registry import ToolRegistry

_OK_TOOL_FN = lambda i: {"status_code": 200, "headers": {}, "body": '{"ok":1}', "elapsed_ms": 10}  # noqa: E731


@pytest.fixture
async def trace_store():
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _run_plan(plan: DagPlan, store: EventStore) -> dict[str, ExecState]:
    executor = ToolExecutor(store=store)
    registry = ToolRegistry()
    registry._register(HTTP_REQUEST_DEF, _OK_TOOL_FN)
    dag = DagExecutor(executor=executor, store=store, registry=registry, max_parallel=1)
    results = await dag.execute(run_id="run-trace", plan=plan)
    return results


# ── 洞 1: step_id ↔ tool_call_id 双向 JOIN ────────────────────────


@pytest.mark.asyncio
async def test_tool_events_carry_step_id(trace_store):
    """TOOL_CALLED / TOOL_COMPLETED 携带 step_id → 可反查某 step 的工具调用。"""
    plan = DagPlan(
        intent="trace",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}),
        ],
    )
    await _run_plan(plan, trace_store)
    events = await trace_store.get_events("run-trace")

    called = [e for e in events if e.event_type == EventType.TOOL_CALLED]
    completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
    assert len(called) == 2 and len(completed) == 2

    step_ids = sorted(e.payload["step_id"] for e in called)
    assert step_ids == ["s1", "s2"]
    assert all(e.payload["step_id"] for e in completed)


@pytest.mark.asyncio
async def test_dag_step_events_carry_tool_call_id(trace_store):
    """DAG_STEP_COMPLETED 携带 tool_call_id → 可反查该 step 用哪个工具调用。"""
    plan = DagPlan(
        intent="trace",
        steps=[DagStep(id="s1", tool="http_request", input={"url": "http://a"})],
    )
    await _run_plan(plan, trace_store)
    events = await trace_store.get_events("run-trace")

    dag_done = next(e for e in events if e.event_type == EventType.DAG_STEP_COMPLETED)
    assert dag_done.payload["step_id"] == "s1"
    assert dag_done.payload["tool_call_id"], "DAG_STEP_COMPLETED 必须携带 tool_call_id"

    called = next(e for e in events if e.event_type == EventType.TOOL_CALLED)
    # JOIN 关键：同一 step 的 DAG 事件与工具事件共享 tool_call_id
    assert dag_done.payload["tool_call_id"] == called.payload["tool_call_id"]
    assert called.payload["step_id"] == "s1"


@pytest.mark.asyncio
async def test_join_step_to_tool_call_reconstructs_params(trace_store):
    """从事件流重建: step s1 → tool_call_id → TOOL_CALLED 的 input。"""
    plan = DagPlan(
        intent="trace",
        steps=[DagStep(id="s1", tool="http_request", input={"url": "http://example.com/x"})],
    )
    await _run_plan(plan, trace_store)
    events = await trace_store.get_events("run-trace")

    dag_done = next(e for e in events if e.event_type == EventType.DAG_STEP_COMPLETED)
    tc_id = dag_done.payload["tool_call_id"]
    called = next(e for e in events if e.event_type == EventType.TOOL_CALLED and e.payload["tool_call_id"] == tc_id)
    completed = next(
        e for e in events if e.event_type == EventType.TOOL_COMPLETED and e.payload["tool_call_id"] == tc_id
    )

    assert called.payload["step_id"] == "s1"
    assert called.payload["input"]["url"] == "http://example.com/x"
    assert completed.payload["output"]["status_code"] == 200


# ── 洞 2: 计划结构落事件，可从事件流重建 DAG 蓝图 ──────────────────


@pytest.mark.asyncio
async def test_plan_created_carries_full_steps_blueprint(trace_store):
    """PlanCreated 携带每步 tool/input/depends_on/probe → 事后重建蓝图。"""
    plan = DagPlan(
        intent="blueprint",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}, description="fetch A"),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"], description="fetch B"),
        ],
    )
    await _run_plan(plan, trace_store)
    events = await trace_store.get_events("run-trace")

    created = next(e for e in events if e.event_type == EventType.PLAN_CREATED)
    steps = created.payload["steps"]
    assert len(steps) == 2
    assert steps[0]["step_id"] == "s1"
    assert steps[0]["tool_name"] == "http_request"
    assert steps[0]["input"] == {"url": "http://a"}
    assert steps[0]["depends_on"] == []
    assert steps[0]["description"] == "fetch A"
    assert steps[1]["depends_on"] == ["s1"]


@pytest.mark.asyncio
async def test_fold_reconstructs_blueprint_from_events(trace_store):
    """fold_events 后 state.plan_history 的 steps 含蓝图结构（非仅 'N steps'）。"""
    from harness.core.fold import fold_events

    plan = DagPlan(
        intent="blueprint",
        steps=[
            DagStep(id="s1", tool="http_request", input={"url": "http://a"}),
            DagStep(id="s2", tool="http_request", input={"url": "http://b"}, depends_on=["s1"]),
        ],
    )
    await _run_plan(plan, trace_store)
    events = await trace_store.get_events("run-trace")

    state = fold_events(events)
    assert state.latest_plan is not None
    blueprint = state.latest_plan["steps"]
    step_ids = {s["step_id"] for s in blueprint}
    assert step_ids == {"s1", "s2"}
    # 完成事件已把 tool_call_id 挂钩进折叠状态
    done = next(s for s in blueprint if s["step_id"] == "s1")
    assert done["tool_call_id"]


@pytest.mark.asyncio
async def test_timeout_and_guardrail_payloads_keep_step_join_key(trace_store):
    """Every pre/post-execution tool outcome remains joinable to its DAG step."""
    from harness.models.events import GuardrailTriggeredPayload, ToolTimeoutPayload

    timeout = ToolTimeoutPayload(
        tool_call_id="tc-timeout",
        tool_name="http_request",
        timeout_ms=10,
        step_id="s-timeout",
    )
    blocked = GuardrailTriggeredPayload(
        tool_call_id="tc-blocked",
        tool_name="http_request",
        guardrail_id="scope",
        reason="outside scope",
        step_id="s-blocked",
    )
    assert timeout.step_id == "s-timeout"
    assert blocked.step_id == "s-blocked"
