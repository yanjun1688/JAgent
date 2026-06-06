from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from harness.models.events import (
    AgentThoughtPayload,
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
    ContextCheckpointedPayload,
    ContextCompressedPayload,
    EpisodeSummary,
    Event,
    EventType,
    FeedbackInjectedPayload,
    GuardrailTriggeredPayload,
    OrchestrationCompletedPayload,
    OrchestrationFailedPayload,
    OrchestrationStartedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
    StepCompletedPayload,
    StepFailedPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolTimeoutPayload,
)


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolResultStatus(str, Enum):
    COMPLETED = "completed"
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
    output: object = None
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
    context_snapshot: dict = field(default_factory=dict)
    thought_history: list[ThoughtEntry] = field(default_factory=list)
    latest_thought: ThoughtEntry | None = None
    tool_calls: list[ToolCalledPayload] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    last_error: str | None = None
    summary: EpisodeSummary | str | None = None
    keep_recent_count: int = 0
    pause_reason: str | None = None
    pending_confirmations: list[ConfirmationRequestedPayload] = field(default_factory=list)
    last_checkpoint_seq: int | None = None
    orchestration_history: list = field(default_factory=list)
    latest_orchestration: dict | None = None
    feedbacks: list[FeedbackInjectedPayload] = field(default_factory=list)


def fold_events(events: list[Event]) -> RunState:
    """Pure function: fold a sorted event stream into a RunState snapshot.

    Events must be sorted by seq ascending. The function is deterministic:
    the same event stream always produces the same RunState.
    """
    if not events:
        raise ValueError("Cannot fold empty event list")

    run_id = events[0].run_id
    state = RunState(run_id=run_id)

    for event in events:
        if event.run_id != run_id:
            raise ValueError(f"Mixed run_ids in event stream: expected '{run_id}', got '{event.run_id}'")
        state.seq = max(state.seq, event.seq)

        match event.event_type:
            case EventType.RUN_STARTED:
                p = RunStartedPayload(**event.payload)
                state.intent = p.intent
                state.context_snapshot = p.context_snapshot
                state.status = RunStatus.RUNNING

            case EventType.AGENT_THOUGHT:
                p = AgentThoughtPayload(**event.payload)
                state.thought_history.append(ThoughtEntry(
                    seq=event.seq,
                    thought=p.thought,
                    tool_choice=p.tool_choice,
                    token_count=p.token_count,
                ))
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
                        status=ToolResultStatus.COMPLETED,
                        output=p.output,
                        duration_ms=p.duration_ms,
                        event_seq=event.seq,
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
                p = ContextCompressedPayload(**event.payload)
                state.summary = p.summary_ref
                state.keep_recent_count = p.keep_recent_count

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

            case EventType.RUN_FAILED:
                p = RunFailedPayload(**event.payload)
                state.status = RunStatus.FAILED
                state.last_error = p.final_error

            case EventType.ORCHESTRATION_STARTED:
                p = OrchestrationStartedPayload(**event.payload)
                entry = {"plan_id": p.plan_id, "intent": p.intent, "steps_summary": p.steps_summary, "steps": []}
                state.orchestration_history.append(entry)
                state.latest_orchestration = entry

            case EventType.STEP_COMPLETED:
                p = StepCompletedPayload(**event.payload)
                if state.latest_orchestration and state.latest_orchestration["plan_id"] == p.plan_id:
                    state.latest_orchestration["steps"].append({"status": "completed", "step_index": p.step_index})

            case EventType.STEP_FAILED:
                p = StepFailedPayload(**event.payload)
                if state.latest_orchestration and state.latest_orchestration["plan_id"] == p.plan_id:
                    state.latest_orchestration["steps"].append({"status": "failed", "step_index": p.step_index, "error": p.error})

            case EventType.ORCHESTRATION_COMPLETED:
                p = OrchestrationCompletedPayload(**event.payload)
                if state.latest_orchestration and state.latest_orchestration["plan_id"] == p.plan_id:
                    state.latest_orchestration["status"] = "completed"
                    state.latest_orchestration["summary"] = p.summary

            case EventType.ORCHESTRATION_FAILED:
                p = OrchestrationFailedPayload(**event.payload)
                if state.latest_orchestration and state.latest_orchestration["plan_id"] == p.plan_id:
                    state.latest_orchestration["status"] = "failed"
                    state.latest_orchestration["final_error"] = p.final_error

            case EventType.FEEDBACK_INJECTED:
                p = FeedbackInjectedPayload(**event.payload)
                state.feedbacks.append(p)

    return state
