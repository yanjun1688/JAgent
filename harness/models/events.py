from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    RUN_STARTED = "RunStarted"
    AGENT_THOUGHT = "AgentThought"
    TOOL_CALLED = "ToolCalled"
    TOOL_COMPLETED = "ToolCompleted"
    TOOL_FAILED = "ToolFailed"
    TOOL_TIMEOUT = "ToolTimeout"
    GUARDRAIL_TRIGGERED = "GuardrailTriggered"
    CONFIRMATION_REQUESTED = "ConfirmationRequested"
    CONFIRMATION_RECEIVED = "ConfirmationReceived"
    CONTEXT_COMPRESSED = "ContextCompressed"
    CONTEXT_CHECKPOINTED = "ContextCheckpointed"
    RUN_PAUSED = "RunPaused"
    RUN_RESUMED = "RunResumed"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"


# ── Payload Models ──────────────────────────────────────────────


class RunStartedPayload(BaseModel):
    intent: str
    context_snapshot: dict[str, Any] = Field(default_factory=dict)


class AgentThoughtPayload(BaseModel):
    thought: str
    tool_choice: str | None = None
    token_count: int = 0


class ToolCalledPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    input: dict[str, Any]
    idempotency_key: str


class ToolCompletedPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    output: Any
    duration_ms: int


class ToolFailedPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    error: str
    retryable: bool = False


class ToolTimeoutPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    timeout_ms: int


class GuardrailTriggeredPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    guardrail_id: str
    reason: str


class ConfirmationRequestedPayload(BaseModel):
    confirmation_id: str
    tool_call_id: str
    tool_name: str
    input: dict[str, Any]
    idempotency_key: str
    risk_level: str = "medium"


class ConfirmationReceivedPayload(BaseModel):
    confirmation_id: str
    confirmed: bool
    operator_id: str


class ContextCompressedPayload(BaseModel):
    original_tokens: int
    compressed_tokens: int
    summary_ref: str


class ContextCheckpointedPayload(BaseModel):
    checkpoint_seq: int
    snapshot_ref: str
    token_count: int


class RunPausedPayload(BaseModel):
    reason: str


class RunResumedPayload(BaseModel):
    resume_from_seq: int


class RunCompletedPayload(BaseModel):
    result_summary: str


class RunFailedPayload(BaseModel):
    final_error: str
    event_count: int


# ── Payload model registry ─────────────────────────────────────

PAYLOAD_MODEL_MAP: dict[EventType, type[BaseModel]] = {
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.AGENT_THOUGHT: AgentThoughtPayload,
    EventType.TOOL_CALLED: ToolCalledPayload,
    EventType.TOOL_COMPLETED: ToolCompletedPayload,
    EventType.TOOL_FAILED: ToolFailedPayload,
    EventType.TOOL_TIMEOUT: ToolTimeoutPayload,
    EventType.GUARDRAIL_TRIGGERED: GuardrailTriggeredPayload,
    EventType.CONFIRMATION_REQUESTED: ConfirmationRequestedPayload,
    EventType.CONFIRMATION_RECEIVED: ConfirmationReceivedPayload,
    EventType.CONTEXT_COMPRESSED: ContextCompressedPayload,
    EventType.CONTEXT_CHECKPOINTED: ContextCheckpointedPayload,
    EventType.RUN_PAUSED: RunPausedPayload,
    EventType.RUN_RESUMED: RunResumedPayload,
    EventType.RUN_COMPLETED: RunCompletedPayload,
    EventType.RUN_FAILED: RunFailedPayload,
}


# ── Event model ────────────────────────────────────────────────


class Event(BaseModel):
    run_id: str
    seq: int
    event_type: EventType
    payload: dict[str, Any]
    idempotency_key: str | None = None
    created_at: float

    def parsed_payload(self) -> BaseModel:
        model_cls = PAYLOAD_MODEL_MAP[self.event_type]
        return model_cls(**self.payload)
