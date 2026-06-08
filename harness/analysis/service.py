from __future__ import annotations

import json
import time
from collections import defaultdict

from harness.core.fold import RunStatus, fold_events
from harness.core.logger import guard_logger
from harness.models.events import (
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
    Event,
    EventType,
    GuardrailTriggeredPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolTimeoutPayload,
)
from harness.storage.event_store import EventStore

from harness.analysis.schemas import (
    DashboardOverview,
    DashboardResponse,
    GuardrailStatItem,
    GuardrailStatsResponse,
    ParsedEventDetail,
    RetryableInfo,
    RunAnalysisSummary,
    TimelineResponse,
    ToolStatItem,
    ToolStatsResponse,
    ToolTraceItem,
    ToolTracesResponse,
)

_log = guard_logger("analysis")

_TYPE_TOOL_LIFECYCLE = {
    EventType.TOOL_CALLED,
    EventType.TOOL_COMPLETED,
    EventType.TOOL_FAILED,
    EventType.TOOL_TIMEOUT,
    EventType.GUARDRAIL_TRIGGERED,
}

_TYPE_TERMINAL = {EventType.RUN_COMPLETED, EventType.RUN_FAILED}


class AnalysisService:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    # ── Dashboard ────────────────────────────────────────────────

    async def get_dashboard(
        self, since: float | None = None, until: float | None = None
    ) -> DashboardResponse:
        _t0 = time.monotonic()
        until = until or time.time()
        since = since or 0

        # 2 queries instead of 4: type counts + merged (distinct_runs + token_sum)
        type_rows = await self._store.execute_query(
            "SELECT event_type, COUNT(*) as cnt FROM events WHERE created_at >= ? AND created_at <= ? GROUP BY event_type",
            (since, until),
        )
        type_counts: dict[str, int] = {r["event_type"]: r["cnt"] for r in type_rows}

        agg = await self._store.execute_query_one(
            "SELECT COUNT(DISTINCT run_id) as run_count, COALESCE(SUM(CASE WHEN event_type = ? THEN json_extract(payload, '$.token_count') ELSE 0 END), 0) as token_sum FROM events WHERE created_at >= ? AND created_at <= ?",
            (EventType.AGENT_THOUGHT.value, since, until),
        )
        total_runs = agg["run_count"] if agg else 0
        total_tokens = agg["token_sum"] if agg else 0

        total_events = sum(type_counts.values())
        total_tool_calls = type_counts.get(EventType.TOOL_CALLED.value, 0)
        total_failures = (
            type_counts.get(EventType.TOOL_FAILED.value, 0)
            + type_counts.get(EventType.TOOL_TIMEOUT.value, 0)
        )
        total_guardrails = type_counts.get(EventType.GUARDRAIL_TRIGGERED.value, 0)
        completed = type_counts.get(EventType.RUN_COMPLETED.value, 0)
        failed = type_counts.get(EventType.RUN_FAILED.value, 0)
        running = max(0, total_runs - completed - failed)
        paused = type_counts.get(EventType.RUN_PAUSED.value, 0)

        total_tool_results = type_counts.get(EventType.TOOL_COMPLETED.value, 0) + total_failures
        avg_rate = round(
            type_counts.get(EventType.TOOL_COMPLETED.value, 0) / total_tool_results, 2
        ) if total_tool_results > 0 else 0.0

        _log.info("dashboard since=%.0f until=%.0f runs=%d events=%d tools=%d failures=%d guardrails=%d tokens=%d (%.0fms)",
                  since, until, total_runs, total_events, total_tool_calls, total_failures,
                  total_guardrails, total_tokens, (time.monotonic() - _t0) * 1000)

        return DashboardResponse(
            overview=DashboardOverview(
                total_runs=total_runs,
                running_runs=running,
                paused_runs=paused,
                completed_runs=completed,
                failed_runs=failed,
                total_events=total_events,
                total_tool_calls=total_tool_calls,
                total_tool_failures=total_failures,
                total_guardrail_triggers=total_guardrails,
                total_tokens_consumed=total_tokens,
                avg_tool_success_rate=avg_rate,
            )
        )

    # ── Tool Stats ───────────────────────────────────────────────

    async def get_tool_stats(
        self, since: float | None = None, until: float | None = None
    ) -> ToolStatsResponse:
        _t0 = time.monotonic()
        until = until or time.time()
        since = since or 0

        rows = await self._fetch_events_by_types(_TYPE_TOOL_LIFECYCLE, since, until)
        events = [self._row_to_event(r) for r in rows]

        stats: dict[str, dict] = {}
        for e in events:
            tn = e.payload.get("tool_name", "?")
            if tn not in stats:
                stats[tn] = {"call_count": 0, "success_count": 0, "failure_count": 0, "timeout_count": 0, "guardrail_count": 0, "total_duration": 0, "result_count": 0}

            if e.event_type == EventType.TOOL_CALLED:
                stats[tn]["call_count"] += 1
            elif e.event_type == EventType.TOOL_COMPLETED:
                stats[tn]["result_count"] += 1
                stats[tn]["success_count"] += 1
                stats[tn]["total_duration"] += e.payload.get("duration_ms", 0)
            elif e.event_type == EventType.TOOL_FAILED:
                stats[tn]["result_count"] += 1
                stats[tn]["failure_count"] += 1
            elif e.event_type == EventType.TOOL_TIMEOUT:
                stats[tn]["result_count"] += 1
                stats[tn]["timeout_count"] += 1
            elif e.event_type == EventType.GUARDRAIL_TRIGGERED:
                stats[tn]["guardrail_count"] += 1

        items = []
        for name, s in sorted(stats.items(), key=lambda x: -x[1]["call_count"]):
            items.append(ToolStatItem(
                tool_name=name,
                call_count=s["call_count"],
                success_count=s["success_count"],
                failure_count=s["failure_count"],
                timeout_count=s["timeout_count"],
                guardrail_blocked_count=s["guardrail_count"],
                avg_duration_ms=round(s["total_duration"] / s["success_count"], 1) if s["success_count"] > 0 else 0.0,
            ))

        _log.info("tool_stats since=%.0f until=%.0f tools=%d (%.0fms)",
                  since, until, len(items), (time.monotonic() - _t0) * 1000)
        return ToolStatsResponse(tools=items)

    # ── Guardrail Stats ──────────────────────────────────────────

    async def get_guardrail_stats(
        self, since: float | None = None, until: float | None = None
    ) -> GuardrailStatsResponse:
        _t0 = time.monotonic()
        until = until or time.time()
        since = since or 0

        rows = await self._fetch_events_by_types({EventType.GUARDRAIL_TRIGGERED}, since, until)
        events = [self._row_to_event(r) for r in rows]

        stats: dict[str, dict] = {}
        for e in events:
            p = GuardrailTriggeredPayload(**e.payload)
            if p.guardrail_id not in stats:
                stats[p.guardrail_id] = {"count": 0, "tools": set(), "last_reason": None}
            stats[p.guardrail_id]["count"] += 1
            stats[p.guardrail_id]["tools"].add(p.tool_name)
            stats[p.guardrail_id]["last_reason"] = p.reason

        items = [
            GuardrailStatItem(
                guardrail_id=gid,
                trigger_count=s["count"],
                tools_affected=sorted(s["tools"]),
                recent_reason=s["last_reason"],
            )
            for gid, s in sorted(stats.items(), key=lambda x: -x[1]["count"])
        ]

        _log.info("guardrail_stats since=%.0f until=%.0f guardrails=%d (%.0fms)",
                  since, until, len(items), (time.monotonic() - _t0) * 1000)
        return GuardrailStatsResponse(guardrails=items)

    # ── Run Analysis ─────────────────────────────────────────────

    async def get_run_analysis(self, run_id: str) -> RunAnalysisSummary | None:
        _t0 = time.monotonic()
        events = await self._store.get_events(run_id)
        if not events:
            _log.info("run_analysis run=%s — not found (%.0fms)", run_id, (time.monotonic() - _t0) * 1000)
            return None

        state = fold_events(events)

        total_tokens = sum(
            e.payload.get("token_count", 0)
            for e in events if e.event_type == EventType.AGENT_THOUGHT
        )
        total_duration = sum(
            e.payload.get("duration_ms", 0)
            for e in events if e.event_type == EventType.TOOL_COMPLETED
        )

        tool_call_count = sum(1 for e in events if e.event_type == EventType.TOOL_CALLED)
        guardrail_count = sum(1 for e in events if e.event_type == EventType.GUARDRAIL_TRIGGERED)
        feedback_count = sum(1 for e in events if e.event_type == EventType.FEEDBACK_INJECTED)

        created_at = events[0].created_at
        completed_at = None
        if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            for e in reversed(events):
                if e.event_type in _TYPE_TERMINAL:
                    completed_at = e.created_at
                    break

        _log.info("run_analysis run=%s status=%s events=%d tokens=%d (%.0fms)",
                  run_id, state.status.value, len(events), total_tokens,
                  (time.monotonic() - _t0) * 1000)

        return RunAnalysisSummary(
            run_id=run_id,
            intent=state.intent,
            status=state.status.value,
            event_count=len(events),
            total_tokens=total_tokens,
            total_duration_ms=total_duration,
            created_at=created_at,
            completed_at=completed_at,
            tool_trace_count=tool_call_count,
            guardrail_event_count=guardrail_count,
            feedback_count=feedback_count,
        )

    async def get_run_timeline(
        self, run_id: str, limit: int = 50, cursor: int = 0
    ) -> TimelineResponse:
        _t0 = time.monotonic()
        all_events = await self._store.get_events(run_id)
        if not all_events:
            _log.info("timeline run=%s — empty (%.0fms)", run_id, (time.monotonic() - _t0) * 1000)
            return TimelineResponse(timeline=[])

        total = len(all_events)
        start = cursor
        end = min(start + limit, total)
        page = all_events[start:end]

        items = [self._build_parsed_event(e) for e in page]

        next_cursor = end if end < total else 0
        has_more = end < total

        _log.info("timeline run=%s cursor=%d limit=%d returned=%d total=%d has_more=%s (%.0fms)",
                  run_id, cursor, limit, len(items), total, has_more,
                  (time.monotonic() - _t0) * 1000)

        return TimelineResponse(timeline=items, next_cursor=next_cursor, has_more=has_more)

    async def get_run_tool_traces(self, run_id: str) -> ToolTracesResponse:
        _t0 = time.monotonic()
        events = await self._store.get_events(run_id)

        calls: dict[str, dict] = {}
        guardrails: dict[str, dict] = {}

        for e in events:
            if e.event_type == EventType.TOOL_CALLED:
                p = ToolCalledPayload(**e.payload)
                calls[p.tool_call_id] = {
                    "tool_call_id": p.tool_call_id,
                    "tool_name": p.tool_name,
                    "called_seq": e.seq,
                    "input": p.input,
                    "idempotency_key": p.idempotency_key,
                    "status": "unknown",
                }

            elif e.event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED, EventType.TOOL_TIMEOUT):
                tid = e.payload.get("tool_call_id")
                if tid not in calls:
                    continue
                entry = calls[tid]
                if e.event_type == EventType.TOOL_COMPLETED:
                    p = ToolCompletedPayload(**e.payload)
                    entry.update(status="completed", completed_seq=e.seq, output=p.output, duration_ms=p.duration_ms)
                elif e.event_type == EventType.TOOL_FAILED:
                    p = ToolFailedPayload(**e.payload)
                    entry.update(status="failed", completed_seq=e.seq, error=p.error)
                elif e.event_type == EventType.TOOL_TIMEOUT:
                    p = ToolTimeoutPayload(**e.payload)
                    entry.update(status="timeout", completed_seq=e.seq, error=f"Timeout after {p.timeout_ms}ms", duration_ms=p.timeout_ms)

            elif e.event_type == EventType.GUARDRAIL_TRIGGERED:
                p = GuardrailTriggeredPayload(**e.payload)
                guardrails[p.tool_call_id] = {
                    "tool_call_id": p.tool_call_id,
                    "tool_name": p.tool_name,
                    "called_seq": e.seq,
                    "guardrail_id": p.guardrail_id,
                    "guardrail_reason": p.reason,
                }

        traces = []
        for tid, c in calls.items():
            g = guardrails.get(tid, {})
            status = c.get("status", g.get("status", "unknown"))
            if g and status == "unknown":
                status = "guardrail_blocked"

            retryable = self._build_retryable(status, c.get("error"))
            if g and status in ("failed", "timeout"):
                retryable.requires_input_modification = True

            traces.append(ToolTraceItem(
                tool_call_id=tid,
                tool_name=c.get("tool_name", "?"),
                called_seq=c.get("called_seq"),
                input=c.get("input"),
                idempotency_key=c.get("idempotency_key"),
                status=status,
                completed_seq=c.get("completed_seq"),
                output=c.get("output"),
                error=c.get("error"),
                duration_ms=c.get("duration_ms", 0),
                guardrail_id=g.get("guardrail_id"),
                guardrail_reason=g.get("guardrail_reason"),
                retryable=retryable,
            ))

        # Guardrail-only entries: blocked before TOOL_CALLED was ever written
        for gid, g in guardrails.items():
            if gid not in calls:
                traces.append(ToolTraceItem(
                    tool_call_id=gid,
                    tool_name=g.get("tool_name", "?"),
                    called_seq=g.get("called_seq"),
                    status="guardrail_blocked",
                    guardrail_id=g.get("guardrail_id"),
                    guardrail_reason=g.get("guardrail_reason"),
                    retryable=self._build_retryable("guardrail_blocked"),
                ))

        _log.info("tool_traces run=%s traces=%d (%.0fms)",
                  run_id, len(traces), (time.monotonic() - _t0) * 1000)
        return ToolTracesResponse(tool_traces=traces)

    # ── Internal helpers ─────────────────────────────────────────

    async def _fetch_events_by_types(
        self, types: set[EventType], since: float, until: float
    ) -> list[dict]:
        type_names = [t.value for t in types]
        placeholders = ",".join("?" * len(type_names))
        return await self._store.execute_query(
            f"SELECT * FROM events WHERE event_type IN ({placeholders}) AND created_at >= ? AND created_at <= ? ORDER BY run_id, seq",
            (*type_names, since, until),
        )

    def _build_retryable(self, status: str, error: str | None = None) -> RetryableInfo:
        match status:
            case "completed":
                return RetryableInfo(eligible=False, ineligible_reason="already succeeded")
            case "failed":
                return RetryableInfo(eligible=True, suggested_backoff_ms=1000)
            case "timeout":
                return RetryableInfo(eligible=True, suggested_backoff_ms=5000)
            case "guardrail_blocked":
                return RetryableInfo(eligible=True, requires_input_modification=True)
            case _:
                return RetryableInfo(eligible=False, ineligible_reason=f"cannot retry status: {status}")

    def _build_parsed_event(self, event: Event) -> ParsedEventDetail:
        p = event.payload
        detail = ParsedEventDetail(
            run_id=event.run_id,
            seq=event.seq,
            event_type=event.event_type.value,
            created_at=event.created_at,
            payload=p,
        )

        match event.event_type:
            case EventType.TOOL_CALLED:
                detail.tool_call_id = p.get("tool_call_id")
                detail.tool_name = p.get("tool_name")
                detail.input = p.get("input")
                detail.idempotency_key = p.get("idempotency_key")
                detail.retryable = self._build_retryable("unknown")

            case EventType.TOOL_COMPLETED:
                detail.tool_call_id = p.get("tool_call_id")
                detail.tool_name = p.get("tool_name")
                detail.idempotency_key = p.get("idempotency_key")
                detail.duration_ms = p.get("duration_ms")
                detail.retryable = self._build_retryable("completed")

            case EventType.TOOL_FAILED:
                detail.tool_call_id = p.get("tool_call_id")
                detail.tool_name = p.get("tool_name")
                detail.input = p.get("input")
                detail.idempotency_key = p.get("idempotency_key")
                detail.error = p.get("error")
                detail.retryable = self._build_retryable("failed")

            case EventType.TOOL_TIMEOUT:
                detail.tool_call_id = p.get("tool_call_id")
                detail.tool_name = p.get("tool_name")
                detail.input = p.get("input")
                detail.idempotency_key = p.get("idempotency_key")
                detail.error = p.get("error")
                detail.duration_ms = p.get("timeout_ms")
                detail.retryable = self._build_retryable("timeout")

            case EventType.GUARDRAIL_TRIGGERED:
                detail.tool_call_id = p.get("tool_call_id")
                detail.tool_name = p.get("tool_name")
                detail.error = f"{p.get('guardrail_id')}: {p.get('reason')}"
                detail.retryable = self._build_retryable("guardrail_blocked")

            case EventType.CONFIRMATION_REQUESTED:
                detail.confirmation_id = p.get("confirmation_id")
                detail.tool_name = p.get("tool_name")
                detail.input = p.get("input")

            case EventType.CONFIRMATION_RECEIVED:
                detail.confirmation_id = p.get("confirmation_id")

        return detail

    @staticmethod
    def _row_to_event(row: dict) -> Event:
        return Event(
            run_id=row["run_id"],
            seq=row["seq"],
            event_type=EventType(row["event_type"]),
            payload=json.loads(row["payload"]),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )
