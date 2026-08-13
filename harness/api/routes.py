"""REST API route handlers for Run lifecycle and event querying.

All endpoints receive HarnessAPI via FastAPI Depends(get_hapi).
Endpoints with response_model export their shapes into the OpenAPI schema
for frontend type generation.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from harness.api.deps import HarnessAPI, get_hapi
from harness.api.schemas import (
    ConfirmationResponse,
    ConfirmRequest,
    CreateRunRequest,
    CreateRunResponse,
    CreateWorkspaceRequest,
    EventListResponse,
    FeedbackResponse,
    PauseRequest,
    PendingConfirmationItem,
    RunControlResponse,
    RunDetailResponse,
    RunListResponse,
    SuccessResponse,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from harness.core.fold import RunStatus, fold_events
from harness.core.logger import fmtkv, guard_logger
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
from harness.models.events import (
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
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
    WorkspaceCreatedPayload,
    WorkspaceDeletedPayload,
    WorkspaceUpdatedPayload,
)
from harness.models.intent import DeliveryContract, DeliverySource, validate_delivery_contract_input
from harness.models.workspace import Workspace, WorkspaceUpdate

_log = guard_logger("serve")
router = APIRouter(tags=["runs"])

# P1-B: 本地/沙盒执行根的受信基目录。DIRECTORY 的 filesystem_root 与 SANDBOX
# 的 host_mount_src 必须解析到该目录之内，防止任意租户把 Agent 的文件操作
# 指向宿主任意路径（C:\\、/etc、用户目录等）。可用环境变量覆盖。
WORKSPACE_BASE_DIR = Path(os.environ.get("JAGENT_WORKSPACE_BASE_DIR", "data/workspaces")).resolve()

# 契约提取单次调用超时（秒）。抽取在 scheduler 首轮 plan 前（run 内）执行，
# 不再占用 API 请求时间（create_run/send_message 立即返回 run_id）。
# 该上限约束 run 内等待；超时 → contracts=[] + unverified（D-04 兜底）。
# 定义于 harness.core.contract_extractor，供 scheduler 使用。


async def _validate_workspace_scope(scope) -> None:
    target = scope.target
    if target.type.value == "remote":
        if target.private_key_path:
            is_file = await asyncio.to_thread(Path(target.private_key_path).is_file)
            if not is_file:
                raise HTTPException(status_code=422, detail="remote private_key_path must point to an existing file")
        return
    root_value = target.filesystem_root if target.type.value == "directory" else target.host_mount_src
    if not root_value:
        raise HTTPException(status_code=422, detail=f"{target.type.value} target requires a filesystem root")

    # 与 LocalDirectoryBackend / DockerSandboxBackend 相同的解析语义：
    # 相对路径按进程 CWD resolve 后再做包含性校验，避免"校验通过但实际
    # 落在基目录之外"。
    def _assert_within_base() -> None:
        resolved = Path(root_value).expanduser().resolve()
        try:
            resolved.relative_to(WORKSPACE_BASE_DIR)
        except ValueError as exc:
            raise PermissionError(
                f"Execution root '{root_value}' must be inside the workspace base directory {WORKSPACE_BASE_DIR}"
            ) from exc

    try:
        await asyncio.to_thread(_assert_within_base)
    except PermissionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/api/v1/workspaces", response_model=WorkspaceResponse, status_code=201, tags=["workspaces"])
async def create_workspace(body: CreateWorkspaceRequest, api: HarnessAPI = Depends(get_hapi)):
    await _validate_workspace_scope(body.scope)
    now = time.time()
    workspace = Workspace(
        workspace_id=f"ws_{uuid.uuid4().hex[:12]}",
        tenant_id=api.store.tenant_id,
        name=body.name,
        description=body.description,
        scope=body.scope,
        created_at=now,
        updated_at=now,
    )
    try:
        await api.store.create_workspace(workspace)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Workspace name already exists") from exc
    await api.store.append_event(
        workspace.workspace_id,
        EventType.WORKSPACE_CREATED,
        WorkspaceCreatedPayload(
            workspace_id=workspace.workspace_id,
            tenant_id=workspace.tenant_id,
            name=workspace.name,
            description=workspace.description,
            scope=workspace.scope.model_dump(mode="json"),
        ).model_dump(),
        workspace_id=workspace.workspace_id,
        is_audit=True,
    )
    return workspace.model_dump()


@router.get("/api/v1/workspaces", response_model=WorkspaceListResponse, tags=["workspaces"])
async def list_workspaces(
    limit: int = Query(100, ge=1, le=500, description="Max workspaces per page"),
    offset: int = Query(0, ge=0, description="Number of workspaces to skip"),
    api: HarnessAPI = Depends(get_hapi),
):
    workspaces = await api.store.list_workspaces()
    total = len(workspaces)
    page = workspaces[offset : offset + limit]
    result = []
    for workspace in page:
        run_count = await api.store.total_run_count(workspace.workspace_id)
        result.append(WorkspaceResponse(**workspace.model_dump(), run_count=run_count))
    return {"workspaces": result, "total": total}


@router.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse, tags=["workspaces"])
async def get_workspace(workspace_id: str, api: HarnessAPI = Depends(get_hapi)):
    workspace = await api.store.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return WorkspaceResponse(**workspace.model_dump(), run_count=await api.store.total_run_count(workspace_id))


@router.patch("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse, tags=["workspaces"])
async def update_workspace(workspace_id: str, body: WorkspaceUpdate, api: HarnessAPI = Depends(get_hapi)):
    before = await api.store.get_workspace(workspace_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if body.scope is not None:
        await _validate_workspace_scope(body.scope)
    after = await api.store.update_workspace(workspace_id, body)
    assert after is not None
    changed = list(body.model_dump(exclude_unset=True))
    await api.store.append_event(
        workspace_id,
        EventType.WORKSPACE_UPDATED,
        WorkspaceUpdatedPayload(
            workspace_id=workspace_id,
            tenant_id=api.store.tenant_id,
            changed_fields=changed,
            old_values=before.model_dump(mode="json"),
            new_values=after.model_dump(mode="json"),
        ).model_dump(),
        workspace_id=workspace_id,
        is_audit=True,
    )
    return WorkspaceResponse(**after.model_dump(), run_count=await api.store.total_run_count(workspace_id))


@router.delete("/api/v1/workspaces/{workspace_id}", response_model=SuccessResponse, tags=["workspaces"])
async def delete_workspace(workspace_id: str, api: HarnessAPI = Depends(get_hapi)):
    if workspace_id == "default":
        raise HTTPException(status_code=409, detail="The 'default' workspace cannot be deleted")
    workspace = await api.store.delete_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await api.store.append_event(
        workspace_id,
        EventType.WORKSPACE_DELETED,
        WorkspaceDeletedPayload(workspace_id=workspace_id, tenant_id=api.store.tenant_id).model_dump(),
        workspace_id=workspace_id,
        is_audit=True,
    )
    return {"success": True}


@router.get("/api/v1/workspaces/{workspace_id}/events", response_model=EventListResponse, tags=["workspaces"])
async def get_workspace_events(
    workspace_id: str,
    limit: int = Query(100, ge=1, le=1000, description="Max events per page"),
    offset: int = Query(0, ge=0, description="Number of events to skip"),
    api: HarnessAPI = Depends(get_hapi),
):
    if await api.store.get_workspace(workspace_id) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    events = await api.store.get_workspace_events(workspace_id)
    page = events[offset : offset + limit]
    return {"events": [event.model_dump(mode="json") for event in page], "total": len(events)}


# ── List / Create ──────────────────────────────────────────────


@router.get("/api/v1/runs", response_model=RunListResponse)
async def list_runs(
    limit: int = Query(50, ge=1, le=500, description="Max runs per page"),
    offset: int = Query(0, ge=0, description="Number of runs to skip"),
    workspace_id: str | None = Query(None, description="Filter runs by workspace"),
    api: HarnessAPI = Depends(get_hapi),
):
    """List all runs with folded state summary, ordered by most recent."""
    rows = await api.store.list_runs(limit=limit, offset=offset, workspace_id=workspace_id)
    total = await api.store.total_run_count(workspace_id=workspace_id)
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
        summaries.append(
            {
                "run_id": rid,
                "intent": state.intent,
                "status": state.status.value,
                "event_count": len(events),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "orphaned": state.orphaned,
                "workspace_id": state.workspace_id,
            }
        )
    return {"runs": summaries, "total": total}


async def _build_delivery_contracts(api: HarnessAPI, body: CreateRunRequest) -> list[DeliveryContract]:
    """S07 (D-02 / C-01): 构造 caller 提供的交付契约列表（source=caller，方案 A）。

    仅当调用方显式提供 ``required_operations`` 时调用。未提供（None）→ 抽取兜底
    （方案 B）由 scheduler 首轮 plan 前异步执行，不在此处同步等待。
    """
    if body.required_operations is None:
        return []
    errors: list[str] = []
    contracts: list[DeliveryContract] = []
    for index, op in enumerate(body.required_operations):
        tool_def = api.registry.get_tool_def(op.tool) if api.registry is not None else None
        errors.extend(
            f"required_operations[{index}]: {error}"
            for error in validate_delivery_contract_input(op.tool, op.input, tool_def)
        )
        contracts.append(DeliveryContract(tool=op.tool, input=op.input, source=DeliverySource.CALLER))
    if errors:
        raise HTTPException(status_code=400, detail={"code": "invalid_delivery_contract", "errors": errors})
    return contracts


@router.post("/api/v1/runs", response_model=CreateRunResponse)
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

    intent = body.intent
    conversation_context = ""
    workspace = await api.store.get_workspace(body.workspace_id or "default")
    if workspace is None and body.workspace_id is None:
        workspace = await api.ensure_default_workspace()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if body.conversation_id:
        conv = await api.store.get_conversation(body.conversation_id)
        if conv:
            conversation_context = await _build_conversation_context(api.store, body.conversation_id)

    # RunStarted 先写（run 可观察），contracts 含 caller 显式提交（source=caller）。
    # caller 未提供 required_operations → requires_contract_extraction=True，
    # 由 scheduler 首轮 plan 前异步抽取（不阻塞本 API 响应）；失败 → [] + unverified。
    run_started_payload = RunStartedPayload(
        intent=intent,
        current_request=body.intent,
        conversation_id=body.conversation_id,
        workspace_id=workspace.workspace_id,
        # S05: 原始用户请求落事件（不可变受信数据）。
        intent_raw=body.intent,
        # S07 (D-02): 交付契约 — caller 显式提交，或标记需抽取由 scheduler 异步解析。
        contracts=(await _build_delivery_contracts(api, body)) if body.required_operations is not None else [],
        requires_contract_extraction=body.required_operations is None,
    ).model_dump()
    if body.conversation_id and body.client_request_id:
        run_id, claimed = await api.store.claim_client_request(
            body.conversation_id,
            body.client_request_id,
            run_id,
            run_started_payload,
            workspace_id=workspace.workspace_id,
        )
        if not claimed:
            return {"run_id": run_id}
    else:
        await api.store.append_event(
            run_id,
            EventType.RUN_STARTED,
            run_started_payload,
            workspace_id=workspace.workspace_id,
        )

    await api.start_run(run_id, intent, conversation_context=conversation_context, workspace_id=workspace.workspace_id)

    # Write ConversationMessage for user message
    if body.conversation_id:
        await api.store.append_event(
            body.conversation_id,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=body.conversation_id,
                run_id=run_id,
                role="user",
                content=body.intent,
                client_request_id=body.client_request_id,
            ).model_dump(),
            workspace_id=workspace.workspace_id,
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
        raise HTTPException(status_code=404, detail="Run not found")
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
        "orphaned": state.orphaned,
        "workspace_id": state.workspace_id,
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
    from_seq: int = Query(0, ge=0, description="Starting seq (inclusive); 0 = from the beginning"),
    limit: int = Query(200, ge=1, le=1000, description="Max events returned"),
    offset: int = Query(0, ge=0, description="Number of events to skip within the from_seq window"),
    api: HarnessAPI = Depends(get_hapi),
):
    """Get the event stream for a run, optionally from a given seq."""
    if from_seq > 0:
        events = await api.store.get_event_range(run_id, from_seq)
    else:
        events = await api.store.get_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail="Run not found")
    page = events[offset : offset + limit]
    return {
        "events": [e.model_dump(mode="json") for e in page],
        "total": len(events),
    }


# ── Lifecycle control ──────────────────────────────────────────


@router.post("/api/v1/runs/{run_id}/pause", response_model=RunControlResponse)
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
            raise HTTPException(status_code=404, detail="Run not found")
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


@router.post("/api/v1/runs/{run_id}/resume", response_model=RunControlResponse)
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
            raise HTTPException(status_code=404, detail="Run not found")
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


@router.post("/api/v1/runs/{run_id}/confirm", response_model=ConfirmationResponse)
async def confirm_run(run_id: str, body: ConfirmRequest, api: HarnessAPI = Depends(get_hapi)):
    """Submit an operator confirmation decision. Idempotent per confirmation_id."""
    events = await api.store.get_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail="Run not found")
    existing = [e for e in events if e.event_type == EventType.CONFIRMATION_RECEIVED]
    for e in existing:
        p = ConfirmationReceivedPayload(**e.payload)
        if p.confirmation_id == body.confirmation_id:
            return {
                "success": True,
                "message": "Confirmation already processed (idempotent)",
            }

    state = fold_events(events)
    if state.status != RunStatus.PAUSED:
        raise HTTPException(
            status_code=409,
            detail={"code": "run_not_waiting_confirmation", "status": state.status.value},
        )

    requested = None
    for e in events:
        if e.event_type == EventType.CONFIRMATION_REQUESTED:
            candidate = ConfirmationRequestedPayload(**e.payload)
            if candidate.confirmation_id == body.confirmation_id:
                requested = candidate
                break

    pending = next(
        (item for item in state.pending_confirmations if item.confirmation_id == body.confirmation_id),
        None,
    )
    if requested is None or pending is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "confirmation_not_pending", "confirmation_id": body.confirmation_id},
        )

    await api.store.append_event(
        run_id,
        EventType.CONFIRMATION_RECEIVED,
        ConfirmationReceivedPayload(
            confirmation_id=body.confirmation_id,
            confirmed=body.confirmed,
            operator_id=body.operator_id,
            step_id=requested.step_id,
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


@router.post("/api/v1/runs/{run_id}/feedback", response_model=FeedbackResponse)
async def operator_feedback(
    run_id: str,
    body: OperatorFeedbackRequest = Body(...),
    api: HarnessAPI = Depends(get_hapi),
):
    """Operator injects manual feedback into a running run.

    Feedback goes through EventStore → fold → Scheduler path,
    same as Monitor-injected feedback.
    """
    _log.info(
        "Operator feedback request %s",
        fmtkv(
            run_id=run_id,
            text_len=len(body.text),
            priority=body.priority,
            has_suggestion=body.suggestion is not None,
            expires_in_seqs=body.expires_in_seqs,
        ),
    )

    events = await api.store.get_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail="Run not found")

    feedback_id = FeedbackInjectedPayload.compute_feedback_id(
        run_id,
        FeedbackCategory.OPERATOR_ADVICE.value,
        body.text[:100],
        "?",
    )

    current_seq = events[-1].seq
    expires_at_seq = current_seq + body.expires_in_seqs if body.expires_in_seqs is not None else None
    payload = FeedbackInjectedPayload(
        feedback_id=feedback_id,
        source=FeedbackSource.OPERATOR,
        category=FeedbackCategory.OPERATOR_ADVICE,
        feedback_text=body.text,
        priority=body.priority,
        suggestion=body.suggestion,
        expires_at_seq=expires_at_seq,
    )
    try:
        await api.store.append_event(
            run_id,
            EventType.FEEDBACK_INJECTED,
            payload.model_dump(),
        )
        _log.info(
            "Operator feedback injected %s",
            fmtkv(
                feedback_id=feedback_id,
                run_id=run_id,
                text_len=len(body.text),
                priority=body.priority,
            ),
        )
    except Exception:
        _log.exception("Failed to inject operator feedback for %s", run_id)
        raise
    return {"status": "ok", "feedback_id": feedback_id}


@router.delete("/api/v1/runs/{run_id}", response_model=SuccessResponse)
async def delete_run(run_id: str, api: HarnessAPI = Depends(get_hapi)):
    """Cancel a run and write a RunFailed termination event."""
    events = await api.store.get_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail="Run not found")

    scheduler = api._schedulers.get(run_id)
    if scheduler:
        await scheduler.cancel(run_id)

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
    limit: int = Query(50, ge=1, le=500, description="Max conversations per page"),
    offset: int = Query(0, ge=0, description="Number of conversations to skip"),
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
        raise HTTPException(status_code=404, detail="Conversation not found")

    events = await api.store.get_events_for_conversation(conversation_id)
    messages: list[ConversationMessageItem] = []
    for e in events:
        if e.event_type == EventType.CONVERSATION_MESSAGE:
            p = e.payload
            messages.append(
                ConversationMessageItem(
                    seq=e.seq,
                    run_id=p.get("run_id", ""),
                    role=p.get("role", ""),
                    content=p.get("content", ""),
                    created_at=e.created_at,
                    status="completed",
                )
            )

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
        raise HTTPException(status_code=404, detail="Conversation not found")

    run_id = str(uuid.uuid4())[:8]

    intent = body.message
    workspace = await api.store.get_workspace(body.workspace_id or "default")
    if workspace is None and body.workspace_id is None:
        workspace = await api.ensure_default_workspace()
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    conversation_context = await _build_conversation_context(api.store, conversation_id)

    contract_request = CreateRunRequest(
        intent=intent,
        conversation_id=conversation_id,
        client_request_id=body.client_request_id,
        workspace_id=workspace.workspace_id,
        required_operations=[operation.model_dump() for operation in body.required_operations]
        if body.required_operations is not None
        else None,
    )
    caller_contracts = (
        await _build_delivery_contracts(api, contract_request) if body.required_operations is not None else []
    )

    run_started_payload = RunStartedPayload(
        intent=intent,
        current_request=body.message,
        conversation_id=conversation_id,
        workspace_id=workspace.workspace_id,
        intent_raw=body.message,
        contracts=caller_contracts,
        requires_contract_extraction=body.required_operations is None,
    ).model_dump()
    if body.client_request_id:
        run_id, claimed = await api.store.claim_client_request(
            conversation_id,
            body.client_request_id,
            run_id,
            run_started_payload,
            workspace_id=workspace.workspace_id,
        )
        if not claimed:
            existing = await api.store.get_events_for_conversation(conversation_id)
            message = next(
                (
                    event
                    for event in existing
                    if event.event_type == EventType.CONVERSATION_MESSAGE
                    and event.payload.get("client_request_id") == body.client_request_id
                ),
                None,
            )
            return {
                "run_id": run_id,
                "conversation_id": conversation_id,
                "seq": message.seq if message else 1,
                "claimed": False,
            }
    else:
        await api.store.append_event(
            run_id,
            EventType.RUN_STARTED,
            run_started_payload,
            workspace_id=workspace.workspace_id,
        )

    await api.start_run(run_id, intent, conversation_context=conversation_context, workspace_id=workspace.workspace_id)

    await api.store.append_event(
        conversation_id,
        EventType.CONVERSATION_MESSAGE,
        ConversationMessagePayload(
            conversation_id=conversation_id,
            run_id=run_id,
            role="user",
            content=body.message,
            client_request_id=body.client_request_id,
        ).model_dump(),
        workspace_id=workspace.workspace_id,
    )
    await api.store.increment_message_count(conversation_id)

    seq = await api.store.get_latest_seq(conversation_id)
    return {"run_id": run_id, "conversation_id": conversation_id, "seq": seq, "claimed": True}


@router.delete("/api/v1/conversations/{conversation_id}", response_model=DeleteConversationResponse)
async def delete_conversation(
    conversation_id: str,
    api: HarnessAPI = Depends(get_hapi),
):
    """Soft-delete a conversation (marks as archived)."""
    conv = await api.store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
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
        raise HTTPException(status_code=404, detail="Conversation not found")
    ok = await api.store.update_conversation(
        conversation_id,
        title=body.title,
        status=body.status,
    )
    return {"success": ok}
