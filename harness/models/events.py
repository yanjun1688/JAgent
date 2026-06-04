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
    ORCHESTRATION_STARTED = "OrchestrationStarted"
    STEP_COMPLETED = "StepCompleted"
    STEP_FAILED = "StepFailed"
    ORCHESTRATION_COMPLETED = "OrchestrationCompleted"
    ORCHESTRATION_FAILED = "OrchestrationFailed"
    FEEDBACK_INJECTED = "FeedbackInjected"


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
    idempotency_key: str | None = None


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


class EpisodeSummary(BaseModel):
    episode_range: tuple[int, int]
    original_tokens: int
    compressed_tokens: int
    key_decisions: list[str]
    tools_used: list[str]
    key_findings: list[str]
    errors_encountered: list[str]
    current_plan: str | None = None
    original_event_refs: list[int]


class ContextCompressedPayload(BaseModel):
    original_tokens: int
    compressed_tokens: int
    summary_ref: EpisodeSummary | str
    keep_recent_count: int = 0


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


class OrchestrationStartedPayload(BaseModel):
    plan_id: str
    intent: str
    steps_summary: str


class StepCompletedPayload(BaseModel):
    plan_id: str
    step_index: int
    tool_call_id: str
    output: Any


class StepFailedPayload(BaseModel):
    plan_id: str
    step_index: int
    tool_call_id: str
    error: str


class OrchestrationCompletedPayload(BaseModel):
    plan_id: str
    completed_steps: int
    summary: str


class OrchestrationFailedPayload(BaseModel):
    plan_id: str
    completed_steps: int
    final_error: str


from typing import Literal


class FeedbackInjectedPayload(BaseModel):
    feedback_text: str
    priority: Literal["high", "medium"] = "medium"


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
    EventType.ORCHESTRATION_STARTED: OrchestrationStartedPayload,
    EventType.STEP_COMPLETED: StepCompletedPayload,
    EventType.STEP_FAILED: StepFailedPayload,
    EventType.ORCHESTRATION_COMPLETED: OrchestrationCompletedPayload,
    EventType.ORCHESTRATION_FAILED: OrchestrationFailedPayload,
    EventType.FEEDBACK_INJECTED: FeedbackInjectedPayload,
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
