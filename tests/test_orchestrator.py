"""Integration tests for V0.4+ Dynamic Orchestration — Orchestrator + PlanGuardrail."""

import asyncio
import time
from typing import Any

import pytest

from harness import (
    EventStore,
    EventType,
    ExecutionStatus,
    Guardrail,
    GuardrailRunner,
    Orchestrator,
    PlanGuardrail,
    RetryPolicy,
    SideEffect,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
)
from harness.models.events import (
    ConfirmationReceivedPayload,
    OrchestrationCompletedPayload,
    OrchestrationFailedPayload,
    OrchestrationStartedPayload,
    StepCompletedPayload,
    StepFailedPayload,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_tool(name="test", input_schema=None, guardrails=None, idempotency_key_fields=None, side_effects=None, **kw):
    return ToolDefinition(
        name=name,
        description=name,
        input_schema=input_schema or {},
        idempotency_key_fields=idempotency_key_fields or ["x"],
        side_effects=side_effects or [],
        guardrails=guardrails,
        **kw,
    )


def _noop_fn(input: dict[str, Any]) -> dict[str, Any]:
    return {"result": f"done_{input.get('x', '?')}"}


async def _failing_fn(input: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError(f"Step failed intentionally: {input}")


async def _wait_for_event(store, run_id, event_type, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = await store.get_events(run_id)
        if any(e.event_type == event_type for e in events):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timeout waiting for {event_type} in run {run_id}")


# ── PlanGuardrail ────────────────────────────────────────────────────


class TestPlanGuardrail:
    def test_rejects_empty_steps(self):
        guardrail = PlanGuardrail(ToolRegistry(), max_steps=10)
        with pytest.raises(ValueError, match="at least one step"):
            guardrail.validate([])

    def test_rejects_excessive_steps(self):
        guardrail = PlanGuardrail(ToolRegistry(), max_steps=2)
        with pytest.raises(ValueError, match="exceeds maximum steps"):
            guardrail.validate([{"tool": "a", "input": {}}, {"tool": "b", "input": {}}, {"tool": "c", "input": {}}])

    def test_rejects_missing_tool_field(self):
        guardrail = PlanGuardrail(ToolRegistry(), max_steps=10)
        with pytest.raises(ValueError, match="missing 'tool'"):
            guardrail.validate([{"input": {}}])

    def test_rejects_non_string_tool(self):
        guardrail = PlanGuardrail(ToolRegistry(), max_steps=10)
        with pytest.raises(ValueError, match="must be a string"):
            guardrail.validate([{"tool": 42, "input": {}}])

    def test_rejects_unknown_tool(self):
        guardrail = PlanGuardrail(ToolRegistry(), max_steps=10)
        with pytest.raises(ValueError, match="unknown tool"):
            guardrail.validate([{"tool": "nope", "input": {}}])

    def test_rejects_bad_input_schema(self):
        registry = ToolRegistry()
        registry.register(_make_tool(name="echo", input_schema={
            "type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"],
        }), _noop_fn)
        guardrail = PlanGuardrail(registry, max_steps=10)
        with pytest.raises(ValueError, match="input schema validation failed"):
            guardrail.validate([{"tool": "echo", "input": {"x": "not_an_int"}}])

    def test_valid_plan_passes(self):
        registry = ToolRegistry()
        registry.register(_make_tool(name="echo"), _noop_fn)
        registry.register(_make_tool(name="ping"), _noop_fn)
        guardrail = PlanGuardrail(registry, max_steps=10)
        guardrail.validate([
            {"tool": "echo", "input": {"x": 1}},
            {"tool": "ping", "input": {"x": 2}},
        ])


# ── Orchestrator — Basic flow ──────────────────────────────────────


@pytest.fixture
async def orch_store():
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def orch_registry():
    registry = ToolRegistry()
    registry.register(_make_tool(name="echo"), _noop_fn)
    registry.register(_make_tool(name="ping"), _noop_fn)
    return registry


class TestOrchestratorBasic:
    @pytest.mark.asyncio
    async def test_successful_3_step_plan_produces_5_events(self, orch_store, orch_registry):
        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        result = await orch.execute("run-1", {
            "intent": "test plan",
            "steps": [
                {"tool": "echo", "input": {"x": 1}},
                {"tool": "ping", "input": {"x": 2}},
                {"tool": "echo", "input": {"x": 3}},
            ],
        })

        assert result["status"] == "completed"
        assert result["completed_steps"] == 3

        events = await orch_store.get_events("run-1")
        event_types = [e.event_type for e in events]
        orchestration_started_count = event_types.count(EventType.ORCHESTRATION_STARTED)
        step_completed_count = event_types.count(EventType.STEP_COMPLETED)
        orchestration_completed_count = event_types.count(EventType.ORCHESTRATION_COMPLETED)

        assert orchestration_started_count == 1
        assert step_completed_count == 3
        assert orchestration_completed_count == 1
        assert EventType.ORCHESTRATION_FAILED not in event_types
        assert EventType.STEP_FAILED not in event_types

    @pytest.mark.asyncio
    async def test_step_failure_terminates_plan(self, orch_store, orch_registry):
        orch_registry.register(
            _make_tool(name="failer"),
            _failing_fn,
        )
        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        result = await orch.execute("run-2", {
            "intent": "fail test",
            "steps": [
                {"tool": "echo", "input": {"x": 1}},
                {"tool": "failer", "input": {"x": 2}},
                {"tool": "echo", "input": {"x": 3}},
            ],
        })

        assert result["status"] == "failed"
        assert result["completed_steps"] == 1
        assert "failer" in result["error"]

        events = await orch_store.get_events("run-2")
        event_types = [e.event_type for e in events]
        assert event_types.count(EventType.STEP_COMPLETED) == 1
        assert event_types.count(EventType.STEP_FAILED) == 1
        assert event_types.count(EventType.ORCHESTRATION_FAILED) == 1
        assert EventType.ORCHESTRATION_COMPLETED not in event_types

    @pytest.mark.asyncio
    async def test_plan_guardrail_rejects_bad_plan(self, orch_store, orch_registry):
        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        result = await orch.execute("run-3", {
            "intent": "bad",
            "steps": [{"tool": "nonexistent", "input": {}}],
        })
        assert result["status"] == "failed"
        assert "unknown tool" in result["error"]

        events = await orch_store.get_events("run-3")
        assert any(e.event_type == EventType.GUARDRAIL_TRIGGERED for e in events)

    @pytest.mark.asyncio
    async def test_idempotency_works_within_orchestration(self, orch_store, orch_registry):
        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        result1 = await orch.execute("run-4", {
            "intent": "idem test",
            "steps": [{"tool": "echo", "input": {"x": 1}}],
        })
        result2 = await orch.execute("run-4", {
            "intent": "idem test",
            "steps": [{"tool": "echo", "input": {"x": 1}}],
        })

        assert result1["status"] == "completed"
        assert result2["status"] == "completed"
        events = await orch_store.get_events("run-4")
        tool_completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
        assert len(tool_completed) == 1

    @pytest.mark.asyncio
    async def test_orchestration_results_in_aggregated_output(self, orch_store, orch_registry):
        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        result = await orch.execute("run-5", {
            "intent": "aggregated",
            "steps": [
                {"tool": "echo", "input": {"x": 10}},
                {"tool": "ping", "input": {"x": 20}},
            ],
        })

        assert result["status"] == "completed"
        assert len(result["results"]) == 2
        assert result["results"][0]["tool"] == "echo"
        assert result["results"][0]["output"]["result"] == "done_10"
        assert result["results"][1]["tool"] == "ping"
        assert result["results"][1]["output"]["result"] == "done_20"


# ── Orchestrator — Confirmation flow ──────────────────────────────


class TestOrchestratorConfirmation:
    @pytest.mark.asyncio
    async def test_confirmation_needed_pauses_and_resumes(self, orch_store):
        """Step requiring confirmation triggers pause; after RunResumed it continues."""
        registry = ToolRegistry()
        registry.register(_make_tool(name="safe"), _noop_fn)
        registry.register(
            _make_tool(name="dangerous", requires_confirmation=True),
            _noop_fn,
        )

        orch = Orchestrator(orch_store, ToolExecutor(orch_store), registry, max_steps=10)

        async def _run_plan():
            return await orch.execute("run-cf1", {
                "intent": "confirm test",
                "steps": [
                    {"tool": "safe", "input": {"x": 1}},
                    {"tool": "dangerous", "input": {"x": 2}},
                    {"tool": "safe", "input": {"x": 3}},
                ],
            })

        task = asyncio.create_task(_run_plan())
        await _wait_for_event(orch_store, "run-cf1", EventType.RUN_PAUSED)

        events = await orch_store.get_events("run-cf1")
        assert EventType.CONFIRMATION_REQUESTED in [e.event_type for e in events]

        cf_req = next(e for e in events if e.event_type == EventType.CONFIRMATION_REQUESTED)
        confirmation_id = cf_req.payload["confirmation_id"]

        await orch_store.append_event(
            "run-cf1",
            EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=confirmation_id,
                confirmed=True,
                operator_id="test-operator",
            ).model_dump(),
        )
        await orch_store.append_event(
            "run-cf1",
            EventType.RUN_RESUMED,
            {"resume_from_seq": await orch_store.get_latest_seq("run-cf1")},
        )
        await _wait_for_event(orch_store, "run-cf1", EventType.ORCHESTRATION_COMPLETED)

        result = await task
        assert result["status"] == "completed"
        assert result["completed_steps"] == 3

        final_events = await orch_store.get_events("run-cf1")
        final_types = [e.event_type for e in final_events]
        assert final_types.count(EventType.STEP_COMPLETED) == 3
        assert EventType.ORCHESTRATION_FAILED not in final_types

    @pytest.mark.asyncio
    async def test_confirmation_denied_during_orchestration(self, orch_store):
        registry = ToolRegistry()
        registry.register(_make_tool(name="safe"), _noop_fn)
        registry.register(
            _make_tool(name="dangerous", requires_confirmation=True),
            _noop_fn,
        )

        orch = Orchestrator(orch_store, ToolExecutor(orch_store), registry, max_steps=10)

        async def _run_plan():
            return await orch.execute("run-cf2", {
                "intent": "deny test",
                "steps": [
                    {"tool": "dangerous", "input": {"x": 1}},
                    {"tool": "safe", "input": {"x": 2}},
                ],
            })

        task = asyncio.create_task(_run_plan())
        await _wait_for_event(orch_store, "run-cf2", EventType.RUN_PAUSED)

        events = await orch_store.get_events("run-cf2")
        cf_req = next(e for e in events if e.event_type == EventType.CONFIRMATION_REQUESTED)
        confirmation_id = cf_req.payload["confirmation_id"]

        await orch_store.append_event(
            "run-cf2",
            EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=confirmation_id,
                confirmed=False,
                operator_id="test-operator",
            ).model_dump(),
        )
        await orch_store.append_event(
            "run-cf2",
            EventType.RUN_RESUMED,
            {"resume_from_seq": await orch_store.get_latest_seq("run-cf2")},
        )
        await _wait_for_event(orch_store, "run-cf2", EventType.ORCHESTRATION_FAILED)

        result = await task
        assert result["status"] == "failed"
        assert result["completed_steps"] == 0
        assert "denied" in result["error"].lower() or "confirmation" in result["error"].lower()


# ── Orchestrator — Event stream integrity ─────────────────────────


class TestOrchestratorEventIntegrity:
    @pytest.mark.asyncio
    async def test_orchestration_events_are_traceable(self, orch_store, orch_registry):
        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        await orch.execute("run-trace", {
            "intent": "traceable plan",
            "steps": [
                {"tool": "echo", "input": {"x": 1}},
                {"tool": "ping", "input": {"x": 2}},
            ],
        })

        events = await orch_store.get_events("run-trace")
        seqs = [e.seq for e in events]

        orchestration_events = [
            EventType.ORCHESTRATION_STARTED,
            EventType.STEP_COMPLETED,
            EventType.STEP_COMPLETED,
            EventType.ORCHESTRATION_COMPLETED,
        ]
        orch_event_seq = 0
        for et in orchestration_events:
            for e in events:
                if e.event_type == et and e.seq > orch_event_seq:
                    orch_event_seq = e.seq
                    break

        assert orch_event_seq > 0

        # Verify step outputs are stored in StepCompleted events
        step_events = [e for e in events if e.event_type == EventType.STEP_COMPLETED]
        assert len(step_events) == 2
        payloads = [StepCompletedPayload.model_validate(e.payload) for e in step_events]
        assert payloads[0].step_index == 0
        assert payloads[1].step_index == 1

    @pytest.mark.asyncio
    async def test_orchestration_with_no_regression(self, orch_store, orch_registry):
        """Running orchestration does not break existing event folding."""
        from harness import fold_events

        orch = Orchestrator(orch_store, ToolExecutor(orch_store), orch_registry, max_steps=10)
        await orch.execute("run-nr", {
            "intent": "no regression",
            "steps": [{"tool": "echo", "input": {"x": 1}}],
        })

        events = await orch_store.get_events("run-nr")
        state = fold_events(events)
        assert state.run_id == "run-nr"
        assert len(state.orchestration_history) == 1
        assert state.orchestration_history[0]["intent"] == "no regression"
        assert state.orchestration_history[0]["status"] == "completed"
