from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.models.intent import DeliveryContract


class EventType(str, Enum):
    RUN_STARTED = "RunStarted"
    DELIVERY_CONTRACTS_RESOLVED = "DeliveryContractsResolved"
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
    DAG_STEP_SKIPPED = "DagStepSkipped"
    PLAN_REVISED = "PlanRevised"
    PLAN_COMPLETED = "PlanCompleted"
    PLAN_FAILED = "PlanFailed"
    CONVERSATION_STARTED = "ConversationStarted"
    CONVERSATION_MESSAGE = "ConversationMessage"
    CONVERSATION_ENDED = "ConversationEnded"
    RUN_ORPHANED = "RunOrphaned"
    EPISODE_ARCHIVED = "EpisodeArchived"
    CONTEXT_PRUNED = "ContextPruned"
    WORKSPACE_CREATED = "WorkspaceCreated"
    WORKSPACE_UPDATED = "WorkspaceUpdated"
    WORKSPACE_DELETED = "WorkspaceDeleted"
    # S09 (D-06): 终态后迟到事件被拦截 — 结构化记录，供观测（L-03 只作用于 run 流）。
    LATE_EVENT_REJECTED = "LateEventRejected"
    # S10 (问题八 / C-03 / D-06): 分阶段超时 + 子任务清理超时
    PHASE_TIMED_OUT = "PhaseTimedOut"
    TASK_CLEANUP_TIMEOUT = "TaskCleanupTimeout"


class ToolResultType(str, Enum):
    SUCCESS = "success"
    UNSUCCESSFUL = "unsuccessful"


# ── Payload Models ──────────────────────────────────────────────


class RunStartedPayload(BaseModel):
    intent: str
    current_request: str | None = None
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None
    workspace_id: str | None = None
    # S05 (D-02 / C-01): 原始用户请求（不可变受信数据）+ 交付契约列表。
    # intent_raw 是用户原话；Planner/Reviser 不得覆盖。contracts 来源
    # caller / extracted（S07 接线），全部经受信完成门判定。
    intent_raw: str | None = None
    contracts: list[DeliveryContract] = Field(default_factory=list)
    # S07 (D-02 / 方案 B): caller 未提供 required_operations 时标记 True，
    # scheduler 在首轮 plan 前强制执行契约解析（run 内异步前置，不阻塞 API）。
    requires_contract_extraction: bool = False


class DeliveryContractsResolvedPayload(BaseModel):
    contracts: list[DeliveryContract] = Field(default_factory=list)
    source: str = "extracted"
    timed_out: bool = False
    error: str | None = None


class ConversationStartedPayload(BaseModel):
    conversation_id: str
    title: str
    user_id: str = "default"


class ConversationMessagePayload(BaseModel):
    conversation_id: str
    run_id: str
    role: str
    content: str
    client_request_id: str | None = None


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
    step_id: str | None = None  # v2.2 (C, D6): 挂钩 DAG 步骤，实现 step↔tool JOIN


class ToolCompletedPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    output: Any
    duration_ms: int
    result_type: ToolResultType = ToolResultType.SUCCESS
    error: str | None = None
    step_id: str | None = None  # v2.2 (C, D6)


class ToolFailedPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    error: str
    retryable: bool = False
    step_id: str | None = None  # v2.2 (C, D6)


class ToolTimeoutPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    timeout_ms: int
    step_id: str | None = None


class GuardrailTriggeredPayload(BaseModel):
    tool_call_id: str
    tool_name: str
    guardrail_id: str
    reason: str
    step_id: str | None = None
    workspace_id: str | None = None


class WorkspaceCreatedPayload(BaseModel):
    workspace_id: str
    tenant_id: str
    name: str
    description: str = ""
    scope: dict[str, Any]
    actor: str = "operator"


class WorkspaceUpdatedPayload(BaseModel):
    workspace_id: str
    tenant_id: str
    changed_fields: list[str]
    old_values: dict[str, Any]
    new_values: dict[str, Any]
    actor: str = "operator"


class WorkspaceDeletedPayload(BaseModel):
    workspace_id: str
    tenant_id: str
    reason: str = ""
    actor: str = "operator"


class ConfirmationRequestedPayload(BaseModel):
    confirmation_id: str
    tool_call_id: str
    tool_name: str
    input: dict[str, Any]
    idempotency_key: str
    risk_level: str = "medium"
    step_id: str | None = None


class ConfirmationReceivedPayload(BaseModel):
    confirmation_id: str
    confirmed: bool
    operator_id: str
    step_id: str | None = None


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
    # v2.2 (D5): 机械达成证据 — 完成门=最终计划所有步骤 step_normal 聚合。
    # RUN_COMPLETED 携带 all_normal + unmet_step_ids，反查即得机械检查结果（洞 5 修复）。
    all_normal: bool = True
    unmet_step_ids: list[str] = Field(default_factory=list)
    # S06 (D-03/D-04): 交付契约维度 — 与机械完成正交分层。空契约（旧请求）显式
    # 标记 deliverable_met=False + deliverable_status="unverified"，禁止宣称交付达成。
    deliverable_met: bool | None = None
    deliverable_status: str = "unverified"  # "met" | "unverified" | "failed"
    deliverable_summary: list[dict[str, Any]] = Field(default_factory=list)


class RunFailedPayload(BaseModel):
    final_error: str
    event_count: int
    result_summary: str | None = None
    user_facing_message: str = "任务未能完成，请检查任务要求或稍后重试。"


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
    # v2.2 (C, D6): 计划结构落事件 — 事后可重建 DAG 蓝图
    steps: list[dict[str, Any]] = Field(default_factory=list)


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
    tool_call_id: str | None = None  # v2.2 (C, D6): 挂钩工具调用


class DagStepFailedPayload(BaseModel):
    plan_id: str
    step_id: str
    error: str
    retryable: bool = False
    tool_name: str = ""
    tool_call_id: str | None = None  # v2.2 (C, D6)


class DagStepSkippedPayload(BaseModel):
    """v2.2 (D9): 下游门控产生的 SKIPPED 记录 — 依赖步骤非 normal 时本步不执行。

    可观测、可审计：前端/分析 API 可区分"被系统跳过"与"执行失败"。
    """

    plan_id: str
    step_id: str
    reason: str = "dep_not_normal"
    tool_name: str = ""


class PlanRevisedPayload(BaseModel):
    plan_id: str
    revision_reason: str
    intent: str = ""
    remaining_steps_summary: str = ""
    # v2.2 (C, D6): 修订后计划结构落事件
    steps: list[dict[str, Any]] = Field(default_factory=list)
    # v2.2 (D11): LLM 的 task_state 审计便签落事件（供审计 + 未来 LLM vs 系统差异展示）。
    # 纯观测，不参与任何受信判定（约束 4）。
    step_tasks: dict[str, str] = Field(default_factory=dict)


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


class LateEventRejectedPayload(BaseModel):
    """S09 (D-06 / L-03): Run 终态后尝试写入 run 事件流被拦截的结构化记录。

    ``seq`` 为尝试写入时 Run 已推进到的 seq；``event_type`` 为被拦截的事件类型。
    仅作用于 run 事件流，EventStore 全局 append 不受影响（workspace/conversation
    审计事件复用同一表，不误伤）。
    """

    seq: int
    event_type: str
    reason: str


class PhaseTimedOutPayload(BaseModel):
    """S10 (问题八): 单个执行阶段超时（classify/plan/revise/tool/answer）。"""

    phase: str
    budget_ms: int


class TaskCleanupTimeoutPayload(BaseModel):
    """S10 (C-03 / D-06): 取消宽限期后子任务仍未回收 — 强制 cleanup 的结构化告警。"""

    pending_count: int
    grace_ms: int


# ── Payload model registry ─────────────────────────────────────

PAYLOAD_MODEL_MAP: dict[EventType, type[BaseModel]] = {
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.DELIVERY_CONTRACTS_RESOLVED: DeliveryContractsResolvedPayload,
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
    EventType.DAG_STEP_SKIPPED: DagStepSkippedPayload,
    EventType.PLAN_REVISED: PlanRevisedPayload,
    EventType.PLAN_COMPLETED: PlanCompletedPayload,
    EventType.PLAN_FAILED: PlanFailedPayload,
    EventType.CONVERSATION_STARTED: ConversationStartedPayload,
    EventType.CONVERSATION_MESSAGE: ConversationMessagePayload,
    EventType.CONVERSATION_ENDED: ConversationEndedPayload,
    EventType.RUN_ORPHANED: RunOrphanedPayload,
    EventType.EPISODE_ARCHIVED: EpisodeArchivedPayload,
    EventType.CONTEXT_PRUNED: ContextPrunedPayload,
    EventType.WORKSPACE_CREATED: WorkspaceCreatedPayload,
    EventType.WORKSPACE_UPDATED: WorkspaceUpdatedPayload,
    EventType.WORKSPACE_DELETED: WorkspaceDeletedPayload,
    EventType.LATE_EVENT_REJECTED: LateEventRejectedPayload,
    EventType.PHASE_TIMED_OUT: PhaseTimedOutPayload,
    EventType.TASK_CLEANUP_TIMEOUT: TaskCleanupTimeoutPayload,
}


# ── Event model ────────────────────────────────────────────────


class Event(BaseModel):
    run_id: str
    seq: int
    event_type: EventType
    payload: dict[str, Any]
    idempotency_key: str | None = None
    created_at: float
    tenant_id: str = "default"
    workspace_id: str | None = None
    is_audit: bool = False

    def parsed_payload(self) -> BaseModel:
        model_cls = PAYLOAD_MODEL_MAP[self.event_type]
        return model_cls(**self.payload)
