"""R6 (BDD): plan-layer trusted rejections are recorded as structured events.

Feature: Plan rejection observability
  As the operator of an Agent-First engine
  I want every trusted rejection of a planner output to be a structured event
  So that I can see how many times a plan was rejected and which rule rejected it

A non-trusted LLM plan that parses but violates a trusted rule (unknown tool,
illegal probe, cycle, bad ``$ref``, degenerate revision, weakened invariant,
post-merge invalid plan) must emit a ``Diagnostic/plan_rejected`` event. Parse
failures are deliberately out of scope: the prompt pins the JSON contract.

Scenarios:
  1. Payload contract — round-trip and Diagnostic-union membership
  2. Plan stage     — one event per attempt; the last is recovered=False
  3. Revise stage   — one event per attempt; the last is recovered=False
  4. Degenerate     — revisions repeating a failed action are rejected
  5. Merge          — post-merge invalid plan fails closed (recovered=False)
  6. Missing store / run_id degrades to logging, never raises
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import ExecState, StepResult
from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner
from harness.core.planner.rejections import record_plan_rejection
from harness.core.scheduler.plan import PlanningExecutorScheduler
from harness.models.events import EventType, PlanRejectedPayload, parse_event_payload
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import ToolDefinition, unknown_tool_message
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

BAD_PLAN_JSON = json.dumps({"steps": [{"id": "s1", "tool": "ghost_tool", "input": {}}]})


# ── Scenario 1: payload contract ───────────────────────────────


class TestPlanRejectedPayloadContract:
    def test_round_trip_through_diagnostic_union(self):
        payload = PlanRejectedPayload(
            stage="plan",
            attempt=2,
            reasons=[unknown_tool_message("ghost_tool")],
            recovered=False,
        )
        dumped = payload.model_dump(mode="json")
        parsed = parse_event_payload(EventType.DIAGNOSTIC, dumped)
        assert parsed == payload
        assert parsed.phase == "plan_rejected"

    def test_unknown_stage_rejected_by_payload(self):
        with pytest.raises(Exception):
            PlanRejectedPayload(stage="not_a_stage", attempt=1, reasons=["x"], recovered=True)  # type: ignore[arg-type]


# ── Scenario 2: plan stage ─────────────────────────────────────


class TestPlanStageRejection:
    @pytest.mark.asyncio
    async def test_every_failed_plan_attempt_emits_rejection_event(self):
        # Given a planner whose LLM always returns a parseable but invalid plan
        store = EventStore(":memory:")
        await store.initialize()
        try:
            registry = ToolRegistry()
            planner = Planner(
                MockLLMClient(responses=[BAD_PLAN_JSON, BAD_PLAN_JSON]),
                registry,
                store,
                max_plan_retries=1,
            )

            # When the planner runs (2 attempts, both rejected by the guardrail)
            result = await planner.plan("do the thing", run_id="r1")

            # Then the planner gives up and one rejection event exists per attempt
            assert result is None
            events = await store.get_events("r1")
            rejections = [
                e for e in events if e.event_type == EventType.DIAGNOSTIC and e.phase == "plan_rejected"
            ]
            assert [e.payload["attempt"] for e in rejections] == [1, 2]
            assert all(e.payload["stage"] == "plan" for e in rejections)
            assert rejections[0].payload["recovered"] is True
            assert rejections[-1].payload["recovered"] is False
            assert all(
                any(unknown_tool_message("ghost_tool") in r for r in e.payload["reasons"])
                for e in rejections
            )
        finally:
            await store.close()


# ── Scenario 3: revise stage ───────────────────────────────────


class TestReviseStageRejection:
    @pytest.mark.asyncio
    async def test_every_failed_revise_attempt_emits_rejection_event(self):
        # Given a planner revising a plan, always returning an invalid revision
        store = EventStore(":memory:")
        await store.initialize()
        try:
            registry = ToolRegistry()
            planner = Planner(
                MockLLMClient(responses=[BAD_PLAN_JSON, BAD_PLAN_JSON]),
                registry,
                store,
                max_plan_retries=1,
            )
            current = DagPlan(intent="x", steps=[DagStep(id="s0", tool="ghost_tool", input={})])

            # When the planner revises (2 attempts, both rejected)
            result = await planner.revise(current, {}, "system state", run_id="r2")

            # Then it gives up with a rejection event per attempt
            assert result is None
            events = await store.get_events("r2")
            rejections = [
                e for e in events if e.event_type == EventType.DIAGNOSTIC and e.phase == "plan_rejected"
            ]
            assert [e.payload["attempt"] for e in rejections] == [1, 2]
            assert all(e.payload["stage"] == "revise" for e in rejections)
            assert rejections[0].payload["recovered"] is True
            assert rejections[-1].payload["recovered"] is False
        finally:
            await store.close()


# ── Scenario 4: degenerate revision ────────────────────────────


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry._register(
        ToolDefinition(
            name="echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
            side_effects=[],
        ),
        lambda value: value,
    )
    return registry


class TestDegenerateStageRejection:
    @pytest.mark.asyncio
    async def test_revision_repeating_failed_action_emits_degenerate_event(self):
        # Given a run whose only step failed, and a reviser that repeats it
        store = EventStore(":memory:")
        await store.initialize()
        try:
            executor = ToolExecutor(store)
            registry = _echo_registry()
            dag = DagExecutor(executor, store, registry)
            planner = Planner(MockLLMClient([]), registry, store)
            sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {})

            current = DagPlan(intent="x", steps=[DagStep(id="s1", tool="echo", input={"msg": "hi"})])
            results = {"s1": StepResult(step_id="s1", exec_state=ExecState.FAILED, error="boom")}
            repeated = DagPlan(intent="x", steps=[DagStep(id="s1", tool="echo", input={"msg": "hi"})])
            planner.revise = AsyncMock(return_value=repeated)

            # When the degenerate guard runs (2 attempts)
            revised, error = await sched._revise_with_degenerate_guard(
                "r3", current, results, "state", None, "x", merge_context=None
            )

            # Then no revision is accepted and degenerate rejection events exist
            assert revised is None
            assert error is not None
            events = await store.get_events("r3")
            rejections = [
                e for e in events if e.event_type == EventType.DIAGNOSTIC and e.phase == "plan_rejected"
            ]
            assert [e.payload["attempt"] for e in rejections] == [1, 2, 3]
            assert all(e.payload["stage"] == "degenerate" for e in rejections)
            assert rejections[0].payload["recovered"] is True
            assert rejections[-1].payload["recovered"] is False
        finally:
            await store.close()


# ── Scenario 5: post-merge fail-closed ─────────────────────────


class TestMergeStageRejection:
    @pytest.mark.asyncio
    async def test_merged_invalid_plan_emits_merge_event_and_fails_run(self):
        # Given a run with a failed step, whose revision merges to an invalid plan
        store = EventStore(":memory:")
        await store.initialize()
        try:
            executor = ToolExecutor(store)
            registry = _echo_registry()
            dag = DagExecutor(executor, store, registry)
            planner = Planner(MockLLMClient([]), registry, store)
            sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {})

            original = DagPlan(intent="x", steps=[DagStep(id="s1", tool="echo", input={"msg": "hi"})])
            planner.plan = AsyncMock(return_value=original)
            planner.last_raw_response = "mock"
            planner.revise = AsyncMock(
                return_value=DagPlan(intent="x", steps=[DagStep(id="s2", tool="ghost_tool", input={})])
            )

            async def _execute_layer(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
                all_results["s1"] = StepResult(step_id="s1", exec_state=ExecState.FAILED, error="boom")
                return False

            dag.execute_layer = AsyncMock(side_effect=_execute_layer)
            dag.build_dag_status_text = Mock(return_value="")

            # When the run is driven
            state = await sched.run("r4", "do the thing")

            # Then the run fails and a merge rejection event is recorded
            assert state.status.value == "failed"
            events = await store.get_events("r4")
            merge_events = [
                e
                for e in events
                if e.event_type == EventType.DIAGNOSTIC
                and e.phase == "plan_rejected"
                and e.payload["stage"] == "merge"
            ]
            assert len(merge_events) == 1
            assert merge_events[0].payload["recovered"] is False
            assert merge_events[0].payload["reasons"]
        finally:
            await store.close()


# ── Scenario 6: graceful degradation ───────────────────────────


class TestRejectionRecordingDegradesSafely:
    @pytest.mark.asyncio
    async def test_missing_run_id_does_not_emit_or_raise(self):
        store = EventStore(":memory:")
        await store.initialize()
        try:
            await record_plan_rejection(
                store, None, stage="plan", attempt=1, reasons=["x"], recovered=True
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_missing_store_does_not_raise(self):
        await record_plan_rejection(
            None, "r9", stage="plan", attempt=1, reasons=["x"], recovered=True
        )
