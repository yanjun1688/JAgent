"""R1/R2 (2026-09-12): planner failure fails the run honestly — no serial fallback.

Deleting the planner-failure fallback to ``AgentLoopScheduler`` removes the
second weak completion path (R1) and the nested-scheduler seam (R2). When the
planner cannot produce a valid plan, the trusted scheduler must write a
structured ``RunFailed`` instead of silently switching execution models.
"""

from __future__ import annotations

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_planner_failure_fails_run_without_serial_fallback():
    store = EventStore(":memory:")
    await store.initialize()
    try:
        executor = ToolExecutor(store)
        registry = ToolRegistry()
        # Empty responses → MockLLMClient returns a stop marker for every call,
        # which is not parseable as a plan → plan() exhausts retries and returns None.
        planner = Planner(MockLLMClient(responses=[]), registry, store, max_plan_retries=1)
        dag = DagExecutor(executor, store, registry)
        sched = PlanningExecutorScheduler(
            store,
            executor,
            planner,
            dag,
            [],
            {},
            config=SchedulerConfig(run_attempt_budget=5),
        )

        state = await sched.run("planner_fail", "do something")

        events = await store.get_events("planner_fail")
        assert any((e.event_type == EventType.RUN and e.phase == "failed") for e in events)
        assert not any((e.event_type == EventType.RUN and e.phase == "completed") for e in events)
        assert state.status.value == "failed"
        failed = next(e for e in events if (e.event_type == EventType.RUN and e.phase == "failed"))
        assert "Planner failed to produce a valid plan" in (failed.payload.get("final_error") or "")
    finally:
        await store.close()
