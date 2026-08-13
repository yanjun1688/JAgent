from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from harness.models.events import (
    AgentThoughtPayload,
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
    ContextCheckpointedPayload,
    ContextCompressedPayload,
    ContextPrunedPayload,
    DagStepCompletedPayload,
    DagStepFailedPayload,
    DagStepSkippedPayload,
    DagStepStartedPayload,
    DeliveryContractsResolvedPayload,
    Episode,
    EpisodeArchivedPayload,
    Event,
    EventType,
    FeedbackInjectedPayload,
    GuardrailTriggeredPayload,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanFailedPayload,
    PlanRevisedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunOrphanedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolResultType,
    ToolTimeoutPayload,
)
from harness.models.intent import DeliveryContract


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolResultStatus(str, Enum):
    COMPLETED = "completed"
    UNSUCCESSFUL = "unsuccessful"
    FAILED = "failed"
    TIMEOUT = "timeout"
    GUARDRAIL_BLOCKED = "guardrail_blocked"


@dataclass
class ThoughtEntry:
    seq: int
    thought: str
    tool_choice: str | None = None
    token_count: int = 0


@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    idempotency_key: str | None = None
    event_seq: int = 0


@dataclass
class RunState:
    run_id: str
    status: RunStatus = RunStatus.RUNNING
    seq: int = 0
    intent: str = ""
    current_request: str = ""
    context_snapshot: dict = field(default_factory=dict)
    thought_history: list[ThoughtEntry] = field(default_factory=list)
    latest_thought: ThoughtEntry | None = None
    tool_calls: list[ToolCalledPayload] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    last_error: str | None = None
    user_facing_message: str | None = None
    summary: Episode | str | None = None
    keep_recent_count: int = 0
    pause_reason: str | None = None
    pending_confirmations: list[ConfirmationRequestedPayload] = field(default_factory=list)
    last_checkpoint_seq: int | None = None
    feedbacks: list[FeedbackInjectedPayload] = field(default_factory=list)
    plan_history: list[dict] = field(default_factory=list)
    step_tasks: dict[str, str] = field(default_factory=dict)
    latest_plan: dict | None = None
    plan_boundary_seqs: list[int] = field(default_factory=list)
    # v2.2 (D5): run 终态机械达成证据（洞 5）— RUN_COMPLETED 携带
    completion_evidence: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    workspace_id: str | None = None
    orphaned: bool = False
    episodes: list[Episode] = field(default_factory=list)
    # S05 (D-02/D-04/D-05, C-01): 原始意图（不可变）+ 交付契约列表（多交付物）
    intent_raw: str = ""
    delivery_contracts: list[DeliveryContract] = field(default_factory=list)
    # S07 (D-02 / 方案 B): RunStarted 标记 → scheduler 首轮 plan 前需执行契约解析。
    requires_contract_extraction: bool = False


def fold_events(events: list[Event]) -> RunState:
    """Pure function: fold a sorted event stream into a RunState snapshot.

    Events must be sorted by seq ascending. The function is deterministic:
    the same event stream always produces the same RunState.
    """
    if not events:
        raise ValueError("Cannot fold empty event list")

    run_id = events[0].run_id
    state = RunState(run_id=run_id)
    p: Any = None

    for event in events:
        if event.run_id != run_id:
            raise ValueError(f"Mixed run_ids in event stream: expected '{run_id}', got '{event.run_id}'")
        state.seq = max(state.seq, event.seq)

        match event.event_type:
            case EventType.RUN_STARTED:
                p = RunStartedPayload(**event.payload)
                state.intent = p.intent
                state.current_request = p.current_request or p.intent
                state.context_snapshot = p.context_snapshot
                state.status = RunStatus.RUNNING
                state.conversation_id = p.conversation_id
                state.workspace_id = p.workspace_id
                # S05: 原始意图与交付契约折叠（intent_raw 不可变，Planner 不得覆盖）
                state.intent_raw = p.intent_raw or p.intent
                state.delivery_contracts = list(p.contracts)
                state.requires_contract_extraction = p.requires_contract_extraction

            case EventType.DELIVERY_CONTRACTS_RESOLVED:
                p = DeliveryContractsResolvedPayload(**event.payload)
                state.delivery_contracts = list(p.contracts)

            case EventType.AGENT_THOUGHT:
                p = AgentThoughtPayload(**event.payload)
                state.thought_history.append(
                    ThoughtEntry(
                        seq=event.seq,
                        thought=p.thought,
                        tool_choice=p.tool_choice,
                        token_count=p.token_count,
                    )
                )
                state.latest_thought = state.thought_history[-1]

            case EventType.TOOL_CALLED:
                p = ToolCalledPayload(**event.payload)
                state.tool_calls.append(p)

            case EventType.TOOL_COMPLETED:
                p = ToolCompletedPayload(**event.payload)
                state.tool_results.append(
                    ToolResult(
                        tool_call_id=p.tool_call_id,
                        tool_name=p.tool_name,
                        status=(
                            ToolResultStatus.UNSUCCESSFUL
                            if p.result_type == ToolResultType.UNSUCCESSFUL
                            else ToolResultStatus.COMPLETED
                        ),
                        output=p.output,
                        duration_ms=p.duration_ms,
                        event_seq=event.seq,
                        error=p.error,
                    )
                )

            case EventType.TOOL_FAILED:
                p = ToolFailedPayload(**event.payload)
                state.tool_results.append(
                    ToolResult(
                        tool_call_id=p.tool_call_id,
                        tool_name=p.tool_name,
                        status=ToolResultStatus.FAILED,
                        error=p.error,
                        event_seq=event.seq,
                    )
                )
                state.last_error = p.error

            case EventType.TOOL_TIMEOUT:
                p = ToolTimeoutPayload(**event.payload)
                state.tool_results.append(
                    ToolResult(
                        tool_call_id=p.tool_call_id,
                        tool_name=p.tool_name,
                        status=ToolResultStatus.TIMEOUT,
                        error=f"Timeout after {p.timeout_ms}ms",
                        event_seq=event.seq,
                    )
                )
                state.last_error = f"Tool '{p.tool_name}' timed out"

            case EventType.GUARDRAIL_TRIGGERED:
                p = GuardrailTriggeredPayload(**event.payload)
                state.tool_results.append(
                    ToolResult(
                        tool_call_id=p.tool_call_id,
                        tool_name=p.tool_name,
                        status=ToolResultStatus.GUARDRAIL_BLOCKED,
                        error=f"Guardrail '{p.guardrail_id}': {p.reason}",
                        event_seq=event.seq,
                    )
                )

            case EventType.CONFIRMATION_REQUESTED:
                p = ConfirmationRequestedPayload(**event.payload)
                state.pending_confirmations.append(p)

            case EventType.CONFIRMATION_RECEIVED:
                p = ConfirmationReceivedPayload(**event.payload)
                state.pending_confirmations = [
                    c for c in state.pending_confirmations if c.confirmation_id != p.confirmation_id
                ]

            case EventType.CONTEXT_COMPRESSED:
                # Legacy read-only: old runs used ContextCompressed with Episode/str summary.
                p = ContextCompressedPayload(**event.payload)
                state.summary = p.summary_ref
                state.keep_recent_count = p.keep_recent_count
                if isinstance(state.summary, Episode) and state.summary.original_event_refs:
                    compressed_seqs = set(state.summary.original_event_refs)
                    keep = max(p.keep_recent_count, 0)
                    recent_thought_seqs = {t.seq for t in state.thought_history[-keep:]} if keep > 0 else set()
                    recent_result_seqs = {tr.event_seq for tr in state.tool_results[-keep:]} if keep > 0 else set()
                    state.thought_history = [
                        t for t in state.thought_history if t.seq not in compressed_seqs or t.seq in recent_thought_seqs
                    ]
                    state.tool_results = [
                        tr
                        for tr in state.tool_results
                        if tr.event_seq not in compressed_seqs or tr.event_seq in recent_result_seqs
                    ]
                    # Known Issue: tool_calls and feedbacks are NOT trimmed here.
                    # They accumulate unboundedly over long runs.
                    # See TODO_v2.1.md §Known Technical Debt.

            case EventType.CONTEXT_CHECKPOINTED:
                p = ContextCheckpointedPayload(**event.payload)
                state.last_checkpoint_seq = p.checkpoint_seq

            case EventType.RUN_PAUSED:
                p = RunPausedPayload(**event.payload)
                state.status = RunStatus.PAUSED
                state.pause_reason = p.reason

            case EventType.RUN_RESUMED:
                p = RunResumedPayload(**event.payload)
                state.status = RunStatus.RUNNING
                state.pause_reason = None

            case EventType.RUN_COMPLETED:
                p = RunCompletedPayload(**event.payload)
                state.status = RunStatus.COMPLETED
                state.summary = p.result_summary
                state.completion_evidence = {
                    "all_normal": p.all_normal,
                    "unmet_step_ids": list(p.unmet_step_ids),
                    # S06 (D-03/D-04): 交付契约维度折叠
                    "deliverable_met": p.deliverable_met,
                    "deliverable_status": p.deliverable_status,
                    "deliverable_summary": list(p.deliverable_summary),
                }

            case EventType.RUN_FAILED:
                p = RunFailedPayload(**event.payload)
                state.status = RunStatus.FAILED
                state.last_error = p.final_error
                state.user_facing_message = p.user_facing_message
                if p.result_summary:
                    state.summary = p.result_summary

            case EventType.RUN_COMMAND:
                # Control-plane command events are consumed by Scheduler and do
                # not themselves mutate folded run state.
                pass

            case EventType.FEEDBACK_INJECTED:
                p = FeedbackInjectedPayload(**event.payload)
                if p.injected_at_seq is None:
                    p = p.model_copy(update={"injected_at_seq": event.seq})
                state.feedbacks.append(p)

            case EventType.PLAN_CREATED:
                p = PlanCreatedPayload(**event.payload)
                # v2.2 (C, D6): 计划结构落事件 — 重建 DAG 蓝图
                blueprint = p.steps if p.steps else []
                entry = {
                    "plan_id": p.plan_id,
                    "intent": p.intent,
                    "steps_summary": p.steps_summary,
                    "layer_count": p.layer_count,
                    "steps": list(blueprint),
                }
                state.plan_history.append(entry)
                state.latest_plan = entry

            case EventType.DAG_STEP_STARTED:
                p = DagStepStartedPayload(**event.payload)
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    existing = next((s for s in state.latest_plan["steps"] if s["step_id"] == p.step_id), None)
                    if existing:
                        existing["status"] = "started"
                        existing["tool_name"] = p.tool_name
                    else:
                        state.latest_plan["steps"].append(
                            {"step_id": p.step_id, "tool_name": p.tool_name, "status": "started"}
                        )

            case EventType.DAG_STEP_COMPLETED:
                p = DagStepCompletedPayload(**event.payload)
                step_status = p.status if p.status else "completed"
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    existing = next((s for s in state.latest_plan["steps"] if s["step_id"] == p.step_id), None)
                    if existing:
                        existing["status"] = step_status
                        existing["output_summary"] = p.output_summary
                        if p.error:
                            existing["error"] = p.error
                        if p.tool_call_id:
                            existing["tool_call_id"] = p.tool_call_id
                    else:
                        step_data = {"step_id": p.step_id, "status": step_status, "output_summary": p.output_summary}
                        if p.error:
                            step_data["error"] = p.error
                        if p.tool_call_id:
                            step_data["tool_call_id"] = p.tool_call_id
                        state.latest_plan["steps"].append(step_data)

            case EventType.DAG_STEP_FAILED:
                p = DagStepFailedPayload(**event.payload)
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    existing = next((s for s in state.latest_plan["steps"] if s["step_id"] == p.step_id), None)
                    if existing:
                        existing["status"] = "failed"
                        existing["error"] = p.error
                        if p.tool_call_id:
                            existing["tool_call_id"] = p.tool_call_id
                    else:
                        entry = {"step_id": p.step_id, "status": "failed", "error": p.error}
                        if p.tool_call_id:
                            entry["tool_call_id"] = p.tool_call_id
                        state.latest_plan["steps"].append(entry)

            case EventType.DAG_STEP_SKIPPED:
                p = DagStepSkippedPayload(**event.payload)
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    existing = next((s for s in state.latest_plan["steps"] if s["step_id"] == p.step_id), None)
                    if existing:
                        existing["status"] = "skipped"
                        existing["reason"] = p.reason
                    else:
                        state.latest_plan["steps"].append(
                            {
                                "step_id": p.step_id,
                                "status": "skipped",
                                "reason": p.reason,
                            }
                        )

            case EventType.PLAN_REVISED:
                p = PlanRevisedPayload(**event.payload)
                state.feedbacks = [
                    fb.model_copy(update={"consumed_at_seq": event.seq}) if fb.consumed_at_seq is None else fb
                    for fb in state.feedbacks
                ]
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    state.latest_plan["intent"] = p.intent
                    state.latest_plan["revision_reason"] = p.revision_reason
                    state.latest_plan["remaining_steps_summary"] = p.remaining_steps_summary
                    state.latest_plan["step_tasks"] = dict(p.step_tasks)
                    # v2.2 (P2): 修订蓝图落事件 → 折叠态 latest_plan 与实际修订计划对齐。
                    # 修订后的步骤列表替换旧蓝图（各步骤以 step_id 为主键，后续
                    # DAG_STEP_* 事件继续按 step_id 更新状态）。
                    if p.steps:
                        state.latest_plan["steps"] = [dict(s) for s in p.steps]
                state.step_tasks.update(p.step_tasks)

            case EventType.PLAN_COMPLETED:
                p = PlanCompletedPayload(**event.payload)
                state.plan_boundary_seqs.append(event.seq)
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    state.latest_plan["status"] = "completed"
                    state.latest_plan["summary"] = p.summary

            case EventType.PLAN_FAILED:
                p = PlanFailedPayload(**event.payload)
                state.plan_boundary_seqs.append(event.seq)
                if state.latest_plan and state.latest_plan["plan_id"] == p.plan_id:
                    state.latest_plan["status"] = "failed"
                    state.latest_plan["final_error"] = p.final_error

            case EventType.CONVERSATION_STARTED | EventType.CONVERSATION_MESSAGE | EventType.CONVERSATION_ENDED:
                pass

            case EventType.RUN_ORPHANED:
                RunOrphanedPayload(**event.payload)
                state.orphaned = True

            case EventType.EPISODE_ARCHIVED:
                p = EpisodeArchivedPayload(**event.payload)
                state.summary = p.episode
                state.episodes.append(p.episode)
                state.keep_recent_count = p.keep_recent_count
                archived_set = set(p.archived_event_refs)
                keep = max(p.keep_recent_count, 0)
                recent_thought_seqs = {t.seq for t in state.thought_history[-keep:]} if keep > 0 else set()
                recent_result_seqs = {tr.event_seq for tr in state.tool_results[-keep:]} if keep > 0 else set()
                state.thought_history = [
                    t for t in state.thought_history if t.seq not in archived_set or t.seq in recent_thought_seqs
                ]
                state.tool_results = [
                    tr
                    for tr in state.tool_results
                    if tr.event_seq not in archived_set or tr.event_seq in recent_result_seqs
                ]

            case EventType.CONTEXT_PRUNED:
                p = ContextPrunedPayload(**event.payload)
                pruned_set = set(p.pruned_event_refs)
                state.thought_history = [t for t in state.thought_history if t.seq not in pruned_set]
                state.tool_results = [tr for tr in state.tool_results if tr.event_seq not in pruned_set]

    return state
