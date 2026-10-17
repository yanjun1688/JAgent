"""R5 (PRD v3.5): whole-plan revalidation at the REAL execution entry.

The third layer of the "invariants → PlanGuardrail → execution entry" defense
historically lived only in ``DagExecutor.execute()``, which the production
scheduler never calls (it drives ``execute_layer()`` layer by layer). These
tests pin the revalidation at the production seam (``scheduler/plan.py`` loop
entry) so the defense cannot again exist only in tests:

* an invalid plan reaching the execution entry is rejected BEFORE any
  Plan/created, Step, or Tool event — Plan/failed (zero steps/layers) plus
  Run/failed;
* a valid plan still passes the entry check and executes.
"""

from __future__ import annotations

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner
from harness.core.scheduler.base import SchedulerConfig
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType, RunStartedPayload
from harness.models.plan import DagPlan
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        side_effects=[],
        operations=[
            {
                "operation": "read",
                "input_schema": {},
                "side_effects": [],
                "required_input": [],
            }
        ],
    )


async def _make_scheduler(store: EventStore) -> PlanningExecutorScheduler:
    executor = ToolExecutor(store)
    registry = ToolRegistry()
    td = _tool_def("data_reader")
    registry._register(td, lambda _i: {"ok": True})
    dag = DagExecutor(executor, store, registry)
    planner = Planner(MockLLMClient(responses=[]), registry, store)
    defs, fns = registry.list_tool_defs(), registry.list_tool_fns()
    return PlanningExecutorScheduler(
        store, executor, planner, dag, defs, fns, config=SchedulerConfig()
    )


async def _start_run(store: EventStore, run_id: str) -> None:
    await store.append_event(
        run_id,
        EventType.RUN,
        RunStartedPayload(intent="t").model_dump(),
    )


class TestExecutionEntryRevalidation:
    @pytest.mark.asyncio
    async def test_invalid_plan_rejected_before_any_execution_event(self):
        store = EventStore(":memory:")
        await store.initialize()
        run_id = "r5-reject"
        try:
            await _start_run(store, run_id)
            sched = await _make_scheduler(store)
            bad_plan = DagPlan.model_validate(
                {
                    "intent": "t",
                    "steps": [
                        {"id": "s1", "tool": "nonexistent_tool", "input": {}}
                    ],
                }
            )

            state, _ = await sched._execute_plan(run_id, bad_plan, 0)
            events = await store.get_events(run_id)

            assert state.status.value == "failed"
            assert not any(e.event_type == EventType.PLAN and e.phase == "created" for e in events)
            assert not any(e.event_type == EventType.STEP for e in events)
            assert not any(e.event_type == EventType.TOOL for e in events)

            plan_failed = [e for e in events if e.event_type == EventType.PLAN and e.phase == "failed"]
            assert len(plan_failed) == 1
            assert plan_failed[0].payload["completed_steps"] == 0
            assert plan_failed[0].payload["total_layers"] == 0
            assert "nonexistent_tool" in plan_failed[0].payload["final_error"]

            assert any(e.event_type == EventType.RUN and e.phase == "failed" for e in events)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_valid_plan_passes_entry_check_and_completes(self):
        store = EventStore(":memory:")
        await store.initialize()
        run_id = "r5-pass"
        try:
            await _start_run(store, run_id)
            sched = await _make_scheduler(store)
            plan = DagPlan.model_validate(
                {
                    "intent": "t",
                    "steps": [
                        {"id": "s1", "tool": "data_reader", "input": {}}
                    ],
                }
            )

            state, _ = await sched._execute_plan(run_id, plan, 0)
            events = await store.get_events(run_id)

            assert state.status.value == "completed"
            assert any(e.event_type == EventType.PLAN and e.phase == "created" for e in events)
            assert any(e.event_type == EventType.PLAN and e.phase == "completed" for e in events)
            assert not any(e.event_type == EventType.PLAN and e.phase == "failed" for e in events)
        finally:
            await store.close()
