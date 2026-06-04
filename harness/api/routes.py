"""REST API route handlers for Run lifecycle and event querying.

All endpoints receive HarnessAPI via FastAPI Depends(get_hapi).
Endpoints with response_model export their shapes into the OpenAPI schema
for frontend type generation.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from harness.api.deps import HarnessAPI, get_hapi
from harness.api.schemas import (
    ConfirmRequest,
    CreateRunRequest,
    EventListResponse,
    PauseRequest,
    PendingConfirmationItem,
    RunDetailResponse,
    RunListResponse,
)
from harness.core.fold import RunStatus, fold_events
from harness.models.events import (
    ConfirmationReceivedPayload,
    EventType,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
)

router = APIRouter(tags=["runs"])


# ── List / Create ──────────────────────────────────────────────


@router.get("/api/v1/runs", response_model=RunListResponse)
async def list_runs(limit: int = 50, offset: int = 0, api: HarnessAPI = Depends(get_hapi)):
    """List all runs with folded state summary, ordered by most recent."""
    rows = await api.store.list_runs(limit=limit, offset=offset)
    total = await api.store.total_run_count()
    if not rows:
        return {"runs": [], "total": total}

    # Batch-fetch events for all listed runs in one query (avoids N+1)
    run_ids = [r["run_id"] for r in rows]
    all_events = await api.store.get_events_for_runs(run_ids)
    grouped: dict[str, list] = defaultdict(list)
    for e in all_events:
        grouped[e.run_id].append(e)

    summaries = []
    for r in rows:
        rid = r["run_id"]
        events = grouped.get(rid, [])
        state = fold_events(events)
        summaries.append({
            "run_id": rid,
            "intent": state.intent,
            "status": state.status.value,
            "event_count": len(events),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return {"runs": summaries, "total": total}


@router.post("/api/v1/runs")
async def create_run(body: CreateRunRequest, api: HarnessAPI = Depends(get_hapi)):
    """Create a new run and write the RunStarted event.

    写入 RunStarted 后立即通过 start_run() 拉起 AgentLoopScheduler 的后台循环。
    Scheduler 自动执行 think→act→observe，事件写入后通过 WebSocket 广播。
    API 响应不等待 Scheduler 完成——循环运行在 asyncio.Task 中。
    """
    run_id = str(uuid.uuid4())[:8]
    await api.store.append_event(
        run_id,
        EventType.RUN_STARTED,
        RunStartedPayload(intent=body.intent).model_dump(),
    )
    # 拉起后台循环：Scheduler 在 asyncio.Task 中运行，不阻塞返回
    await api.start_run(run_id, body.intent)
    return {"run_id": run_id}


# ── Read ───────────────────────────────────────────────────────


@router.get("/api/v1/runs/{run_id}", response_model=RunDetailResponse)
async def get_run(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    """Get the folded state snapshot of a single run."""
    events = await api.store.get_events(run_id)
    if not events:
        return JSONResponse(status_code=404, content={"error": "Run not found"})
    state = fold_events(events)
    return {
        "run_id": run_id,
        "status": state.status.value,
        "intent": state.intent,
        "seq": state.seq,
        "event_count": len(events),
        "last_error": state.last_error,
        "summary": state.summary,
        "pause_reason": state.pause_reason,
        "pending_confirmations": [
            PendingConfirmationItem(
                confirmation_id=c.confirmation_id,
                tool_name=c.tool_name,
                tool_call_id=c.tool_call_id,
                input=c.input,
                risk_level=c.risk_level,
            )
            for c in state.pending_confirmations
        ],
    }


@router.get("/api/v1/runs/{run_id}/events", response_model=EventListResponse)
async def get_run_events(
    run_id: str,
    from_seq: int = 0,
    limit: int = 200,
    api: HarnessAPI = Depends(get_hapi),
):
    """Get the event stream for a run, optionally from a given seq."""
    if from_seq > 0:
        events = await api.store.get_event_range(run_id, from_seq)
    else:
        events = await api.store.get_events(run_id)
    return {
        "events": [e.model_dump(mode="json") for e in events],
        "total": len(events),
    }


# ── Lifecycle control ──────────────────────────────────────────


@router.post("/api/v1/runs/{run_id}/pause")
async def pause_run(
    run_id: str,
    body: PauseRequest | None = None,
    api: HarnessAPI = Depends(get_hapi),
):
    """Pause a running run. Writes RunPaused event and halts the scheduler loop."""
    scheduler = api._schedulers.get(run_id)
    if scheduler:
        await scheduler.pause(run_id)
    else:
        events = await api.store.get_events(run_id)
        if not events:
            return JSONResponse(status_code=404, content={"error": "Run not found"})
        state = fold_events(events)
        if state.status != RunStatus.RUNNING:
            return JSONResponse(
                status_code=409,
                content={"error": f"Run is {state.status.value}, cannot pause"},
            )
        await api.store.append_event(
            run_id,
            EventType.RUN_PAUSED,
            RunPausedPayload(reason=body.reason if body else "user_requested").model_dump(),
        )
    return {"success": True}


@router.post("/api/v1/runs/{run_id}/resume")
async def resume_run(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    """Resume a paused run. Writes RunResumed event and wakes the scheduler loop."""
    scheduler = api._schedulers.get(run_id)
    if scheduler:
        await scheduler.resume(run_id)
    else:
        # No active scheduler — validate state via Event Store
        events = await api.store.get_events(run_id)
        if not events:
            return JSONResponse(status_code=404, content={"error": "Run not found"})
        state = fold_events(events)
        if state.status != RunStatus.PAUSED:
            return JSONResponse(
                status_code=409,
                content={"error": f"Run is {state.status.value}, cannot resume"},
            )
        # Run is PAUSED but has no scheduler (edge case: scheduler already exited).
        # Write RunResumed so the event stream is consistent.
        seq = await api.store.get_latest_seq(run_id)
        await api.store.append_event(
            run_id,
            EventType.RUN_RESUMED,
            RunResumedPayload(resume_from_seq=seq).model_dump(),
        )
    return {"success": True}


@router.post("/api/v1/runs/{run_id}/confirm")
async def confirm_run(run_id: str, body: ConfirmRequest, api: HarnessAPI = Depends(get_hapi)):
    """Submit an operator confirmation decision. Idempotent per confirmation_id."""
    events = await api.store.get_events(run_id)
    existing = [
        e for e in events
        if e.event_type == EventType.CONFIRMATION_RECEIVED
    ]
    for e in existing:
        p = ConfirmationReceivedPayload(**e.payload)
        if p.confirmation_id == body.confirmation_id:
            return {
                "success": True,
                "message": "Confirmation already processed (idempotent)",
            }

    await api.store.append_event(
        run_id,
        EventType.CONFIRMATION_RECEIVED,
        ConfirmationReceivedPayload(
            confirmation_id=body.confirmation_id,
            confirmed=body.confirmed,
            operator_id=body.operator_id,
        ).model_dump(),
    )

    scheduler = api._schedulers.get(run_id)
    if scheduler:
        await scheduler.resume(run_id)

    return {"success": True}


@router.delete("/api/v1/runs/{run_id}")
async def delete_run(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    """Cancel a run and write a RunFailed termination event."""
    scheduler = api._schedulers.get(run_id)
    if scheduler:
        await scheduler.cancel(run_id)

    events = await api.store.get_events(run_id)
    if events:
        await api.store.append_event(
            run_id,
            EventType.RUN_FAILED,
            RunFailedPayload(final_error="Run deleted by user", event_count=len(events)).model_dump(),
        )

    return {"success": True}
