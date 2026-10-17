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
            _event("run-1", 1, EventType.RUN, {"intent": "a", "context_snapshot": {}}),
            _event("run-2", 2, EventType.AGENT_THOUGHT, {"thought": "x"}),
        ]
        with pytest.raises(ValueError, match="Mixed run_ids"):
            fold_events(events)

    def test_run_started_sets_intent(self):
        events = [
            _event("run-1", 1, EventType.RUN, {"intent": "test intent", "context_snapshot": {"k": "v"}}),
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "think 1"}),
            _event("r1", 3, EventType.AGENT_THOUGHT, {"thought": "think 2"}),
        ]
        state = fold_events(events)
        assert len(state.thought_history) == 2
        assert state.thought_history[0].thought == "think 1"
        assert state.thought_history[1].thought == "think 2"
        assert state.latest_thought.thought == "think 2"

    def test_latest_thought_is_none_initially(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
        ]
        state = fold_events(events)
        assert state.latest_thought is None


class TestFoldToolCalls:
    def test_tool_called_is_tracked(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.TOOL,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN, {"result_summary": "done"}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.COMPLETED
        assert state.summary == "done"

    def test_run_failed_changes_status(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN, {"final_error": "boom", "event_count": 2}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.FAILED
        assert state.last_error == "boom"

    def test_pause_and_resume(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN, {"reason": "confirm wait"}),
            _event("r1", 3, EventType.RUN, {"resume_from_seq": 2}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.RUNNING
        assert state.pause_reason is None

    def test_running_after_started_by_default(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
        ]
        state = fold_events(events)
        assert state.status == RunStatus.RUNNING


class TestFoldConfirmation:
    def test_confirmation_requested_adds_to_pending(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION,
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
                EventType.CONFIRMATION,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION,
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
                EventType.CONFIRMATION,
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
            _event("r1", 1, EventType.RUN, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONFIRMATION,
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
                EventType.CONFIRMATION,
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
                EventType.CONFIRMATION,
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
                _event("r1", 1, EventType.RUN, {"intent": "a", "context_snapshot": {"k": "v"}}),
                _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "t1"}),
                _event(
                    "r1",
                    3,
                    EventType.TOOL,
                    {
                        "tool_call_id": "tc-1",
                        "tool_name": "http",
                        "output": "ok",
                        "duration_ms": 10,
                    },
                ),
                _event("r1", 4, EventType.RUN, {"result_summary": "done"}),
            ]

        s1 = fold_events(make_events())
        s2 = fold_events(make_events())
        assert s1 == s2

    def test_seq_does_not_affect_state(self):
        e1 = [
            _event("r1", 1, EventType.RUN, {"intent": "a", "context_snapshot": {}}),
            _event("r1", 3, EventType.RUN, {"result_summary": "ok"}),
        ]
        e2 = [
            _event("r1", 10, EventType.RUN, {"intent": "a", "context_snapshot": {}}),
            _event("r1", 11, EventType.RUN, {"result_summary": "ok"}),
        ]
        s1 = fold_events(e1)
        s2 = fold_events(e2)
        assert s1.status == s2.status
        assert s1.intent == s2.intent
        assert s1.summary == s2.summary


class TestFoldContextCheckpointed:
    def test_checkpoint_sets_last_checkpoint_seq(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "thinking"}),
            _event(
                "r1",
                3,
                EventType.CONTEXT,
                {"checkpoint_seq": 3, "snapshot_ref": "snap-1"},
            ),
        ]
        state = fold_events(events)
        assert state.last_checkpoint_seq == 3

    def test_no_checkpoint_is_none(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.AGENT_THOUGHT, {"thought": "thinking"}),
        ]
        state = fold_events(events)
        assert state.last_checkpoint_seq is None

    def test_latest_checkpoint_wins(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.CONTEXT,
                {"checkpoint_seq": 2, "snapshot_ref": "snap-1"},
            ),
            _event("r1", 3, EventType.AGENT_THOUGHT, {"thought": "thinking"}),
            _event(
                "r1",
                4,
                EventType.CONTEXT,
                {"checkpoint_seq": 4, "snapshot_ref": "snap-2"},
            ),
        ]
        state = fold_events(events)
        assert state.last_checkpoint_seq == 4


class TestFoldDagStepDedup:
    """V0.7: DAG_STEP events should update by step_id, not append duplicates."""

    def test_dag_step_started_completed_no_duplicate(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1},
            ),
            _event(
                "r1",
                3,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []},
            ),
            _event("r1", 4, EventType.STEP, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok"}),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s1"
        assert state.latest_plan["steps"][0]["status"] == "completed"

    def test_dag_step_started_failed_no_duplicate(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1},
            ),
            _event(
                "r1",
                3,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []},
            ),
            _event(
                "r1",
                4,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s1", "error": "boom", "retryable": False},
            ),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s1"
        assert state.latest_plan["steps"][0]["status"] == "failed"

    def test_multiple_steps_no_collision(self):
        """Different step_ids should each have their own entry."""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1},
            ),
            _event(
                "r1",
                3,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []},
            ),
            _event(
                "r1",
                4,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s2", "tool_name": "echo", "depends_on": []},
            ),
            _event("r1", 5, EventType.STEP, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok"}),
            _event(
                "r1",
                6,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s2", "error": "boom", "retryable": False},
            ),
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
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "first", "steps_summary": "1 step", "layer_count": 1},
            ),
            _event(
                "r1",
                3,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []},
            ),
            _event("r1", 4, EventType.STEP, {"plan_id": "p1", "step_id": "s1", "output_summary": "ok"}),
            _event(
                "r1",
                5,
                EventType.PLAN,
                {"plan_id": "p1", "revision_reason": "step_failure_revised", "remaining_steps_summary": "revised"},
            ),
            _event(
                "r1",
                6,
                EventType.PLAN,
                {"plan_id": "p2", "intent": "revised", "steps_summary": "1 step", "layer_count": 1},
            ),
            _event(
                "r1",
                7,
                EventType.STEP,
                {"plan_id": "p2", "step_id": "s2", "tool_name": "echo", "depends_on": []},
            ),
            _event("r1", 8, EventType.STEP, {"plan_id": "p2", "step_id": "s2", "output_summary": "done"}),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert state.latest_plan["plan_id"] == "p2"
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s2"

    def test_plan_revised_steps_replace_blueprint(self):
        """v2.2 (P2): PLAN_REVISED 携带 steps → 折叠态 latest_plan 与实际修订计划对齐。"""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "first", "steps_summary": "2 steps", "layer_count": 1},
            ),
            _event(
                "r1",
                3,
                EventType.PLAN,
                {
                    "plan_id": "p1",
                    "revision_reason": "step_failure_revised",
                    "remaining_steps_summary": "1 step remaining",
                    "steps": [
                        {
                            "step_id": "s2",
                            "tool_name": "http_request",
                            "input": {"url": "http://b"},
                            "depends_on": [],
                            "description": "",
                            "probe": False,
                        },
                    ],
                },
            ),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert state.latest_plan["plan_id"] == "p1"
        assert state.latest_plan["revision_reason"] == "step_failure_revised"
        steps = state.latest_plan["steps"]
        assert len(steps) == 1
        assert steps[0]["step_id"] == "s2"
        assert steps[0]["tool_name"] == "http_request"
        assert steps[0]["input"] == {"url": "http://b"}

    def test_plan_revised_without_steps_keeps_blueprint(self):
        """PLAN_REVISED 不带 steps（空修订）→ 保留原有步骤蓝图（兼容旧事件流）。"""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event(
                "r1",
                2,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "first", "steps_summary": "1 step", "layer_count": 1},
            ),
            _event(
                "r1",
                3,
                EventType.STEP,
                {"plan_id": "p1", "step_id": "s1", "tool_name": "echo", "depends_on": []},
            ),
            _event(
                "r1",
                4,
                EventType.PLAN,
                {"plan_id": "p1", "revision_reason": "step_failure_revised", "remaining_steps_summary": "revised"},
            ),
        ]
        state = fold_events(events)
        assert state.latest_plan is not None
        assert len(state.latest_plan["steps"]) == 1
        assert state.latest_plan["steps"][0]["step_id"] == "s1"


class TestFoldFeedbackInjected:
    def test_feedback_injected_appends_to_feedbacks(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.FEEDBACK_INJECTED, {"feedback_text": "too slow", "priority": "high"}),
            _event("r1", 3, EventType.FEEDBACK_INJECTED, {"feedback_text": "retry", "priority": "medium"}),
        ]
        state = fold_events(events)
        assert len(state.feedbacks) == 2
        assert state.feedbacks[0].feedback_text == "too slow"
        assert state.feedbacks[0].priority == "high"
        assert state.feedbacks[1].feedback_text == "retry"
        assert state.feedbacks[1].priority == "medium"

    def test_feedback_injected_no_feedback(self):
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
        ]
        state = fold_events(events)
        assert state.feedbacks == []

    def test_feedback_injected_at_seq_set_by_fold(self):
        """Fold should set injected_at_seq on FeedbackInjectedPayload from event.seq."""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.FEEDBACK_INJECTED, {"feedback_text": "first", "priority": "high"}),
            _event("r1", 5, EventType.FEEDBACK_INJECTED, {"feedback_text": "second", "priority": "medium"}),
        ]
        state = fold_events(events)
        assert len(state.feedbacks) == 2
        assert state.feedbacks[0].injected_at_seq == 2
        assert state.feedbacks[1].injected_at_seq == 5

    def test_plan_revised_marks_feedbacks_consumed(self):
        """PlanRevised should set consumed_at_seq on all unconsumed feedbacks."""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.FEEDBACK_INJECTED, {"feedback_text": "warning", "priority": "high"}),
            _event("r1", 3, EventType.FEEDBACK_INJECTED, {"feedback_text": "info", "priority": "low"}),
            _event(
                "r1",
                4,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "test", "steps_summary": "2 steps", "layer_count": 1},
            ),
            _event(
                "r1",
                5,
                EventType.PLAN,
                {"plan_id": "p1", "revision_reason": "step_failure_revised", "remaining_steps_summary": "revised"},
            ),
        ]
        state = fold_events(events)
        assert len(state.feedbacks) == 2
        # Both feedbacks should be marked consumed at seq=5 (the PlanRevised seq)
        assert state.feedbacks[0].consumed_at_seq == 5
        assert state.feedbacks[1].consumed_at_seq == 5

    def test_plan_revised_does_not_double_consume(self):
        """Subsequent PlanRevised should not overwrite consumed_at_seq of already-consumed feedbacks."""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.FEEDBACK_INJECTED, {"feedback_text": "warning", "priority": "high"}),
            _event(
                "r1",
                3,
                EventType.PLAN,
                {"plan_id": "p1", "intent": "test", "steps_summary": "1 step", "layer_count": 1},
            ),
            _event(
                "r1",
                4,
                EventType.PLAN,
                {"plan_id": "p1", "revision_reason": "step_failure_revised", "remaining_steps_summary": "revised"},
            ),
            _event("r1", 5, EventType.FEEDBACK_INJECTED, {"feedback_text": "new_issue", "priority": "high"}),
            _event(
                "r1",
                6,
                EventType.PLAN,
                {"plan_id": "p2", "intent": "revised", "steps_summary": "1 step", "layer_count": 1},
            ),
            _event(
                "r1",
                7,
                EventType.PLAN,
                {"plan_id": "p2", "revision_reason": "step_failure_revised", "remaining_steps_summary": "revised"},
            ),
        ]
        state = fold_events(events)
        assert len(state.feedbacks) == 2
        # First feedback consumed at seq=4 (first PlanRevised)
        assert state.feedbacks[0].consumed_at_seq == 4
        # Second feedback consumed at seq=7 (second PlanRevised)
        assert state.feedbacks[1].consumed_at_seq == 7

    def test_feedback_without_plan_revised_not_consumed(self):
        """Feedbacks without any PlanRevised should have consumed_at_seq=None."""
        events = [
            _event("r1", 1, EventType.RUN, {"intent": "test", "context_snapshot": {}}),
            _event("r1", 2, EventType.FEEDBACK_INJECTED, {"feedback_text": "warning", "priority": "high"}),
        ]
        state = fold_events(events)
        assert len(state.feedbacks) == 1
        assert state.feedbacks[0].consumed_at_seq is None

