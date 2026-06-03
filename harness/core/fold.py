from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from harness.models.events import (
    AgentThoughtPayload,
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
    ContextCheckpointedPayload,
    ContextCompressedPayload,
    Event,
    EventType,
    GuardrailTriggeredPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
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
class ToolResult:
    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    output: object = None
    error: str | None = None
    duration_ms: int = 0
    idempotency_key: str | None = None


@dataclass
class RunState:
    run_id: str
    status: RunStatus = RunStatus.RUNNING
    seq: int = 0
    intent: str = ""
    context_snapshot: dict = field(default_factory=dict)
    thought_history: list[AgentThoughtPayload] = field(default_factory=list)
    latest_thought: AgentThoughtPayload | None = None
    tool_calls: list[ToolCalledPayload] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    last_error: str | None = None
    summary: str | None = None
    pause_reason: str | None = None
    pending_confirmations: list[ConfirmationRequestedPayload] = field(default_factory=list)
    last_checkpoint_seq: int | None = None


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
                state.thought_history.append(p)
                state.latest_thought = p

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

    return state
