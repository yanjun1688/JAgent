"""Shared event-seeding helpers for replay tests.

Builds a run whose plan fails at step s2 (guardrail block → step failed →
plan failed → run failed), which is the canonical "when did it go wrong"
debugging scenario.
"""

from __future__ import annotations

from harness.models.events import (
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepStartedPayload,
    GuardrailTriggeredPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
    RunFailedPayload,
    RunStartedPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
)
from harness.storage.event_store import EventStore

# (event_type, payload_model) in seq order for the failed-plan scenario.
_SEED_EVENTS = [
    (RunStartedPayload, {"intent": "do thing", "context_snapshot": {}}),
    (
        PlanCreatedPayload,
        {
            "plan_id": "p1",
            "intent": "do thing",
            "steps_summary": "s1 then s2",
            "layer_count": 1,
            "steps": [
                {"step_id": "s1", "tool_name": "read", "status": "pending"},
                {"step_id": "s2", "tool_name": "write", "status": "pending"},
            ],
        },
    ),
    (DagStepStartedPayload, {"plan_id": "p1", "step_id": "s1", "tool_name": "read"}),
    (ToolCalledPayload, {"tool_call_id": "tc1", "tool_name": "read", "input": {"path": "a.txt"}}),
    (ToolCompletedPayload, {"tool_call_id": "tc1", "tool_name": "read", "output": "ok", "duration_ms": 12}),
    (DagStepCompletedPayload, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok", "status": "completed"}),
    (DagStepStartedPayload, {"plan_id": "p1", "step_id": "s2", "tool_name": "write"}),
    (
        GuardrailTriggeredPayload,
        {
            "tool_call_id": "tc2",
            "tool_name": "write",
            "guardrail_id": "no_write_outside_workspace",
            "reason": "path escapes workspace root",
        },
    ),
    (
        DagStepFailedPayload,
        {"plan_id": "p1", "step_id": "s2", "error": "blocked by guardrail", "tool_call_id": "tc2"},
    ),
    (PlanFailedPayload, {"plan_id": "p1", "completed_steps": 1, "total_layers": 1, "final_error": "s2 failed"}),
    (
        RunFailedPayload,
        {"final_error": "Plan failed: s2 failed", "event_count": 11, "user_facing_message": "任务未能完成"},
    ),
]


async def seed_failed_plan_run(store: EventStore, run_id: str, tenant_id: str = "default") -> None:
    """Append the failed-plan event stream to ``store`` (11 events)."""
    for payload_model, payload in _SEED_EVENTS:
        await store.append_event(run_id, _event_type(payload_model), payload_model(**payload).model_dump(),
                                 tenant_id=tenant_id)


def _event_type(payload_model: type):
    from harness.models.events import PAYLOAD_MODEL_MAP

    for et, model in PAYLOAD_MODEL_MAP.items():
        if model is payload_model:
            return et
    raise ValueError(f"no event type mapped for {payload_model.__name__}")
