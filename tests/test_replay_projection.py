"""BDD/TDD tests for the replay *projection* layer — pure functions only.

These tests pin the single most important architectural rule of the Event
Replay Inspector: **any historical state is reconstructed through the existing
``fold_events``** — the projection layer never re-derives state itself.

The projection functions are pure (no I/O, no tenant, no store) so that a
future "rollback / fork from history" write-path can reuse
``reconstruct_state`` unchanged — it does not assume the caller only wants to
display the state.
"""

from __future__ import annotations

import pytest

from harness.core.fold import RunStatus
from harness.models.events import Event, EventType
from harness.replay.projection import diff_states, project_state_view, reconstruct_state

# Note on the pure seam:
#   reconstruct_state(events, at_seq) -> RunState   (fold_events only — the rollback seam)
#   project_state_view(events, at_seq, latest_seq) -> RunStateView
#   diff_states(events, from_seq, to_seq) -> StateDiff
# State is ALWAYS derived via fold_events; the view/diff helpers additionally scan
# the (already folded) event slice to surface structured fields that fold flattens
# (e.g. guardrail_id).


def _event(run_id: str, seq: int, event_type: EventType, payload: dict) -> Event:
    return Event(run_id=run_id, seq=seq, event_type=event_type, payload=payload, created_at=float(seq))


def _failed_plan_run(run_id: str = "run-1") -> list[Event]:
    """A run whose plan fails at step s2 (guardrail block → plan failed → run failed)."""
    return [
        _event(run_id, 1, EventType.RUN_STARTED, {"intent": "do thing", "context_snapshot": {}}),
        _event(
            run_id,
            2,
            EventType.PLAN_CREATED,
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
        _event(run_id, 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "read"}),
        _event(
            run_id,
            4,
            EventType.TOOL_CALLED,
            {"tool_call_id": "tc1", "tool_name": "read", "input": {"path": "a.txt"}},
        ),
        _event(
            run_id,
            5,
            EventType.TOOL_COMPLETED,
            {"tool_call_id": "tc1", "tool_name": "read", "output": "ok", "duration_ms": 12},
        ),
        _event(
            run_id,
            6,
            EventType.DAG_STEP_COMPLETED,
            {"plan_id": "p1", "step_id": "s1", "output_summary": "ok", "status": "completed"},
        ),
        _event(run_id, 7, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s2", "tool_name": "write"}),
        _event(
            run_id,
            8,
            EventType.GUARDRAIL_TRIGGERED,
            {
                "tool_call_id": "tc2",
                "tool_name": "write",
                "guardrail_id": "no_write_outside_workspace",
                "reason": "path escapes workspace root",
            },
        ),
        _event(
            run_id,
            9,
            EventType.DAG_STEP_FAILED,
            {"plan_id": "p1", "step_id": "s2", "error": "blocked by guardrail", "tool_call_id": "tc2"},
        ),
        _event(
            run_id,
            10,
            EventType.PLAN_FAILED,
            {"plan_id": "p1", "completed_steps": 1, "total_layers": 1, "final_error": "s2 failed"},
        ),
        _event(
            run_id,
            11,
            EventType.RUN_FAILED,
            {
                "final_error": "Plan failed: s2 failed",
                "event_count": 11,
                "user_facing_message": "任务未能完成",
            },
        ),
    ]


class TestReconstructStateUsesFoldOnly:
    def test_reconstruct_at_latest_matches_full_fold(self):
        # Given a full event stream
        events = _failed_plan_run()
        # When reconstructing without an at_seq cutoff
        state = reconstruct_state(events)
        # Then it equals folding the whole stream (single source of truth)
        assert state.status is RunStatus.FAILED
        assert state.seq == 11

    def test_reconstruct_at_midpoint_returns_running_state(self):
        # Given a run that eventually fails
        events = _failed_plan_run()
        # When reconstructed as-of seq 6 (s1 just completed, s2 not started)
        state = reconstruct_state(events, at_seq=6)
        # Then the historical state is still RUNNING — not the terminal failure
        assert state.status is RunStatus.RUNNING
        assert state.seq == 6
        steps = {s["step_id"]: s["status"] for s in state.latest_plan["steps"]}
        assert steps["s1"] == "completed"
        assert steps["s2"] == "pending"

    def test_reconstruct_does_not_mutate_or_depend_on_later_events(self):
        # Given the full stream
        events = _failed_plan_run()
        # When reconstructing at seq 2 (plan just created)
        state = reconstruct_state(events, at_seq=2)
        # Then no tool results / failures from the future leak in
        assert state.tool_results == []
        assert state.last_error is None
        assert state.status is RunStatus.RUNNING

    def test_reconstruct_empty_stream_raises(self):
        with pytest.raises(ValueError, match="empty"):
            reconstruct_state([])

    def test_reconstruct_cutoff_before_first_event_raises(self):
        events = _failed_plan_run()
        with pytest.raises(ValueError, match="at_seq"):
            reconstruct_state(events, at_seq=0)


class TestProjectStateView:
    def test_view_surfaces_status_plan_steps_tool_results_and_guardrails(self):
        # Given the terminal (failed) reconstructed state
        events = _failed_plan_run()
        # When projected to a read-only view
        view = project_state_view(events, at_seq=11, latest_seq=11)
        # Then all debug-relevant surfaces are present
        assert view.status == "failed"
        assert view.is_latest is True
        assert view.at_seq == 11
        assert view.plan is not None
        step_status = {s.step_id: s.status for s in view.plan.steps}
        assert step_status["s1"] == "completed"
        assert step_status["s2"] == "failed"
        # s1 completed tool result present
        assert any(t.tool_call_id == "tc1" and t.status == "completed" for t in view.tool_results)
        # guardrail block surfaced as a structured record
        assert len(view.guardrail_blocks) == 1
        block = view.guardrail_blocks[0]
        assert block.guardrail_id == "no_write_outside_workspace"
        assert block.reason == "path escapes workspace root"
        assert block.event_seq == 8
        assert view.last_error == "Plan failed: s2 failed"

    def test_view_marks_historical_point_as_not_latest(self):
        events = _failed_plan_run()
        view = project_state_view(events, at_seq=6, latest_seq=11)
        assert view.is_latest is False
        assert view.status == "running"

    def test_view_surfaces_pending_confirmations(self):
        events = [
            _event("r", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event(
                "r",
                2,
                EventType.CONFIRMATION_REQUESTED,
                {
                    "confirmation_id": "cf1",
                    "tool_call_id": "tc9",
                    "tool_name": "rm",
                    "input": {"path": "/tmp"},
                    "idempotency_key": "k1",
                    "risk_level": "high",
                },
            ),
        ]
        view = project_state_view(events, at_seq=2, latest_seq=2)
        assert len(view.pending_confirmations) == 1
        assert view.pending_confirmations[0].confirmation_id == "cf1"
        assert view.pending_confirmations[0].risk_level == "high"

    def test_view_without_plan(self):
        events = [_event("r", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}})]
        view = project_state_view(events, at_seq=1, latest_seq=1)
        assert view.plan is None
        assert view.status == "running"


class TestStepEvidenceProjection:
    """F-1/L6-L7 (v3.4, ADR-011): Replay 视图暴露受信执行态证据投影。

    ``RunState.step_evidence`` 由 PLAN_*/DAG_STEP_*/TOOL_* 事件折叠，独立于 LLM
    工作视图。时间旅行视图必须能展示它 —— 且压缩事件（EpisodeArchived /
    ContextPruned）只裁工作视图，**不得让证据投影消失**（AC-5）。
    """

    def test_view_exposes_completed_and_unsuccessful_evidence(self):
        # Given a plan run where s1 completed (with a blob output_ref) and s2
        # ended UNSUCCESSFUL (semantic failure)
        events = [
            _event("r", 1, EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}),
            _event(
                "r",
                2,
                EventType.PLAN_CREATED,
                {
                    "plan_id": "p1",
                    "intent": "do",
                    "steps_summary": "s1 read; s2 fetch",
                    "layer_count": 1,
                    "steps": [
                        {"step_id": "s1", "tool_name": "read", "status": "pending"},
                        {"step_id": "s2", "tool_name": "http_request", "status": "pending"},
                    ],
                },
            ),
            _event("r", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "read"}),
            _event("r", 4, EventType.TOOL_CALLED, {"tool_call_id": "tc1", "tool_name": "read", "input": {"x": 1}}),
            _event(
                "r",
                5,
                EventType.TOOL_COMPLETED,
                {
                    "tool_call_id": "tc1",
                    "tool_name": "read",
                    "output": {"ref": "a" * 64 + ".json", "sha256": "abc", "summary": "big", "bytes": 5, "truncated": False},
                    "duration_ms": 3,
                    "step_id": "s1",
                },
            ),
            _event(
                "r",
                6,
                EventType.DAG_STEP_COMPLETED,
                {"plan_id": "p1", "step_id": "s1", "output_summary": "big", "status": "completed", "tool_call_id": "tc1"},
            ),
            _event("r", 7, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s2", "tool_name": "http_request"}),
            _event("r", 8, EventType.TOOL_CALLED, {"tool_call_id": "tc2", "tool_name": "http_request", "input": {}}),
            _event(
                "r",
                9,
                EventType.TOOL_COMPLETED,
                {
                    "tool_call_id": "tc2",
                    "tool_name": "http_request",
                    "output": {"status_code": 503},
                    "duration_ms": 5,
                    "result_type": "unsuccessful",
                    "error": "upstream 503",
                    "step_id": "s2",
                },
            ),
            _event(
                "r",
                10,
                EventType.DAG_STEP_COMPLETED,
                {
                    "plan_id": "p1",
                    "step_id": "s2",
                    "output_summary": "",
                    "status": "unsuccessful",
                    "error": "upstream 503",
                    "tool_call_id": "tc2",
                },
            ),
        ]
        view = project_state_view(events, at_seq=10, latest_seq=10)
        # Then the evidence projection is exposed as a first-class surface
        by_id = {ev.step_id: ev for ev in view.step_evidence}
        assert set(by_id) == {"s1", "s2"}

        s1 = by_id["s1"]
        assert s1.exec_state == "completed"
        assert s1.step_normal is True
        assert s1.tool_call_id == "tc1"
        assert s1.output_ref == "a" * 64 + ".json"
        assert s1.output_summary == "big"

        s2 = by_id["s2"]
        assert s2.exec_state == "unsuccessful"
        assert s2.step_normal is False
        assert s2.tool_call_id == "tc2"
        assert s2.error == "upstream 503"

    def test_evidence_survives_context_prune_of_working_view(self):
        # Given a run whose early TOOL events were pruned by ContextPruned
        # (compression trims only the LLM working view: tool_results/thoughts)
        events = [
            _event("r", 1, EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}),
            _event(
                "r",
                2,
                EventType.PLAN_CREATED,
                {
                    "plan_id": "p1",
                    "intent": "do",
                    "steps_summary": "s1",
                    "layer_count": 1,
                    "steps": [{"step_id": "s1", "tool_name": "read", "status": "pending"}],
                },
            ),
            _event("r", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "read"}),
            _event("r", 4, EventType.TOOL_CALLED, {"tool_call_id": "tc1", "tool_name": "read", "input": {"x": 1}}),
            _event(
                "r",
                5,
                EventType.TOOL_COMPLETED,
                {"tool_call_id": "tc1", "tool_name": "read", "output": "content", "duration_ms": 2, "step_id": "s1"},
            ),
            _event(
                "r",
                6,
                EventType.DAG_STEP_COMPLETED,
                {"plan_id": "p1", "step_id": "s1", "output_summary": "ok", "status": "completed", "tool_call_id": "tc1"},
            ),
            _event(
                "r",
                7,
                EventType.CONTEXT_PRUNED,
                {"pruned_event_refs": [4, 5], "pruned_token_count": 900, "pruned_seq_count": 2},
            ),
            _event("r", 8, EventType.RUN_COMPLETED, {"result_summary": "done"}),
        ]
        # When projected after the prune at latest seq
        view = project_state_view(events, at_seq=8, latest_seq=8)
        # Then the working view (tool_results) was pruned...
        assert view.tool_results == []
        # ...but the trusted evidence projection is NOT trimmed (AC-5): the step
        # still shows completed with its tool_call_id and attached output.
        assert len(view.step_evidence) == 1
        ev = view.step_evidence[0]
        assert ev.step_id == "s1"
        assert ev.exec_state == "completed"
        assert ev.step_normal is True
        assert ev.tool_call_id == "tc1"
        assert ev.output == "content"

    def test_evidence_at_midpoint_shows_running_not_terminal(self):
        # Given reconstruction as-of the step-started event (before completion)
        events = [
            _event("r", 1, EventType.RUN_STARTED, {"intent": "do", "context_snapshot": {}}),
            _event(
                "r",
                2,
                EventType.PLAN_CREATED,
                {
                    "plan_id": "p1",
                    "intent": "do",
                    "steps_summary": "s1",
                    "layer_count": 1,
                    "steps": [{"step_id": "s1", "tool_name": "read", "status": "pending"}],
                },
            ),
            _event("r", 3, EventType.DAG_STEP_STARTED, {"plan_id": "p1", "step_id": "s1", "tool_name": "read"}),
        ]
        view = project_state_view(events, at_seq=3, latest_seq=3)
        assert len(view.step_evidence) == 1
        assert view.step_evidence[0].exec_state == "running"
        assert view.step_evidence[0].step_normal is False

    def test_view_without_steps_has_empty_evidence(self):
        events = [_event("r", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}})]
        view = project_state_view(events, at_seq=1, latest_seq=1)
        assert view.step_evidence == []


class TestDiffStates:
    def test_diff_highlights_status_transition_and_failed_step(self):
        # Given a healthy midpoint (seq 6) and the failed terminal (seq 11)
        events = _failed_plan_run()
        # When diffing the two reconstructed states
        diff = diff_states(events, from_seq=6, to_seq=11)
        # Then the run-status change is prominently captured
        assert diff.status_change is not None
        assert diff.status_change.from_status == "running"
        assert diff.status_change.to_status == "failed"
        # And the step that flipped to failed is reported
        changed = {c.step_id: c for c in diff.steps_changed}
        assert "s2" in changed
        assert changed["s2"].to_status == "failed"
        # The already-completed s1 is NOT reported as changed
        assert "s1" not in changed
        # Guardrail that fired in range is surfaced
        assert any(g.guardrail_id == "no_write_outside_workspace" for g in diff.guardrails_triggered)
        # Events in the (6, 11] window are listed
        assert [e.seq for e in diff.events_in_range] == [7, 8, 9, 10, 11]

    def test_diff_no_status_change_returns_none_status_change(self):
        events = _failed_plan_run()
        diff = diff_states(events, from_seq=3, to_seq=6)
        assert diff.status_change is None
        # s1 moved started -> completed within the window
        changed = {c.step_id: c for c in diff.steps_changed}
        assert changed["s1"].to_status == "completed"

    def test_diff_captures_error_appearance(self):
        events = _failed_plan_run()
        diff = diff_states(events, from_seq=6, to_seq=11)
        assert diff.error_change is not None
        assert diff.error_change.from_error is None
        assert "s2 failed" in diff.error_change.to_error

    def test_diff_rejects_inverted_range(self):
        events = _failed_plan_run()
        with pytest.raises(ValueError, match="from_seq"):
            diff_states(events, from_seq=11, to_seq=6)
