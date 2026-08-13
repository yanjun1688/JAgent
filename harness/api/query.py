"""Unified query endpoint — single entry point for frontend dashboard observability.

GET /api/v1/query?type=<type>&...

Supports 18 query types covering all backend data:
  - API-visible data (runs, run, events, dashboard, tool-stats, guardrail-stats,
    run-analysis, timeline, tool-traces)
  - Hidden engine state (tool-defs, schedulers, mcp, plans, system, ws-clients)
  - Feedback / monitoring / health (feedback, monitor, health)

Response shape:
  { type: str, data: [...], meta: { page, page_size, total, has_more } | null }

Include parameter (type=run only): embed sub-resources in _included field.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Query as FastQuery
from pydantic import BaseModel

from harness.analysis.service import AnalysisService
from harness.api.deps import HarnessAPI, get_hapi
from harness.core.fold import RunStatus, fold_events
from harness.core.logger import guard_logger
from harness.models.events import Event, EventType
from harness.models.tools import ToolDefinition

_log = guard_logger("query")

router = APIRouter(tags=["query"])


# ── Response models ──────────────────────────────────────────────


class QueryMeta(BaseModel):
    page: int
    page_size: int
    total: int
    has_more: bool


class QueryResponse(BaseModel):
    type: str
    data: Any
    meta: QueryMeta | None = None
    _included: dict[str, Any] | None = None


# ── Main dispatch ────────────────────────────────────────────────


@router.get("/api/v1/query")
async def query(
    type: str = FastQuery(..., alias="type"),
    run_id: str | None = FastQuery(None),
    include: str | None = FastQuery(None),
    page: int = FastQuery(1, ge=1),
    page_size: int = FastQuery(20, ge=1, le=100),
    since: float | None = FastQuery(None),
    until: float | None = FastQuery(None),
    hapi: HarnessAPI = Depends(get_hapi),
):
    dispatch: dict[str, Any] = {
        # API-visible data (facade over existing service layer)
        "runs": _query_runs,
        "run": _query_run,
        "events": _query_events,
        "dashboard": _query_dashboard,
        "tool-stats": _query_tool_stats,
        "guardrail-stats": _query_guardrail_stats,
        "run-analysis": _query_run_analysis,
        "timeline": _query_timeline,
        "tool-traces": _query_tool_traces,
        # Hidden engine state (new — no existing API)
        "tool-defs": _query_tool_defs,
        "schedulers": _query_schedulers,
        "mcp": _query_mcp,
        "plans": _query_plans,
        "system": _query_system,
        "ws-clients": _query_ws_clients,
        # Feedback / monitoring / health
        "feedback": _query_feedback,
        "monitor": _query_monitor,
        "health": _query_health,
    }

    handler = dispatch.get(type)
    if handler is None:
        valid = ", ".join(sorted(dispatch.keys()))
        raise HTTPException(400, f"Unknown type: '{type}'. Valid: {valid}")

    return await handler(hapi, run_id, include, page, page_size, since, until)


# ═══════════════════════════════════════════════════════════════
#  API-visible data handlers (facade over existing services)
# ═══════════════════════════════════════════════════════════════


async def _query_runs(hapi, run_id, include, page, page_size, since, until):
    offset = (page - 1) * page_size
    rows = await hapi.store.list_runs(limit=page_size, offset=offset)
    total = await hapi.store.total_run_count()

    if not rows:
        return QueryResponse(
            type="runs",
            data=[],
            meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=False),
        )

    run_ids = [r["run_id"] for r in rows]
    all_events = await hapi.store.get_events_for_runs(run_ids)
    grouped: dict[str, list] = defaultdict(list)
    for e in all_events:
        grouped[e.run_id].append(e)

    summaries = []
    for r in rows:
        rid = r["run_id"]
        events = grouped.get(rid, [])
        state = fold_events(events) if events else None
        if state is None:
            summaries.append(
                {
                    "run_id": rid,
                    "intent": "",
                    "status": "running",
                    "event_count": 0,
                    "tool_call_count": 0,
                    "tool_success_count": 0,
                    "tool_failure_count": 0,
                    "tool_unsuccessful_count": 0,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )
            continue
        summaries.append(
            {
                "run_id": rid,
                "intent": state.intent,
                "status": state.status.value,
                "event_count": len(events),
                "tool_call_count": len(state.tool_results),
                # v2.2 (D2/D3): tool_success_count 只计真正的成功（拿到东西）。
                # UNSUCCESSFUL（跑了没拿到）独立统计，不再算进 success。
                "tool_success_count": sum(1 for tr in state.tool_results if tr.status.value == "completed"),
                "tool_failure_count": sum(
                    1 for tr in state.tool_results if tr.status.value in ("failed", "timeout", "guardrail_blocked")
                ),
                "tool_unsuccessful_count": sum(1 for tr in state.tool_results if tr.status.value == "unsuccessful"),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )

    has_more = (offset + page_size) < total
    return QueryResponse(
        type="runs",
        data=summaries,
        meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=has_more),
    )


async def _query_run(hapi, run_id, include, page, page_size, since, until):
    if not run_id:
        raise HTTPException(400, "run_id required for type=run")
    events = await hapi.store.get_events(run_id)
    if not events:
        raise HTTPException(404, f"Run not found: {run_id}")
    state = fold_events(events)

    tool_stats: dict[str, dict] = {}
    for tr in state.tool_results:
        tn = tr.tool_name
        if tn not in tool_stats:
            tool_stats[tn] = {
                "call_count": 0,
                "completed": 0,
                "unsuccessful": 0,
                "failed": 0,
                "timeout": 0,
                "guardrail_blocked": 0,
            }
        tool_stats[tn]["call_count"] += 1
        sv = tr.status.value
        if sv == "completed":
            tool_stats[tn]["completed"] += 1
        elif sv == "unsuccessful":
            # v2.2 (D2/D3): UNSUCCESSFUL 独立成桶，不再混入 completed。
            tool_stats[tn]["unsuccessful"] += 1
        elif sv == "failed":
            tool_stats[tn]["failed"] += 1
        elif sv == "timeout":
            tool_stats[tn]["timeout"] += 1
        elif sv == "guardrail_blocked":
            tool_stats[tn]["guardrail_blocked"] += 1

    event_type_counts: dict[str, int] = {}
    for e in events:
        et = e.event_type.value
        event_type_counts[et] = event_type_counts.get(et, 0) + 1

    total_tokens = sum(e.payload.get("token_count", 0) for e in events if e.event_type == EventType.AGENT_THOUGHT)

    created_at = events[0].created_at
    completed_at = None
    if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
        for e in reversed(events):
            if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
                completed_at = e.created_at
                break

    data = {
        "run_id": run_id,
        "status": state.status.value,
        "intent": state.intent,
        "seq": state.seq,
        "event_count": len(events),
        "event_type_counts": event_type_counts,
        "total_tokens": total_tokens,
        "created_at": created_at,
        "completed_at": completed_at,
        "last_error": state.last_error,
        "summary": str(state.summary) if state.summary else None,
        "pause_reason": state.pause_reason,
        "tool_stats": tool_stats,
        "tool_results": [
            {
                "tool_call_id": tr.tool_call_id,
                "tool_name": tr.tool_name,
                "status": tr.status.value,
                "error": tr.error,
                "duration_ms": tr.duration_ms,
                "output": tr.output,
            }
            for tr in state.tool_results
        ],
        "thought_count": len(state.thought_history),
        "pending_confirmations": [
            {
                "confirmation_id": c.confirmation_id,
                "tool_name": c.tool_name,
                "tool_call_id": c.tool_call_id,
                "input": c.input,
                "risk_level": c.risk_level,
            }
            for c in state.pending_confirmations
        ],
        "latest_plan": state.latest_plan,
        "plan_history": state.plan_history,
        "completion_evidence": state.completion_evidence,
        "feedback_count": len(state.feedbacks),
        "checkpoint_seq": state.last_checkpoint_seq,
    }

    response = QueryResponse(type="run", data=data, meta=None)

    if include:
        included: dict[str, Any] = {}
        includes = [i.strip() for i in include.split(",") if i.strip()]
        _valid_includes = {"events", "timeline", "tool-traces", "run-analysis", "plans"}
        for inc in includes:
            if inc not in _valid_includes:
                continue
            inc_handler = _include_handlers.get(inc)
            if inc_handler:
                included[inc] = await inc_handler(hapi, run_id, None, 1, page_size, None, None)
        if included:
            response._included = included

    return response


async def _query_events(hapi, run_id, include, page, page_size, since, until):
    if not run_id:
        raise HTTPException(400, "run_id required for type=events")
    events = await hapi.store.get_events(run_id)
    if not events:
        raise HTTPException(404, f"Run not found: {run_id}")
    total = len(events)
    offset = (page - 1) * page_size
    page_events = events[offset : offset + page_size]

    data = [e.model_dump(mode="json") for e in page_events]
    return QueryResponse(
        type="events",
        data=data,
        meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=(offset + page_size) < total),
    )


async def _query_dashboard(hapi, run_id, include, page, page_size, since, until):
    service = AnalysisService(hapi.store)
    result = await service.get_dashboard(since=since, until=until)
    return QueryResponse(type="dashboard", data=result.model_dump(mode="json"), meta=None)


async def _query_tool_stats(hapi, run_id, include, page, page_size, since, until):
    service = AnalysisService(hapi.store)
    result = await service.get_tool_stats(since=since, until=until)
    return QueryResponse(type="tool-stats", data=result.model_dump(mode="json"), meta=None)


async def _query_guardrail_stats(hapi, run_id, include, page, page_size, since, until):
    service = AnalysisService(hapi.store)
    result = await service.get_guardrail_stats(since=since, until=until)
    return QueryResponse(type="guardrail-stats", data=result.model_dump(mode="json"), meta=None)


async def _query_run_analysis(hapi, run_id, include, page, page_size, since, until):
    if not run_id:
        raise HTTPException(400, "run_id required for type=run-analysis")
    service = AnalysisService(hapi.store)
    result = await service.get_run_analysis(run_id)
    if result is None:
        raise HTTPException(404, f"Run not found: {run_id}")
    return QueryResponse(type="run-analysis", data=result.model_dump(mode="json"), meta=None)


async def _query_timeline(hapi, run_id, include, page, page_size, since, until):
    if not run_id:
        raise HTTPException(400, "run_id required for type=timeline")
    cursor = (page - 1) * page_size
    service = AnalysisService(hapi.store)
    events = await hapi.store.get_events(run_id)
    if not events:
        raise HTTPException(404, f"Run not found: {run_id}")
    result = await service.get_run_timeline(run_id, limit=page_size, cursor=cursor)
    total = len(events)
    data = [item.model_dump(mode="json") for item in result.timeline]
    return QueryResponse(
        type="timeline",
        data=data,
        meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=result.has_more),
    )


async def _query_tool_traces(hapi, run_id, include, page, page_size, since, until):
    if not run_id:
        raise HTTPException(400, "run_id required for type=tool-traces")
    if not await hapi.store.get_events(run_id):
        raise HTTPException(404, f"Run not found: {run_id}")
    service = AnalysisService(hapi.store)
    result = await service.get_run_tool_traces(run_id)
    return QueryResponse(type="tool-traces", data=result.model_dump(mode="json"), meta=None)


# ═══════════════════════════════════════════════════════════════
#  Hidden engine state handlers (new — no existing API)
# ═══════════════════════════════════════════════════════════════


async def _query_tool_defs(hapi, run_id, include, page, page_size, since, until):
    if hapi.registry is None:
        return QueryResponse(type="tool-defs", data=[], meta=None)

    def _serialize_tool(td: ToolDefinition) -> dict:
        return td.model_dump(mode="json")

    all_tools = [{"tool_name": td.name, "definition": _serialize_tool(td)} for td in hapi.registry.list_tool_defs()]
    total = len(all_tools)
    offset = (page - 1) * page_size
    page_tools = all_tools[offset : offset + page_size]
    return QueryResponse(
        type="tool-defs",
        data=page_tools,
        meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=(offset + page_size) < total),
    )


async def _query_schedulers(hapi, run_id, include, page, page_size, since, until):
    data: list[dict] = []
    for rid, sched in list(hapi._schedulers.items()):
        try:
            events = await hapi.store.get_events(rid)
            state = fold_events(events)
        except Exception:
            continue
        data.append(
            {
                "run_id": rid,
                "status": state.status.value,
                "intent": state.intent,
                "seq": state.seq,
                "event_count": len(events),
                "last_error": state.last_error,
                "pause_reason": state.pause_reason,
                "is_active": sched.is_active(rid),
                "is_paused": sched.is_paused(rid),
                "config": {
                    "max_iterations": sched.config.max_iterations,
                    "max_consecutive_failures": sched.config.max_consecutive_failures,
                    "pause_timeout_ms": sched.config.pause_timeout_ms,
                    "confirm_timeout_ms": sched.config.confirm_timeout_ms,
                    "max_confirm_retries": sched.config.max_confirm_retries,
                },
                "tool_stats": _compute_scheduler_tool_stats(state),
                "latest_plan": state.latest_plan,
            }
        )

    total = len(data)
    offset = (page - 1) * page_size
    page_data = data[offset : offset + page_size]
    return QueryResponse(
        type="schedulers",
        data=page_data,
        meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=(offset + page_size) < total),
    )


def _compute_scheduler_tool_stats(state) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for tr in state.tool_results:
        tn = tr.tool_name
        if tn not in stats:
            stats[tn] = {
                "call_count": 0,
                "completed": 0,
                "unsuccessful": 0,
                "failed": 0,
                "timeout": 0,
                "guardrail_blocked": 0,
            }
        stats[tn]["call_count"] += 1
        sv = tr.status.value
        if sv == "completed":
            stats[tn]["completed"] += 1
        elif sv == "unsuccessful":
            # v2.2 (D2/D3): UNSUCCESSFUL 独立成桶，不再混入 completed。
            stats[tn]["unsuccessful"] += 1
        elif sv == "failed":
            stats[tn]["failed"] += 1
        elif sv == "timeout":
            stats[tn]["timeout"] += 1
        elif sv == "guardrail_blocked":
            stats[tn]["guardrail_blocked"] += 1
    return stats


async def _query_mcp(hapi, run_id, include, page, page_size, since, until):
    mcp = getattr(hapi, "mcp_manager", None)
    if mcp is None:
        return QueryResponse(type="mcp", data={"servers": [], "message": "No MCP manager configured"}, meta=None)

    connected_names = set(mcp.server_names)
    servers: list[dict] = []
    for cfg in mcp.config.servers:
        servers.append(
            {
                "name": cfg.name,
                "command": cfg.command,
                "url": cfg.url,
                "enabled": cfg.enabled,
                "auto_register_tools": cfg.auto_register_tools,
                "timeout_ms": cfg.timeout_ms,
                "connected": cfg.name in connected_names,
            }
        )
    return QueryResponse(type="mcp", data={"servers": servers, "connected_count": len(connected_names)}, meta=None)


async def _query_plans(hapi, run_id, include, page, page_size, since, until):
    if not run_id:
        raise HTTPException(400, "run_id required for type=plans")
    events = await hapi.store.get_events(run_id)
    if not events:
        raise HTTPException(404, f"Run not found: {run_id}")
    state = fold_events(events)
    return QueryResponse(
        type="plans",
        data={
            "run_id": run_id,
            "plan_history": state.plan_history,
            "latest_plan": state.latest_plan,
            "plan_boundary_seqs": state.plan_boundary_seqs,
        },
        meta=None,
    )


async def _query_system(hapi, run_id, include, page, page_size, since, until):
    llm_info: dict[str, Any] | None = None
    if hapi.llm_client is not None:
        llm_info = {"type": type(hapi.llm_client).__name__}
        if hasattr(hapi.llm_client, "model"):
            llm_info["model"] = hapi.llm_client.model
        if hasattr(hapi.llm_client, "base_url"):
            llm_info["base_url"] = hapi.llm_client.base_url
        if hasattr(hapi.llm_client, "calls"):
            llm_info["total_calls"] = len(hapi.llm_client.calls)

    registry_info: dict[str, Any] | None = None
    if hapi.registry is not None:
        registry_info = {
            "tool_count": len(hapi.registry),
            "tool_names": hapi.registry.tool_names,
        }

    scheduler_config_info = hapi.scheduler_config.__dict__ if hapi.scheduler_config else None

    return QueryResponse(
        type="system",
        data={
            "llm_client": llm_info,
            "tool_registry": registry_info,
            "scheduler_config": scheduler_config_info,
            "tool_defs_count": len(hapi.tool_defs),
        },
        meta=None,
    )


async def _query_ws_clients(hapi, run_id, include, page, page_size, since, until):
    if run_id:
        clients = hapi._ws_clients.get(run_id, [])
        data: dict[str, Any] = {"run_id": run_id, "connected_clients": len(clients)}
    else:
        data = {
            "total_connections": sum(len(v) for v in hapi._ws_clients.values()),
            "by_run": {rid: len(clients) for rid, clients in hapi._ws_clients.items()},
        }
    return QueryResponse(type="ws-clients", data=data, meta=None)


# ═══════════════════════════════════════════════════════════════
#  Feedback / Monitoring / Health handlers
# ═══════════════════════════════════════════════════════════════


async def _query_feedback(hapi, run_id, include, page, page_size, since, until):
    if run_id:
        events = await hapi.store.get_events(run_id)
        if not events:
            raise HTTPException(404, f"Run not found: {run_id}")
        feedback_events = [e for e in events if e.event_type == EventType.FEEDBACK_INJECTED]
    else:
        sql = """
            SELECT * FROM events
            WHERE event_type = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        count_sql = """
            SELECT COUNT(*) AS cnt FROM events
            WHERE event_type = ?
        """
        offset = (page - 1) * page_size
        rows = await hapi.store.execute_query(sql, [EventType.FEEDBACK_INJECTED.value, page_size, offset])
        total_row = await hapi.store.execute_query_one(count_sql, [EventType.FEEDBACK_INJECTED.value])
        total = total_row["cnt"] if total_row else 0
        feedback_events = [e for e in (_row_to_event(r) for r in rows) if e is not None]

    if run_id:
        total = len(feedback_events)
        offset = (page - 1) * page_size
        page_feedback = feedback_events[offset : offset + page_size]
    else:
        page_feedback = feedback_events

    data = [
        {
            "run_id": e.run_id,
            "seq": e.seq,
            "created_at": e.created_at,
            "feedback_id": e.payload.get("feedback_id", ""),
            "source": e.payload.get("source", "unknown"),
            "category": e.payload.get("category", "unknown"),
            "feedback_text": e.payload.get("feedback_text", ""),
            "priority": e.payload.get("priority", "medium"),
            "affected_tool": e.payload.get("affected_tool"),
            "error_type": e.payload.get("error_type"),
            "error_detail": e.payload.get("error_detail"),
            "suggestion": e.payload.get("suggestion"),
            "expires_at_seq": e.payload.get("expires_at_seq"),
            "resolves_feedback_id": e.payload.get("resolves_feedback_id"),
            "consumed_at_seq": e.payload.get("consumed_at_seq"),
        }
        for e in page_feedback
    ]
    return QueryResponse(
        type="feedback",
        data=data,
        meta=QueryMeta(page=page, page_size=page_size, total=total, has_more=(offset + page_size) < total),
    )


async def _query_monitor(hapi, run_id, include, page, page_size, since, until):
    monitor = getattr(hapi, "monitor", None)
    if monitor is None:
        return QueryResponse(type="monitor", data={"message": "No monitor attached"}, meta=None)
    state = monitor.get_state(run_id)
    return QueryResponse(type="monitor", data=state, meta=None)


async def _query_health(hapi, run_id, include, page, page_size, since, until):
    components: dict[str, dict] = {}

    # Store
    try:
        await hapi.store.total_run_count()
        components["store"] = {"status": "ok"}
    except Exception as exc:
        components["store"] = {"status": "error", "error": str(exc)}

    # Schedulers
    components["schedulers"] = {
        "status": "ok",
        "active_count": len(hapi._schedulers),
    }

    # WS clients
    ws_total = sum(len(v) for v in hapi._ws_clients.values())
    components["ws_clients"] = {
        "status": "ok",
        "total_connections": ws_total,
        "subscribed_runs": len(hapi._ws_clients),
    }

    # LLM client
    if hapi.llm_client is not None:
        model = getattr(hapi.llm_client, "model", "unknown")
        components["llm_client"] = {"status": "ok", "model": model}
    else:
        components["llm_client"] = {"status": "missing"}

    # MCP manager
    if hapi.mcp_manager is not None:
        components["mcp_manager"] = {"status": "ok", "server_count": len(hapi.mcp_manager.server_names)}
    else:
        components["mcp_manager"] = {"status": "missing"}

    # Monitor
    if getattr(hapi, "monitor", None) is not None:
        components["monitor"] = {"status": "ok"}
    else:
        components["monitor"] = {"status": "missing"}

    # Tool registry
    if hapi.registry is not None:
        components["tool_registry"] = {"status": "ok", "tool_count": len(hapi.registry)}
    else:
        components["tool_registry"] = {"status": "missing"}

    overall = "ok" if all(c["status"] == "ok" for c in components.values()) else "degraded"

    return QueryResponse(
        type="health",
        data={
            "status": overall,
            "components": components,
        },
        meta=None,
    )


# ── Helpers ────────────────────────────────────────────────────


def _row_to_event(row: dict) -> Event | None:
    """Returns None for rows with a legacy/unknown event_type.

    See harness.storage.event_store._row_to_event for the rationale —
    Append-Only invariant forbids DELETE so historical rows with removed
    enum members persist and would otherwise crash the query read path.
    """
    try:
        et = EventType(row["event_type"])
    except ValueError:
        _log.warning(
            "Skipping query row with unknown event_type=%r (run=%s seq=%s) — likely legacy event",
            row["event_type"],
            row.get("run_id"),
            row.get("seq"),
        )
        return None
    return Event(
        run_id=row["run_id"],
        seq=row["seq"],
        event_type=et,
        payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
        idempotency_key=row.get("idempotency_key"),
        created_at=row["created_at"],
    )


# ── Include handlers registry (must be after all handler defs) ──

_include_handlers: dict[str, Any] = {
    "events": _query_events,
    "timeline": _query_timeline,
    "tool-traces": _query_tool_traces,
    "run-analysis": _query_run_analysis,
    "plans": _query_plans,
}
