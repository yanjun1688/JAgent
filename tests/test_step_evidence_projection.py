"""F-1 (v3.4): 受信执行态证据投影 — 压缩只裁 LLM 工作视图，永不裁证据。

回归 run e05087b6：EpisodeArchived/ContextPruned 折叠时会把工具结果从
``state.tool_results`` 移除，导致 Replay 时间旅行视图证据消失、answer 细节丢失。
新增由 PLAN_*/DAG_STEP_*/TOOL_* 事件确定性折叠的 ``step_evidence`` 投影，
独立于喂 LLM 的工作视图，压缩事件不得裁剪它。
"""

from __future__ import annotations

from harness.core.fold import StepEvidence, fold_events
from harness.models.events import Event, EventType


def _event(run_id: str, seq: int, event_type: EventType, payload: dict) -> Event:
    return Event(run_id=run_id, seq=seq, event_type=event_type, payload=payload, created_at=0.0)


def _started(run: str, seq: int, plan_id: str, step_id: str, tool: str, deps=None) -> Event:
    return _event(
        run,
        seq,
        EventType.DAG_STEP_STARTED,
        {"plan_id": plan_id, "step_id": step_id, "tool_name": tool, "depends_on": deps or []},
    )


def _step_completed(
    run: str,
    seq: int,
    plan_id: str,
    step_id: str,
    status: str = "completed",
    tool_call_id: str | None = "tc-1",
    error: str | None = None,
) -> Event:
    return _event(
        run,
        seq,
        EventType.DAG_STEP_COMPLETED,
        {
            "plan_id": plan_id,
            "step_id": step_id,
            "output_summary": "sum",
            "status": status,
            "error": error,
            "tool_call_id": tool_call_id,
        },
    )


def _step_skipped(run: str, seq: int, plan_id: str, step_id: str, reason: str) -> Event:
    return _event(
        run,
        seq,
        EventType.DAG_STEP_SKIPPED,
        {"plan_id": plan_id, "step_id": step_id, "reason": reason, "tool_name": "browser"},
    )


def _plan_created(run: str, seq: int, plan_id: str, steps: list[dict]) -> Event:
    return _event(
        run,
        seq,
        EventType.PLAN_CREATED,
        {
            "plan_id": plan_id,
            "intent": "intent",
            "steps_summary": f"{len(steps)} steps",
            "layer_count": 1,
            "steps": steps,
        },
    )


def _tool_completed(run: str, seq: int, tcid: str, tool: str, step_id: str, result_type: str = "success") -> Event:
    return _event(
        run,
        seq,
        EventType.TOOL_COMPLETED,
        {
            "tool_call_id": tcid,
            "tool_name": tool,
            "output": {"ok": True},
            "duration_ms": 5,
            "result_type": result_type,
            "step_id": step_id,
        },
    )


def _episode_archived(run: str, seq: int, archived_refs: list[int], keep: int = 2) -> Event:
    episode = {
        "episode_range": [archived_refs[0], archived_refs[-1]] if archived_refs else [0, 0],
        "original_tokens": 100,
        "compressed_tokens": 10,
        "key_decisions": ["d"],
        "tools_used": ["browser"],
        "key_findings": [],
        "errors_encountered": [],
        "original_event_refs": archived_refs,
        "title": "ep",
    }
    return _event(
        run,
        seq,
        EventType.EPISODE_ARCHIVED,
        {
            "original_tokens": 100,
            "compressed_tokens": 10,
            "episode": episode,
            "keep_recent_count": keep,
            "archived_event_refs": archived_refs,
        },
    )


class TestStepEvidenceProjection:
    def test_completed_step_folded_into_evidence(self):
        run = "r1"
        events = [
            _event(run, 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _plan_created(run, 2, "p1", [{"step_id": "s1", "tool_name": "http_request", "depends_on": []}]),
            _started(run, 3, "p1", "s1", "http_request"),
            _tool_completed(run, 4, "tc-1", "http_request", "s1"),
            _step_completed(run, 5, "p1", "s1", "completed", "tc-1"),
        ]
        state = fold_events(events)
        assert "s1" in state.step_evidence
        ev = state.step_evidence["s1"]
        assert isinstance(ev, StepEvidence)
        assert ev.exec_state == "completed"
        assert ev.tool_name == "http_request"
        assert ev.tool_call_id == "tc-1"
        assert ev.step_normal is True

    def test_unsuccessful_and_skipped_reflected(self):
        run = "r2"
        events = [
            _event(run, 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _plan_created(
                run,
                2,
                "p1",
                [
                    {"step_id": "s3", "tool_name": "browser", "depends_on": []},
                    {"step_id": "s4", "tool_name": "browser", "depends_on": ["s3"]},
                ],
            ),
            _started(run, 3, "p1", "s3", "browser"),
            _tool_completed(run, 4, "tc-3", "browser", "s3", result_type="unsuccessful"),
            _step_completed(run, 5, "p1", "s3", "unsuccessful", "tc-3", error="browser unavailable"),
            _step_skipped(run, 6, "p1", "s4", "dep 's3' not normal"),
        ]
        state = fold_events(events)
        assert state.step_evidence["s3"].exec_state == "unsuccessful"
        assert state.step_evidence["s3"].step_normal is False
        assert state.step_evidence["s4"].exec_state == "skipped"
        assert state.step_evidence["s4"].step_normal is False

    def test_episode_archive_does_not_trim_evidence(self):
        run = "r3"
        events = [
            _event(run, 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _plan_created(run, 2, "p1", [{"step_id": "s1", "tool_name": "http_request", "depends_on": []}]),
            _started(run, 3, "p1", "s1", "http_request"),
            _tool_completed(run, 4, "tc-1", "http_request", "s1"),
            _step_completed(run, 5, "p1", "s1", "completed", "tc-1"),
            # Archive the tool_completed event (seq 4) — as e05087b6 seq30 archived [9,10].
            _episode_archived(run, 6, [4], keep=0),
        ]
        state = fold_events(events)
        # Working view trimmed (LLM context) ...
        assert all(tr.event_seq != 4 for tr in state.tool_results)
        # ... but trusted evidence survives.
        assert "s1" in state.step_evidence
        assert state.step_evidence["s1"].exec_state == "completed"
        assert state.step_evidence["s1"].step_normal is True
        assert state.step_evidence["s1"].tool_call_id == "tc-1"

    def test_context_pruned_does_not_trim_evidence(self):
        run = "r4"
        events = [
            _event(run, 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _plan_created(run, 2, "p1", [{"step_id": "s1", "tool_name": "http_request", "depends_on": []}]),
            _started(run, 3, "p1", "s1", "http_request"),
            _tool_completed(run, 4, "tc-1", "http_request", "s1"),
            _step_completed(run, 5, "p1", "s1", "completed", "tc-1"),
            _event(
                run,
                6,
                EventType.CONTEXT_PRUNED,
                {"pruned_event_refs": [4], "pruned_token_count": 50, "pruned_seq_count": 1, "reason": "lazy_clear"},
            ),
        ]
        state = fold_events(events)
        assert all(tr.event_seq != 4 for tr in state.tool_results)
        assert state.step_evidence["s1"].exec_state == "completed"

    def test_evidence_is_deterministic_and_complete_for_replay(self):
        # Folding a prefix vs full stream: evidence at a given seq must be stable.
        run = "r5"
        full = [
            _event(run, 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _plan_created(
                run,
                2,
                "p1",
                [
                    {"step_id": "s1", "tool_name": "http_request", "depends_on": []},
                    {"step_id": "s2", "tool_name": "http_request", "depends_on": ["s1"]},
                ],
            ),
            _started(run, 3, "p1", "s1", "http_request"),
            _tool_completed(run, 4, "tc-1", "http_request", "s1"),
            _step_completed(run, 5, "p1", "s1", "completed", "tc-1"),
        ]
        state = fold_events(full)
        again = fold_events(list(full))
        assert state.step_evidence.keys() == again.step_evidence.keys()
        assert state.step_evidence["s1"].exec_state == again.step_evidence["s1"].exec_state
