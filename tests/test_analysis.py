"""Tests for AnalysisService and analysis API endpoints.

Coverage targets:
  1. get_dashboard — aggregation correctness (empty / multi-run / time window)
  2. get_tool_stats — per-tool grouping, success/failure/guardrail counts
  3. get_guardrail_stats — guardrail_id grouping, tools_affected
  4. get_run_tool_traces — tool_call_id linking, guardrail merging, retryable info
  5. get_run_timeline — cursor pagination, has_more, limit
  6. get_run_analysis — summary fields, 404 for nonexistent
  7. Edge cases — empty store, empty time window, events without tool_call_id
"""

from __future__ import annotations

import json
import time

import pytest

from harness.analysis.service import AnalysisService
from harness.models.events import (
    AgentThoughtPayload,
    Event,
    EventType,
    GuardrailTriggeredPayload,
    RunStartedPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolTimeoutPayload,
)
from harness.storage.event_store import EventStore


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def svc(store: EventStore) -> AnalysisService:
    return AnalysisService(store)


async def _seed_run(
    store: EventStore,
    run_id: str,
    intent: str = "test",
    tool_results: list[tuple[str, str]] | None = None,
    guardrails: list[tuple[str, str, str]] | None = None,
    token_counts: list[int] | None = None,
):
    """Helper: seed a complete run with events.

    tool_results: list of (tool_name, status) where status is one of
                  'completed', 'failed', 'timeout'
    """
    await store.append_event(run_id, EventType.RUN_STARTED, RunStartedPayload(intent=intent).model_dump())

    tool_results = tool_results or []
    guardrails = guardrails or []
    token_counts = token_counts or []

    for i, tc in enumerate(token_counts):
        await store.append_event(run_id, EventType.AGENT_THOUGHT, AgentThoughtPayload(thought=f"t{i}", token_count=tc).model_dump())

    for idx, (tn, status) in enumerate(tool_results):
        tid = f"call_{idx}"
        await store.append_event(run_id, EventType.TOOL_CALLED, ToolCalledPayload(tool_call_id=tid, tool_name=tn, input={"i": idx}).model_dump())
        if status == "completed":
            await store.append_event(run_id, EventType.TOOL_COMPLETED, ToolCompletedPayload(tool_call_id=tid, tool_name=tn, output={"ok": True}, duration_ms=100 * (idx + 1)).model_dump())
        elif status == "failed":
            await store.append_event(run_id, EventType.TOOL_FAILED, ToolFailedPayload(tool_call_id=tid, tool_name=tn, error="err", retryable=True).model_dump())
        elif status == "timeout":
            await store.append_event(run_id, EventType.TOOL_TIMEOUT, ToolTimeoutPayload(tool_call_id=tid, tool_name=tn, timeout_ms=5000).model_dump())

    for tn, gid, reason in guardrails:
        await store.append_event(run_id, EventType.GUARDRAIL_TRIGGERED, GuardrailTriggeredPayload(tool_call_id="call_0", tool_name=tn, guardrail_id=gid, reason=reason).model_dump())

    if tool_results:
        await store.append_event(run_id, EventType.RUN_COMPLETED, {"result_summary": "done"})
    else:
        await store.append_event(run_id, EventType.RUN_FAILED, {"final_error": "no tools", "event_count": 0})


# ── Dashboard ────────────────────────────────────────────────────────────


class TestDashboard:
    async def test_empty_store(self, svc: AnalysisService):
        d = await svc.get_dashboard()
        o = d.overview
        assert o.total_runs == 0
        assert o.total_events == 0
        assert o.total_tool_calls == 0
        assert o.total_tool_failures == 0
        assert o.total_guardrail_triggers == 0
        assert o.total_tokens_consumed == 0
        assert o.avg_tool_success_rate == 0.0

    async def test_single_completed_run(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", intent="test", tool_results=[("http", "completed"), ("http", "completed")], token_counts=[100, 200])
        d = await svc.get_dashboard()
        o = d.overview
        assert o.total_runs == 1
        assert o.completed_runs == 1
        assert o.running_runs == 0
        assert o.total_tool_calls == 2
        assert o.total_tool_failures == 0
        assert o.total_tokens_consumed == 300
        assert o.avg_tool_success_rate == 1.0

    async def test_multi_run_aggregation(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", intent="a", tool_results=[("http", "completed"), ("http", "failed")], token_counts=[50])
        await _seed_run(store, "r2", intent="b", tool_results=[("file_op", "completed")], token_counts=[30])
        await _seed_run(store, "r3", intent="c")  # failed (no tools)

        d = await svc.get_dashboard()
        o = d.overview
        assert o.total_runs == 3
        assert o.completed_runs == 2
        assert o.failed_runs == 1
        assert o.total_tool_calls == 3
        assert o.total_tool_failures == 1
        assert o.total_tokens_consumed == 80
        assert o.avg_tool_success_rate == round(2 / 3, 2)

    async def test_time_window_excludes_old_events(self, store: EventStore, svc: AnalysisService):
        now = time.time()
        old_ts = now - 100000

        await _seed_run(store, "new", intent="new", tool_results=[("http", "completed")], token_counts=[20])

        d = await svc.get_dashboard(since=now - 86400)
        assert d.overview.total_runs == 1
        assert d.overview.total_tokens_consumed == 20

        # Run with far-future window excludes all events
        d2 = await svc.get_dashboard(since=now + 86400, until=now + 172800)
        assert d2.overview.total_runs == 0
        assert d2.overview.total_events == 0


# ── Tool Stats ────────────────────────────────────────────────────────────


class TestToolStats:
    async def test_empty(self, svc: AnalysisService):
        ts = await svc.get_tool_stats()
        assert ts.tools == []

    async def test_groups_by_tool_name(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[
            ("http", "completed"), ("http", "completed"), ("http", "failed"),
            ("file_op", "completed"), ("file_op", "timeout"),
        ])
        ts = await svc.get_tool_stats()
        tools = {t.tool_name: t for t in ts.tools}

        assert "http" in tools
        assert tools["http"].call_count == 3
        assert tools["http"].success_count == 2
        assert tools["http"].failure_count == 1
        assert tools["http"].timeout_count == 0

        assert "file_op" in tools
        assert tools["file_op"].call_count == 2
        assert tools["file_op"].success_count == 1
        assert tools["file_op"].timeout_count == 1

    async def test_guardrail_blocked_count(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "completed")], guardrails=[("http", "rate_limit", "too fast")])
        ts = await svc.get_tool_stats()
        assert ts.tools[0].guardrail_blocked_count == 1

    async def test_avg_duration(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[
            ("http", "completed"), ("http", "completed"),
        ])
        ts = await svc.get_tool_stats()
        # durations: 100ms (idx 0), 200ms (idx 1)
        assert ts.tools[0].avg_duration_ms == 150.0


# ── Guardrail Stats ───────────────────────────────────────────────────────


class TestGuardrailStats:
    async def test_empty(self, svc: AnalysisService):
        gs = await svc.get_guardrail_stats()
        assert gs.guardrails == []

    async def test_groups_by_guardrail_id(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "completed")],
                        guardrails=[("http", "rate_limit", "too fast"), ("http", "destructive_op", "delete detected")])
        await _seed_run(store, "r2", tool_results=[("http", "completed")],
                        guardrails=[("http", "rate_limit", "still fast")])

        gs = await svc.get_guardrail_stats()
        by_id = {g.guardrail_id: g for g in gs.guardrails}

        assert "rate_limit" in by_id
        assert by_id["rate_limit"].trigger_count == 2
        assert "http" in by_id["rate_limit"].tools_affected

        assert "destructive_op" in by_id
        assert by_id["destructive_op"].trigger_count == 1


# ── Tool Traces ───────────────────────────────────────────────────────────


class TestToolTraces:
    async def test_empty_run(self, svc: AnalysisService):
        tt = await svc.get_run_tool_traces("nonexistent")
        assert tt.tool_traces == []

    async def test_linked_by_tool_call_id(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "completed"), ("file_op", "failed")])
        tt = await svc.get_run_tool_traces("r1")
        assert len(tt.tool_traces) == 2

        t0 = tt.tool_traces[0]
        assert t0.tool_call_id == "call_0"
        assert t0.tool_name == "http"
        assert t0.status == "completed"
        assert t0.output == {"ok": True}
        assert t0.duration_ms == 100
        assert t0.retryable.eligible is False

        t1 = tt.tool_traces[1]
        assert t1.tool_call_id == "call_1"
        assert t1.tool_name == "file_op"
        assert t1.status == "failed"
        assert t1.error == "err"
        assert t1.retryable.eligible is True
        assert t1.retryable.suggested_backoff_ms == 1000

    async def test_timeout_status(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "timeout")])
        tt = await svc.get_run_tool_traces("r1")
        assert tt.tool_traces[0].status == "timeout"
        assert "Timeout after" in tt.tool_traces[0].error
        assert tt.tool_traces[0].retryable.suggested_backoff_ms == 5000

    async def test_guardrail_merges_retryable(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "failed")],
                        guardrails=[("http", "rate_limit", "too fast")])
        tt = await svc.get_run_tool_traces("r1")
        t = tt.tool_traces[0]
        assert t.status == "failed"
        assert t.guardrail_id == "rate_limit"
        assert t.retryable.eligible is True
        assert t.retryable.suggested_backoff_ms == 1000
        assert t.retryable.requires_input_modification is True

    async def test_guardrail_only_status(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "completed")],
                        guardrails=[("http", "destructive_op", "blocked")])
        tt = await svc.get_run_tool_traces("r1")
        t = tt.tool_traces[0]
        assert t.status == "completed"  # not overwritten by guardrail
        assert t.guardrail_id == "destructive_op"
        assert t.retryable.eligible is False

    async def test_partial_trace_no_result_event(self, store: EventStore, svc: AnalysisService):
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="x").model_dump())
        await store.append_event("r1", EventType.TOOL_CALLED, ToolCalledPayload(tool_call_id="orphan", tool_name="http", input={}).model_dump())
        tt = await svc.get_run_tool_traces("r1")
        assert len(tt.tool_traces) == 1
        assert tt.tool_traces[0].status == "unknown"
        assert tt.tool_traces[0].retryable.eligible is False


# ── Timeline ──────────────────────────────────────────────────────────────


class TestTimeline:
    async def test_empty_run(self, svc: AnalysisService):
        tl = await svc.get_run_timeline("nonexistent")
        assert tl.timeline == []
        assert tl.has_more is False
        assert tl.next_cursor == 0

    async def test_returns_all_when_under_limit(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "completed")])
        tl = await svc.get_run_timeline("r1", limit=50, cursor=0)
        assert len(tl.timeline) == 4  # RunStarted + ToolCalled + ToolCompleted + RunCompleted
        assert tl.has_more is False
        assert tl.next_cursor == 0

    async def test_pagination_has_more(self, store: EventStore, svc: AnalysisService):
        # 5 tool results = 1 RunStarted + 5 ToolCalled + 5 ToolCompleted + 1 RunCompleted = 12 events
        await _seed_run(store, "r1", tool_results=[("http", "completed")] * 5)
        tl = await svc.get_run_timeline("r1", limit=5, cursor=0)
        assert len(tl.timeline) == 5
        assert tl.has_more is True
        assert tl.next_cursor == 5

        tl2 = await svc.get_run_timeline("r1", limit=5, cursor=5)
        assert len(tl2.timeline) == 5
        assert tl2.has_more is True
        assert tl2.next_cursor == 10

        tl3 = await svc.get_run_timeline("r1", limit=5, cursor=10)
        assert len(tl3.timeline) == 2
        assert tl3.has_more is False
        assert tl3.next_cursor == 0

    async def test_cursor_beyond_end_returns_empty(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[("http", "completed")])
        tl = await svc.get_run_timeline("r1", limit=10, cursor=999)
        assert len(tl.timeline) == 0
        assert tl.has_more is False


# ── Run Analysis ──────────────────────────────────────────────────────────


class TestRunAnalysis:
    async def test_nonexistent_returns_none(self, svc: AnalysisService):
        assert await svc.get_run_analysis("xxx") is None

    async def test_summary_fields(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", intent="hello", tool_results=[("http", "completed"), ("file_op", "failed")],
                        token_counts=[100, 200], guardrails=[("http", "rate_limit", "too fast")])
        summary = await svc.get_run_analysis("r1")
        assert summary is not None
        assert summary.run_id == "r1"
        assert summary.intent == "hello"
        assert summary.status in ("completed",)
        assert summary.event_count == 9
        assert summary.total_tokens == 300
        assert summary.total_duration_ms == 100  # only completed tools count
        assert summary.tool_trace_count == 2
        assert summary.guardrail_event_count == 1
        assert summary.feedback_count == 0
        assert summary.created_at is not None
        assert summary.completed_at is not None


# ── Edge cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    async def test_no_tool_events(self, store: EventStore, svc: AnalysisService):
        await store.append_event("r1", EventType.RUN_STARTED, RunStartedPayload(intent="noop").model_dump())
        await store.append_event("r1", EventType.RUN_FAILED, {"final_error": "x", "event_count": 1})

        d = await svc.get_dashboard()
        assert d.overview.total_runs == 1
        assert d.overview.total_tool_calls == 0

        ts = await svc.get_tool_stats()
        assert ts.tools == []

        gs = await svc.get_guardrail_stats()
        assert gs.guardrails == []

        tt = await svc.get_run_tool_traces("r1")
        assert tt.tool_traces == []

    async def test_retryable_info_all_statuses(self, store: EventStore, svc: AnalysisService):
        await _seed_run(store, "r1", tool_results=[
            ("t1", "completed"), ("t2", "failed"), ("t3", "timeout"),
        ])
        tt = await svc.get_run_tool_traces("r1")
        status_map = {t.tool_name: t for t in tt.tool_traces}
        assert status_map["t1"].retryable.eligible is False
        assert status_map["t2"].retryable.eligible is True
        assert status_map["t3"].retryable.eligible is True
