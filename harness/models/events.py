from __future__ import annotations

from enum import Enum
from typing import Any, Literal

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
    RUN_COMMAND = "RunCommand"
    FEEDBACK_INJECTED = "FeedbackInjected"
    PLAN_CREATED = "PlanCreated"
    DAG_STEP_STARTED = "DagStepStarted"
    DAG_STEP_COMPLETED = "DagStepCompleted"
    DAG_STEP_FAILED = "DagStepFailed"
    PLAN_REVISED = "PlanRevised"
    PLAN_COMPLETED = "PlanCompleted"
    PLAN_FAILED = "PlanFailed"
    CONVERSATION_STARTED = "ConversationStarted"
    CONVERSATION_MESSAGE = "ConversationMessage"
    CONVERSATION_ENDED = "ConversationEnded"
    RUN_ORPHANED = "RunOrphaned"
    EPISODE_ARCHIVED = "EpisodeArchived"
    CONTEXT_PRUNED = "ContextPruned"


class ToolResultType(str, Enum):
    SUCCESS = "success"
    SOFT_ERROR = "soft_error"


# ── Payload Models ──────────────────────────────────────────────


class RunStartedPayload(BaseModel):
    intent: str
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None


class ConversationStartedPayload(BaseModel):
    conversation_id: str
    title: str
    user_id: str = "default"


class ConversationMessagePayload(BaseModel):
    conversation_id: str
    run_id: str
    role: str
    content: str


class ConversationEndedPayload(BaseModel):
    conversation_id: str
    summary: str = ""


class AgentThoughtPayload(BaseModel):
    thought: str
    tool_choice: str | None = None
    token_count: int = 0
    tool_calls: list[str] | None = None


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
    result_type: ToolResultType = ToolResultType.SUCCESS
    error: str | None = None


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


class Episode(BaseModel):
    """Structured episode memory unit.

    Replaces the old EpisodeSummary in v3.0 Phase 1.  All episode-shaped
    data is now an Episode.
    """
    episode_range: tuple[int, int]
    original_tokens: int
    compressed_tokens: int
    key_decisions: list[str]
    tools_used: list[str]
    key_findings: list[str]
    errors_encountered: list[str]
    current_plan: str | None = None
    original_event_refs: list[int]

    # v3.0 Phase 1 extensions
    title: str
    summary: str = ""
    importance_score: float = 0.0
    embedding: list[float] | None = None
    parent_episode_id: str | None = None
    format: str = "structured"


class ContextCompressedPayload(BaseModel):
    original_tokens: int
    compressed_tokens: int
    summary_ref: Episode | str
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
    result_summary: str | None = None


class RunCommandPayload(BaseModel):
    command: Literal["hard_abort", "soft_abort", "pause", "resume", "skip_tool"]
    reason: str = ""
    affected_tool: str | None = None
    issued_by: str = "monitor"


class FeedbackCategory(str, Enum):
    TOOL_FAILURE = "tool_failure"
    TOKEN_WARNING = "token_warning"
    REPEATED_CALL = "repeated_call"
    GUARDRAIL_TRIGGERED = "guardrail_triggered"
    OPERATOR_ADVICE = "operator_advice"
    CONDITION_RESOLVED = "condition_resolved"


class FeedbackSource(str, Enum):
    MONITOR = "monitor"
    OPERATOR = "operator"


class FeedbackInjectedPayload(BaseModel):
    feedback_id: str = ""
    source: FeedbackSource = FeedbackSource.MONITOR
    category: FeedbackCategory = FeedbackCategory.OPERATOR_ADVICE
    feedback_text: str
    priority: Literal["high", "medium", "low"] = "medium"
    affected_tool: str | None = None
    error_type: str | None = None
    error_detail: str | None = None
    suggestion: str | None = None
    expires_at_seq: int | None = None
    resolves_feedback_id: str | None = None
    consumed_at_seq: int | None = None
    injected_at_seq: int | None = None

    @staticmethod
    def compute_feedback_id(run_id: str, category: str, field_a: str, field_b: str) -> str:
        """Deterministic hash — same input always produces same ID."""
        import hashlib
        raw = f"{run_id}:{category}:{field_a}:{field_b}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class PlanCreatedPayload(BaseModel):
    plan_id: str
    intent: str
    steps_summary: str
    layer_count: int = 0


class DagStepStartedPayload(BaseModel):
    plan_id: str
    step_id: str
    tool_name: str
    depends_on: list[str] = Field(default_factory=list)


class DagStepCompletedPayload(BaseModel):
    plan_id: str
    step_id: str
    output_summary: str = ""
    status: str = "completed"
    error: str | None = None


class DagStepFailedPayload(BaseModel):
    plan_id: str
    step_id: str
    error: str
    retryable: bool = False
    tool_name: str = ""


class PlanRevisedPayload(BaseModel):
    plan_id: str
    revision_reason: str
    intent: str = ""
    remaining_steps_summary: str = ""


class PlanCompletedPayload(BaseModel):
    plan_id: str
    completed_steps: int
    total_layers: int = 0
    summary: str = ""


class PlanFailedPayload(BaseModel):
    plan_id: str
    completed_steps: int
    total_layers: int = 0
    final_error: str


class RunOrphanedPayload(BaseModel):
    reason: str = "server_restart"
    detected_at: float = 0.0


class EpisodeArchivedPayload(BaseModel):
    original_tokens: int
    compressed_tokens: int
    episode: Episode
    keep_recent_count: int
    archived_event_refs: list[int]


class ContextPrunedPayload(BaseModel):
    pruned_event_refs: list[int]
    pruned_token_count: int
    pruned_seq_count: int
    reason: str = "lazy_clear"


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
    EventType.RUN_COMMAND: RunCommandPayload,
    EventType.FEEDBACK_INJECTED: FeedbackInjectedPayload,
    EventType.PLAN_CREATED: PlanCreatedPayload,
    EventType.DAG_STEP_STARTED: DagStepStartedPayload,
    EventType.DAG_STEP_COMPLETED: DagStepCompletedPayload,
    EventType.DAG_STEP_FAILED: DagStepFailedPayload,
    EventType.PLAN_REVISED: PlanRevisedPayload,
    EventType.PLAN_COMPLETED: PlanCompletedPayload,
    EventType.PLAN_FAILED: PlanFailedPayload,
    EventType.CONVERSATION_STARTED: ConversationStartedPayload,
    EventType.CONVERSATION_MESSAGE: ConversationMessagePayload,
    EventType.CONVERSATION_ENDED: ConversationEndedPayload,
    EventType.RUN_ORPHANED: RunOrphanedPayload,
    EventType.EPISODE_ARCHIVED: EpisodeArchivedPayload,
    EventType.CONTEXT_PRUNED: ContextPrunedPayload,
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
