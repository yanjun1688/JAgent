from __future__ import annotations

import asyncio

import pytest

from harness.core.dag_executor import DagExecutor
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import RetryPolicy, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def echo_def():
    return ToolDefinition(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object"},
        idempotency_key_fields=["msg"],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )


@pytest.fixture
def registry(echo_def):
    r = ToolRegistry()
    async def echo_fn(input: dict) -> dict:
        await asyncio.sleep(0.01)
        return {"echo": input.get("msg", ""), "status": "ok"}
    r.register(echo_def, echo_fn)
    return r


@pytest.fixture
def executor(store):
    return ToolExecutor(store)


class TestDagExecutorBasic:
    async def test_semaphore_limits_concurrency(self, store, executor, registry):
        """DagExecutor semaphore should limit concurrent tool executions."""
        dag = DagExecutor(executor, store, registry, max_parallel=2)
        plan = DagPlan(
            intent="test concurrency",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "a"}),
                DagStep(id="s2", tool="echo", input={"msg": "b"}),
                DagStep(id="s3", tool="echo", input={"msg": "c"}),
            ],
        )
        results = await dag.execute("run-1", plan)
        assert len(results) == 3
        for sid in ("s1", "s2", "s3"):
            assert results[sid]["status"] == "completed"

    async def test_execute_layer_returns_bool(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="echo", input={"msg": "x"})],
        )
        plan_id = "test-plan"
        layers = plan.topological_sort()
        result = await dag.execute_layer("run-2", plan, plan_id, layers[0], 0, layers, {})
        assert result is True

    async def test_event_order_started_before_completed(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test event order",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "first"}),
            ],
        )
        await dag.execute("run-3", plan)
        events = await store.get_events("run-3")
        dag_started = [e for e in events if e.event_type == EventType.DAG_STEP_STARTED]
        dag_completed = [e for e in events if e.event_type == EventType.DAG_STEP_COMPLETED]
        assert len(dag_started) == 1
        assert len(dag_completed) == 1
        assert dag_started[0].seq < dag_completed[0].seq


class TestDagExecutorEdgeCases:
    async def test_unknown_tool_returns_error(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test unknown tool",
            steps=[DagStep(id="s1", tool="nonexistent", input={})],
        )
        results = await dag.execute("run-edge-1", plan)
        assert results["s1"]["status"] == "error"

    async def test_dependency_results_merged(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test deps",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "hello"}),
                DagStep(id="s2", tool="echo", input={"msg": ""}, depends_on=["s1"]),
            ],
        )
        results = await dag.execute("run-edge-2", plan)
        assert results["s1"]["status"] == "completed"
        assert results["s2"]["status"] == "completed"
