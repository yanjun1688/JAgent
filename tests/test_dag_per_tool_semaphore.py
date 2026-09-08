"""ADR-011 §3.5 regression tests — DagExecutor per-tool concurrency cap.

``ToolDefinition.max_parallel`` is now a *trusted* enforcement (per-tool
``asyncio.Semaphore`` inside the global one), not just a planner warning:

- two parallel steps on a ``max_parallel=1`` tool must actually serialize,
- two steps on *different* ``max_parallel=1`` tools must still overlap
  (semaphores are keyed per tool name, so unrelated tools are not throttled).

Uses probe tools only — no browser / subprocess. Cross-platform green.
"""

from __future__ import annotations

import asyncio

from harness.core.dag_executor import DagExecutor
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import RetryPolicy, ToolDefinition
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry


def _probe_def(name: str, *, max_parallel: int) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"probe {name}",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        idempotency_key_fields=None,
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
        max_parallel=max_parallel,
    )


class TestPerToolSemaphore:
    async def test_max_parallel_one_serializes_steps_on_same_tool(self, store):
        registry = ToolRegistry()
        stats = {"active": 0, "max": 0, "calls": 0}

        async def probe(input):
            stats["active"] += 1
            stats["max"] = max(stats["max"], stats["active"])
            stats["calls"] += 1
            await asyncio.sleep(0.05)
            stats["active"] -= 1
            return {"ok": True}

        registry._register(_probe_def("probe", max_parallel=1), probe)
        dag = DagExecutor(ToolExecutor(store), store, registry, max_parallel=10)
        plan = DagPlan(
            intent="serialize same tool",
            steps=[
                DagStep(id="p1", tool="probe", input={}),
                DagStep(id="p2", tool="probe", input={}),
            ],
        )

        results = await dag.execute("run-serial", plan)

        assert results["p1"].is_completed, results["p1"].error
        assert results["p2"].is_completed, results["p2"].error
        assert stats["calls"] == 2
        assert stats["max"] == 1  # never overlapped — per-tool cap enforced

    async def test_different_tools_are_not_throttled_by_each_other(self, store):
        # Two max_parallel=1 tools on distinct names must run concurrently:
        # the per-tool semaphore is keyed per tool name (global=10 permits it).
        registry = ToolRegistry()
        entered = {"a": asyncio.Event(), "b": asyncio.Event()}
        release = asyncio.Event()

        async def gate_a(input):
            entered["a"].set()
            await release.wait()
            return {"ok": True}

        async def gate_b(input):
            entered["b"].set()
            await release.wait()
            return {"ok": True}

        registry._register(_probe_def("gate_a", max_parallel=1), gate_a)
        registry._register(_probe_def("gate_b", max_parallel=1), gate_b)
        dag = DagExecutor(ToolExecutor(store), store, registry, max_parallel=10)
        plan = DagPlan(
            intent="parallel across tools",
            steps=[
                DagStep(id="a", tool="gate_a", input={}),
                DagStep(id="b", tool="gate_b", input={}),
            ],
        )

        run_task = asyncio.create_task(dag.execute("run-par", plan))
        both_entered = False
        try:
            await asyncio.wait_for(asyncio.gather(entered["a"].wait(), entered["b"].wait()), timeout=5)
            both_entered = True
        except asyncio.TimeoutError:
            pass
        finally:
            release.set()
        results = await asyncio.wait_for(run_task, timeout=10)

        assert both_entered, "gate_a/gate_b did not run concurrently (per-tool semaphore not per-name?)"
        assert results["a"].is_completed, results["a"].error
        assert results["b"].is_completed, results["b"].error
