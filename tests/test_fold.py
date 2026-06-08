from __future__ import annotations

import pytest

from harness.core.fold import RunStatus, fold_events
from harness.models.events import Event, EventType


def _event(
    run_id: str,
    seq: int,
    event_type: EventType,
    payload: dict,
    idempotency_key: str | None = None,
) -> Event:
    return Event(
        run_id=run_id,
        seq=seq,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        created_at=0.0,
    )


class TestFoldBasic:
    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            fold_events([])

    def test_mixed_run_ids_raises(self):
        events = [
            _event("run-1", 1, EventType.RUN_STARTED, {"intent": "a", "context_snapshot": {}}),
            _event("run-2", 2, EventType.AGENT_THOUGHT, {"thought": "x", "token_count": 1}),
        ]
        with pytest.raises(ValueError, match="Mixed run_ids"):
            fold_events(events)

    def test_run_started_sets_intent(self):
        events = [
            _event("run-1", 1, EventType.RUN_STARTED, {"intent": "test intent", "context_snapshot": {"k": "v"}}),
        ]
        state = fold_events(events)
        assert state.run_id == "run-1"
        assert state.intent == "test intent"
        assert state.context_snapshot == {"k": "v"}
        assert state.status == RunStatus.RUNNING
        assert state.seq == 1


class TestFoldThoughtHistory:
    def test_agent_thought_adds_to_history(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "think 1", "token_count": 10}),
            _event("r1", 3, EventType.AGENT_THOUGHT, {"thought": "think 2", "token_count": 20}),
        ]
        state = fold_events(events)
        assert len(state.thought_history) == 2
        assert state.thought_history[0].thought == "think 1"
        assert state.thought_history[1].thought == "think 2"
        assert state.latest_thought.thought == "think 2"

    def test_latest_thought_is_none_initially(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
        ]
        state = fold_events(events)
        assert state.latest_thought is None


class TestFoldToolCalls:
    def test_tool_called_is_tracked(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL_CALLED,
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "http",
                    "input": {"url": "https://x.com"},
                    "idempotency_key": "ik-1",
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.tool_calls) == 1
        assert state.tool_calls[0].tool_call_id == "tc-1"
        assert state.tool_calls[0].tool_name == "http"
        assert state.tool_calls[0].idempotency_key == "ik-1"


class TestFoldToolResults:
    def test_tool_completed_adds_result(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL_COMPLETED,
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "http",
                    "output": "200 OK",
                    "duration_ms": 150,
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.tool_results) == 1
        r = state.tool_results[0]
        assert r.tool_call_id == "tc-1"
        assert r.tool_name == "http"
        assert r.status == "completed"
        assert r.output == "200 OK"
        assert r.duration_ms == 150

    def test_tool_failed_adds_result(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL_FAILED,
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "http",
                    "error": "timeout",
                    "retryable": True,
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.tool_results) == 1
        r = state.tool_results[0]
        assert r.tool_call_id == "tc-1"
        assert r.tool_name == "http"
        assert r.status == "failed"
        assert r.error == "timeout"

    def test_tool_timeout_adds_result(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL_TIMEOUT,
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "browser",
                    "timeout_ms": 5000,
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.tool_results) == 1
        assert state.tool_results[0].status == "timeout"
        assert state.tool_results[0].tool_call_id == "tc-1"

    def test_guardrail_triggered_adds_result(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.GUARDRAIL_TRIGGERED,
                {
                    "tool_call_id": "tc-1",
                    "tool_name": "file_op",
                    "guardrail_id": "scope",
                    "reason": "denied",
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.tool_results) == 1
        assert state.tool_results[0].status == "guardrail_blocked"
        assert state.tool_results[0].tool_call_id == "tc-1"


class TestFoldStatusTransitions:
    def test_run_completed_changes_status(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_COMPLETED, {"result_summary": "done"}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.COMPLETED
        assert state.summary == "done"

    def test_run_failed_changes_status(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_FAILED, {"final_error": "boom", "event_count": 2}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.FAILED
        assert state.last_error == "boom"

    def test_pause_and_resume(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_PAUSED, {"reason": "confirm wait"}),
            _event("r1", 3, EventType.RUN_RESUMED, {"resume_from_seq": 2}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.RUNNING
        assert state.pause_reason is None

    def test_running_after_started_by_default(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.RUNNING


class TestFoldConfirmation:
    def test_confirmation_requested_adds_to_pending(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION_REQUESTED,
                {
                    "confirmation_id": "cf-1",
                    "tool_call_id": "tc-foo",
                    "tool_name": "file_op",
                    "input": {"path": "/x"},
                    "idempotency_key": "ik-foo",
                    "risk_level": "high",
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.pending_confirmations) == 1
        assert state.pending_confirmations[0].confirmation_id == "cf-1"
        assert state.pending_confirmations[0].tool_name == "file_op"
        assert state.pending_confirmations[0].risk_level == "high"

    def test_confirmation_received_confirmed_true(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION_REQUESTED,
                {
                    "confirmation_id": "cf-1",
                    "tool_call_id": "tc-foo",
                    "tool_name": "file_op",
                    "input": {"path": "/x"},
                    "idempotency_key": "ik-foo",
                    "risk_level": "high",
                },
            ),
            _event(
                "r1",
                3,
                EventType.CONFIRMATION_RECEIVED,
                {
                    "confirmation_id": "cf-1",
                    "confirmed": True,
                    "operator_id": "op-1",
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.pending_confirmations) == 0

    def test_confirmation_received_confirmed_false(self):
        """When confirmed=False, the confirmation is still resolved (removed from pending)."""
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION_REQUESTED,
                {
                    "confirmation_id": "cf-1",
                    "tool_call_id": "tc-foo",
                    "tool_name": "file_op",
                    "input": {"path": "/x"},
                    "idempotency_key": "ik-foo",
                    "risk_level": "high",
                },
            ),
            _event(
                "r1",
                3,
                EventType.CONFIRMATION_RECEIVED,
                {
                    "confirmation_id": "cf-1",
                    "confirmed": False,
                    "operator_id": "op-1",
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.pending_confirmations) == 0

    def test_confirmation_received_only_removes_matching_id(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION_REQUESTED,
                {
                    "confirmation_id": "cf-1",
                    "tool_call_id": "tc-foo",
                    "tool_name": "file_op",
                    "input": {"path": "/x"},
                    "idempotency_key": "ik-foo",
                    "risk_level": "high",
                },
            ),
            _event(
                "r1",
                3,
                EventType.CONFIRMATION_REQUESTED,
                {
                    "confirmation_id": "cf-2",
                    "tool_call_id": "tc-bar",
                    "tool_name": "run_code",
                    "input": {"code": "rm -rf /"},
                    "idempotency_key": "ik-bar",
                    "risk_level": "high",
                },
            ),
            _event(
                "r1",
                4,
                EventType.CONFIRMATION_RECEIVED,
                {
                    "confirmation_id": "cf-1",
                    "confirmed": True,
                    "operator_id": "op-1",
                },
            ),
        ]
        state = fold_events(events)
        assert len(state.pending_confirmations) == 1
        assert state.pending_confirmations[0].confirmation_id == "cf-2"


class TestFoldDeterminism:
    def test_same_events_same_state(self):
        def make_events():
            return [
                _event("r1", 1, EventType.RUN_STARTED, {"intent": "a", "context_snapshot": {"k": "v"}}),
                _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "t1", "token_count": 5}),
                _event(
                    "r1",
                    3,
                    EventType.TOOL_COMPLETED,
                    {
                        "tool_call_id": "tc-1",
                        "tool_name": "http",
                        "output": "ok",
                        "duration_ms": 10,
                    },
                ),
                _event("r1", 4, EventType.RUN_COMPLETED, {"result_summary": "done"}),
            ]

        s1 = fold_events(make_events())
        s2 = fold_events(make_events())
        assert s1 == s2

    def test_seq_does_not_affect_state(self):
        e1 = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "a", "context_snapshot": {}}),
            _event("r1", 3, EventType.RUN_COMPLETED, {"result_summary": "ok"}),
        ]
        e2 = [
            _event("r1", 10, EventType.RUN_STARTED, {"intent": "a", "context_snapshot": {}}),
            _event("r1", 11, EventType.RUN_COMPLETED, {"result_summary": "ok"}),
        ]
        s1 = fold_events(e1)
        s2 = fold_events(e2)
        assert s1.status == s2.status
        assert s1.intent == s2.intent
        assert s1.summary == s2.summary


class TestFoldContextCheckpointed:
    def test_checkpoint_sets_last_checkpoint_seq(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "thinking", "token_count": 50}),
            _event(
                "r1",
                3,
                EventType.CONTEXT_CHECKPOINTED,
                {"checkpoint_seq": 3, "snapshot_ref": "snap-1", "token_count": 500},
            ),
        ]
        state = fold_events(events)
        assert state.last_checkpoint_seq == 3

    def test_no_checkpoint_is_none(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "thinking", "token_count": 50}),
        ]
        state = fold_events(events)
        assert state.last_checkpoint_seq is None

    def test_latest_checkpoint_wins(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONTEXT_CHECKPOINTED,
                {"checkpoint_seq": 2, "snapshot_ref": "snap-1", "token_count": 500},
            ),
            _event("r1", 3, EventType.AGENT_THOUGHT, {"thought": "thinking", "token_count": 50}),
            _event(
                "r1",
                4,
                EventType.CONTEXT_CHECKPOINTED,
                {"checkpoint_seq": 4, "snapshot_ref": "snap-2", "token_count": 800},
            ),
        ]
        state = fold_events(events)
        assert state.last_checkpoint_seq == 4


class TestFoldDagStepDedup:
    """V0.7: DAG_STEP events should update by step_id, not append duplicates."""

    def test_dag_step_started_completed_no_duplicate(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.PLAN_CREATED, {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1}),
            _event("r1", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []}),
            _event("r1", 4, EventType.DAG_STEP_COMPLETED, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok"}),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s1"
        assert state.latest_plan["steps"][0]["status"] == "completed"

    def test_dag_step_started_failed_no_duplicate(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.PLAN_CREATED, {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1}),
            _event("r1", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []}),
            _event("r1", 4, EventType.DAG_STEP_FAILED, {"plan_id": "p1", "step_id": "s1", "error": "boom", "retryable": False}),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s1"
        assert state.latest_plan["steps"][0]["status"] == "failed"

    def test_multiple_steps_no_collision(self):
        """Different step_ids should each have their own entry."""
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.PLAN_CREATED, {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1}),
            _event("r1", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []}),
            _event("r1", 4, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s2", "tool_name": "echo", "depends_on": []}),
            _event("r1", 5, EventType.DAG_STEP_COMPLETED, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok"}),
            _event("r1", 6, EventType.DAG_STEP_FAILED, {"plan_id": "p1", "step_id": "s2", "error": "boom", "retryable": False}),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert len(state.latest_plan["steps"]) == 2
        statuses = {s["step_id"]: s["status"] for s in state.latest_plan["steps"]}
        assert statuses["s1"] == "completed"
        assert statuses["s2"] == "failed"

    def test_dag_step_different_plan_no_interference(self):
        """Steps from different plan_ids should not mix."""
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.PLAN_CREATED, {"plan_id": "p1", "intent": "first", "steps_summary": "1 step", "layer_count": 1}),
            _event("r1", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []}),
            _event("r1", 4, EventType.DAG_STEP_COMPLETED, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok"}),
            _event("r1", 5, EventType.PLAN_REVISED, {"plan_id": "p1", "revision_reason": "step_failure_revised", "remaining_steps_summary": "revised"}),
            _event("r1", 6, EventType.PLAN_CREATED, {"plan_id": "p2", "intent": "revised", "steps_summary": "1 step", "layer_count": 1}),
            _event("r1", 7, EventType.DAG_STEP_STARTED, {"plan_id": "p2", "step_id": "s2", "tool_name": "echo", "depends_on": []}),
            _event("r1", 8, EventType.DAG_STEP_COMPLETED, {"plan_id": "p2", "step_id": "s2", "output_summary": "done"}),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert state.latest_plan["plan_id"] == "p2"
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s2"
