"""REST API route handlers for Run lifecycle and event querying.

All endpoints receive HarnessAPI via FastAPI Depends(get_hapi).
Endpoints with response_model export their shapes into the OpenAPI schema
for frontend type generation.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from harness.api.deps import HarnessAPI, get_hapi
from harness.core.logger import fmtkv, guard_logger

_log = guard_logger("serve")
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
    ConversationMessagePayload,
    ConversationStartedPayload,
    EventType,
    FeedbackCategory,
    FeedbackInjectedPayload,
    FeedbackSource,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
)
from harness.models.conversation import (
    Conversation,
    ConversationDetail,
    ConversationListResponse,
    ConversationMessageItem,
    CreateConversationRequest,
    CreateConversationResponse,
    DeleteConversationResponse,
    SendMessageRequest,
    SendMessageResponse,
    UpdateConversationRequest,
    UpdateConversationResponse,
    _build_conversation_context,
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

    写入 RunStarted 后立即通过 start_run() 拉起 PlanningExecutorScheduler 的后台循环。
    Scheduler 自动执行 plan→execute→revise，事件写入后通过 WebSocket 广播。
    API 响应不等待 Scheduler 完成——循环运行在 asyncio.Task 中。
    注：当 Planner 生成 Plan 失败时，会降级到 AgentLoopScheduler（串行 think→act→observe）。
    """
    run_id = str(uuid.uuid4())[:8]
    _log.info("Creating run — intent: %.120s", body.intent)
    _t0 = time.monotonic()

    # Build conversation context if conversation_id is provided
    intent = body.intent
    if body.conversation_id:
        conv = await api.store.get_conversation(body.conversation_id)
        if conv:
            ctx = await _build_conversation_context(api.store, body.conversation_id)
            if ctx:
                intent = f"Previous conversation:\n{ctx}\n\nCurrent request: {body.intent}"
                _log.info("Conversation context injected into intent for conversation=%s", body.conversation_id)

    await api.store.append_event(
        run_id,
        EventType.RUN_STARTED,
        RunStartedPayload(
            intent=intent,
            conversation_id=body.conversation_id,
        ).model_dump(),
    )

    await api.start_run(run_id, intent)

    # Write ConversationMessage for user message
    if body.conversation_id:
        now = time.time()
        await api.store.append_event(
            body.conversation_id,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=body.conversation_id,
                run_id=run_id,
                role="user",
                content=body.intent,
            ).model_dump(),
        )
        await api.store.increment_message_count(body.conversation_id)
    _ms = (time.monotonic() - _t0) * 1000
    _log.info("Run %s started in %dms", run_id, _ms)
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
        "conversation_id": state.conversation_id,
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
        ok = await scheduler.pause(run_id)
        if not ok:
            return JSONResponse(
                status_code=409,
                content={"error": "Run is not in RUNNING state, cannot pause"},
            )
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
        ok = await scheduler.resume(run_id)
        if not ok:
            return JSONResponse(
                status_code=409,
                content={"error": "Run is not in PAUSED state, cannot resume"},
            )
    else:
        events = await api.store.get_events(run_id)
        if not events:
            return JSONResponse(status_code=404, content={"error": "Run not found"})
        state = fold_events(events)
        if state.status != RunStatus.PAUSED:
            return JSONResponse(
                status_code=409,
                content={"error": f"Run is {state.status.value}, cannot resume"},
            )
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
        idempotency_key=f"confirm_{body.confirmation_id}",
    )

    scheduler = api._schedulers.get(run_id)
    if scheduler:
        await scheduler.resume(run_id)

    return {"success": True}


class OperatorFeedbackRequest(BaseModel):
    text: str = Field(..., max_length=500)
    priority: str = Field(default="medium", pattern="^(high|medium|low)$")
    suggestion: str | None = Field(None, max_length=300)
    expires_in_seqs: int | None = Field(None, ge=1, le=500)


@router.post("/api/v1/runs/{run_id}/feedback")
async def operator_feedback(
    run_id: str,
    body: OperatorFeedbackRequest = Body(...),
    api: HarnessAPI = Depends(get_hapi),
):
    """Operator injects manual feedback into a running run.

    Feedback goes through EventStore → fold → Scheduler path,
    same as Monitor-injected feedback.
    """
    _log.info("Operator feedback request %s", fmtkv(
        run_id=run_id, text_len=len(body.text),
        priority=body.priority, has_suggestion=body.suggestion is not None,
        expires_in_seqs=body.expires_in_seqs,
    ))

    feedback_id = FeedbackInjectedPayload.compute_feedback_id(
        run_id, FeedbackCategory.OPERATOR_ADVICE.value,
        body.text[:100], "?",
    )

    payload = FeedbackInjectedPayload(
        feedback_id=feedback_id,
        source=FeedbackSource.OPERATOR,
        category=FeedbackCategory.OPERATOR_ADVICE,
        feedback_text=body.text,
        priority=body.priority,
        suggestion=body.suggestion,
        expires_at_seq=body.expires_in_seqs,
    )
    try:
        await api.store.append_event(
            run_id, EventType.FEEDBACK_INJECTED, payload.model_dump(),
        )
        _log.info("Operator feedback injected %s", fmtkv(
            feedback_id=feedback_id, run_id=run_id,
            text_len=len(body.text), priority=body.priority,
        ))
    except Exception:
        _log.exception("Failed to inject operator feedback for %s", run_id)
        raise
    return {"status": "ok", "feedback_id": feedback_id}


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

    api.cleanup_run_resources(run_id)
    return {"success": True}


# ── Conversation endpoints ─────────────────────────────────────


@router.post("/api/v1/conversations", status_code=201, response_model=CreateConversationResponse)
async def create_conversation(
    body: CreateConversationRequest | None = None,
    api: HarnessAPI = Depends(get_hapi),
):
    """Create a new conversation and write ConversationStarted event."""
    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    title = (body.title if body and body.title else None) or "New conversation"
    now = time.time()

    await api.store.upsert_conversation(conv_id, title)
    await api.store.append_event(
        conv_id,
        EventType.CONVERSATION_STARTED,
        ConversationStartedPayload(
            conversation_id=conv_id,
            title=title,
        ).model_dump(),
    )
    return {"conversation_id": conv_id, "title": title, "created_at": now}


@router.get("/api/v1/conversations", response_model=ConversationListResponse)
async def list_conversations(
    limit: int = 50,
    offset: int = 0,
    api: HarnessAPI = Depends(get_hapi),
):
    """List all active conversations, ordered by most recent."""
    rows = await api.store.list_conversations(limit=limit, offset=offset)
    total = await api.store.total_conversation_count()
    conversations = [
        Conversation(
            conversation_id=r["conversation_id"],
            user_id=r["user_id"],
            title=r["title"],
            status=r["status"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            message_count=r["message_count"],
        )
        for r in rows
    ]
    return {"conversations": conversations, "total": total}


@router.get("/api/v1/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    api: HarnessAPI = Depends(get_hapi),
):
    """Get conversation details with message list."""
    conv = await api.store.get_conversation(conversation_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})

    events = await api.store.get_events_for_conversation(conversation_id)
    messages: list[ConversationMessageItem] = []
    for e in events:
        if e.event_type == EventType.CONVERSATION_MESSAGE:
            p = e.payload
            messages.append(ConversationMessageItem(
                seq=e.seq,
                run_id=p.get("run_id", ""),
                role=p.get("role", ""),
                content=p.get("content", ""),
                created_at=e.created_at,
                status="completed",
            ))

    conv_response = Conversation(
        conversation_id=conv["conversation_id"],
        user_id=conv.get("user_id", "default"),
        title=conv["title"],
        status=conv["status"],
        created_at=conv["created_at"],
        updated_at=conv["updated_at"],
        message_count=conv["message_count"],
    )
    return {"conversation": conv_response, "messages": messages}


@router.post("/api/v1/conversations/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    api: HarnessAPI = Depends(get_hapi),
):
    """Send a message in a conversation, creating a new Run with context."""
    conv = await api.store.get_conversation(conversation_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})

    run_id = str(uuid.uuid4())[:8]
    now = time.time()

    ctx = await _build_conversation_context(api.store, conversation_id)
    intent = body.message
    if ctx:
        intent = f"Previous conversation:\n{ctx}\n\nCurrent request: {body.message}"

    await api.store.append_event(
        run_id,
        EventType.RUN_STARTED,
        RunStartedPayload(intent=intent, conversation_id=conversation_id).model_dump(),
    )

    await api.start_run(run_id, intent)

    await api.store.append_event(
        conversation_id,
        EventType.CONVERSATION_MESSAGE,
        ConversationMessagePayload(
            conversation_id=conversation_id,
            run_id=run_id,
            role="user",
            content=body.message,
        ).model_dump(),
    )
    await api.store.increment_message_count(conversation_id)

    seq = await api.store.get_latest_seq(conversation_id)
    return {"run_id": run_id, "conversation_id": conversation_id, "seq": seq}


@router.delete("/api/v1/conversations/{conversation_id}", response_model=DeleteConversationResponse)
async def delete_conversation(
    conversation_id: str,
    api: HarnessAPI = Depends(get_hapi),
):
    """Soft-delete a conversation (marks as archived)."""
    conv = await api.store.get_conversation(conversation_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})
    await api.store.delete_conversation(conversation_id)
    return {"success": True}


@router.patch("/api/v1/conversations/{conversation_id}", response_model=UpdateConversationResponse)
async def update_conversation(
    conversation_id: str,
    body: UpdateConversationRequest,
    api: HarnessAPI = Depends(get_hapi),
):
    """Update conversation title or status."""
    conv = await api.store.get_conversation(conversation_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Conversation not found"})
    ok = await api.store.update_conversation(
        conversation_id,
        title=body.title,
        status=body.status,
    )
    return {"success": ok}
