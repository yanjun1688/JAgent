from __future__ import annotations

from enum import Enum
from typing import Any, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from harness.models.intent import DeliveryContract


class EventType(str, Enum):
    """Consolidated event taxonomy (R-events 2026-09-12).

    One member per *aggregate lifecycle*; the specific transition lives in the
    payload's ``phase`` discriminator (Pydantic discriminated union). This keeps
    the event surface small while preserving strong per-phase payload typing —
    see the ``*Payload`` union aliases below.

    Singletons (thought / feedback / delivery-contracts) keep a dedicated member
    because they carry no lifecycle dimension.
    """

    # Aggregate lifecycles (phase-discriminated).
    RUN = "Run"
    PLAN = "Plan"
    STEP = "Step"
    STEP_REPAIR = "StepRepair"
    TOOL = "Tool"
    CONFIRMATION = "Confirmation"
    CONTEXT = "Context"
    CONVERSATION = "Conversation"
    WORKSPACE = "Workspace"
    DIAGNOSTIC = "Diagnostic"
    # Singletons.
    AGENT_THOUGHT = "AgentThought"
    FEEDBACK_INJECTED = "FeedbackInjected"
    DELIVERY_CONTRACTS_RESOLVED = "DeliveryContractsResolved"


# Run terminal phases (唯一真源). ``orphaned`` is a diagnostic flag, not a
# terminal state — it is later converged by RUN failed; consumers needing the
# "no longer driven" semantic add ``orphaned`` explicitly via ``is_run_stopped``.
_TERMINAL_RUN_PHASES: frozenset[str] = frozenset({"completed", "failed"})


def is_terminal_event(event_type: EventType, payload: dict[str, Any]) -> bool:
    """Single source of "this event terminates the run" (replaces the old
    enum-set check, which can no longer be expressed on the aggregate type)."""
    return event_type is EventType.RUN and payload.get("phase") in _TERMINAL_RUN_PHASES


def is_run_stopped(event_type: EventType, payload: dict[str, Any]) -> bool:
    """Terminal OR orphaned — "this run will not be driven any further"."""
    return is_terminal_event(event_type, payload) or (
        event_type is EventType.RUN and payload.get("phase") == "orphaned"
    )


# Legacy (pre-consolidation) granular event-name → (aggregate, phase). Kept so
# declarative tool dependencies / external configs that still name a specific
# transition keep matching after the 41→13 consolidation.
_LEGACY_PHASE_NAMES: dict[str, tuple[EventType, str]] = {
    "RunStarted": (EventType.RUN, "started"),
    "RunPaused": (EventType.RUN, "paused"),
    "RunResumed": (EventType.RUN, "resumed"),
    "RunCompleted": (EventType.RUN, "completed"),
    "RunFailed": (EventType.RUN, "failed"),
    "RunOrphaned": (EventType.RUN, "orphaned"),
    "PlanCreated": (EventType.PLAN, "created"),
    "PlanRevised": (EventType.PLAN, "revised"),
    "PlanCompleted": (EventType.PLAN, "completed"),
    "PlanFailed": (EventType.PLAN, "failed"),
    "DagStepStarted": (EventType.STEP, "started"),
    "DagStepCompleted": (EventType.STEP, "completed"),
    "DagStepFailed": (EventType.STEP, "failed"),
    "DagStepSkipped": (EventType.STEP, "skipped"),
    "StepLocalRepairStarted": (EventType.STEP_REPAIR, "started"),
    "StepLocalRepairCompleted": (EventType.STEP_REPAIR, "completed"),
    "ToolCalled": (EventType.TOOL, "called"),
    "ToolCompleted": (EventType.TOOL, "completed"),
    "ToolFailed": (EventType.TOOL, "failed"),
    "ToolTimeout": (EventType.TOOL, "timeout"),
    "GuardrailTriggered": (EventType.TOOL, "blocked"),
    "OutputBlobStored": (EventType.TOOL, "output_stored"),
    "ConfirmationRequested": (EventType.CONFIRMATION, "requested"),
    "ConfirmationReceived": (EventType.CONFIRMATION, "received"),
    "ContextCheckpointed": (EventType.CONTEXT, "checkpointed"),
    "EpisodeArchived": (EventType.CONTEXT, "archived"),
    "ContextPruned": (EventType.CONTEXT, "pruned"),
    "ConversationStarted": (EventType.CONVERSATION, "started"),
    "ConversationMessage": (EventType.CONVERSATION, "message"),
    "WorkspaceCreated": (EventType.WORKSPACE, "created"),
    "WorkspaceUpdated": (EventType.WORKSPACE, "updated"),
    "WorkspaceDeleted": (EventType.WORKSPACE, "deleted"),
    "LateEventRejected": (EventType.DIAGNOSTIC, "late_event_rejected"),
    "PhaseTimedOut": (EventType.DIAGNOSTIC, "phase_timeout"),
    "TaskCleanupTimeout": (EventType.DIAGNOSTIC, "cleanup_timeout"),
}


def event_matches(event_type: EventType, payload: dict[str, Any], spec: str) -> bool:
    """Does ``(event_type, phase)`` satisfy ``spec``?

    ``spec`` may be an aggregate value (``"Tool"``) or a legacy granular name
    (``"ToolCompleted"``). Used by declarative dependency guardrails.
    """
    if event_type.value == spec:
        return True
    pair = _LEGACY_PHASE_NAMES.get(spec)
    return pair is not None and event_type is pair[0] and payload.get("phase") == pair[1]



class ToolResultType(str, Enum):
    SUCCESS = "success"
    UNSUCCESSFUL = "unsuccessful"


class EventPayload(BaseModel):
    """Base for all event payloads.

    ``extra="forbid"`` makes the aggregate Union parse unambiguous when a raw
    payload omits the explicit ``phase`` discriminator (unknown keys cannot
    leak into an unrelated phase model) and rejects malformed writes.
    """

    model_config = ConfigDict(extra="forbid")


# ── Payload Models ──────────────────────────────────────────────


class RunStartedPayload(EventPayload):
    phase: Literal["started"] = "started"
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


class DeliveryContractsResolvedPayload(EventPayload):
    contracts: list[DeliveryContract] = Field(default_factory=list)
    source: str = "extracted"
    timed_out: bool = False
    error: str | None = None


class ConversationStartedPayload(EventPayload):
    phase: Literal["started"] = "started"
    conversation_id: str
    title: str
    user_id: str = "default"


class ConversationMessagePayload(EventPayload):
    phase: Literal["message"] = "message"
    conversation_id: str
    run_id: str
    role: str
    content: str
    client_request_id: str | None = None


class AgentThoughtPayload(EventPayload):
    thought: str
    tool_choice: str | None = None
    tool_calls: list[str] | None = None


class ToolCalledPayload(EventPayload):
    phase: Literal["called"] = "called"
    tool_call_id: str
    tool_name: str
    input: dict[str, Any]
    idempotency_key: str | None = None
    step_id: str | None = None  # v2.2 (C, D6): 挂钩 DAG 步骤，实现 step↔tool JOIN


class ToolCompletedPayload(EventPayload):
    phase: Literal["completed"] = "completed"
    tool_call_id: str
    tool_name: str
    output: Any
    duration_ms: int
    result_type: ToolResultType = ToolResultType.SUCCESS
    error: str | None = None
    step_id: str | None = None  # v2.2 (C, D6)


class ToolFailedPayload(EventPayload):
    phase: Literal["failed"] = "failed"
    tool_call_id: str
    tool_name: str
    error: str
    retryable: bool = False
    step_id: str | None = None  # v2.2 (C, D6)


class ToolTimeoutPayload(EventPayload):
    phase: Literal["timeout"] = "timeout"
    tool_call_id: str
    tool_name: str
    timeout_ms: int
    step_id: str | None = None


class GuardrailTriggeredPayload(EventPayload):
    phase: Literal["blocked"] = "blocked"
    tool_call_id: str
    tool_name: str
    guardrail_id: str
    reason: str
    step_id: str | None = None
    workspace_id: str | None = None


class OutputBlobStoredPayload(EventPayload):
    """v3.4 (F-2, ADR-011): 超大工具输出被收口为 blob 引用。

    Tool Layer 在输出超过阈值时把完整体写入 workspace 作用域的 content-addressed
    blob，事件流里只保留摘要 + 引用；Agent 需要细节时用受信只读工具 ``fetch_output``
    按 ``ref`` 取回。受信组件（fold/门控/replay）只持引用，fold 内不做 I/O。
    """

    phase: Literal["output_stored"] = "output_stored"
    tool_call_id: str
    tool_name: str
    ref: str
    sha256: str
    bytes: int
    summary: str = ""
    truncated: bool = True
    step_id: str | None = None
    workspace_id: str | None = None


class WorkspaceCreatedPayload(EventPayload):
    phase: Literal["created"] = "created"
    workspace_id: str
    tenant_id: str
    name: str
    description: str = ""
    scope: dict[str, Any]
    actor: str = "operator"


class WorkspaceUpdatedPayload(EventPayload):
    phase: Literal["updated"] = "updated"
    workspace_id: str
    tenant_id: str
    changed_fields: list[str]
    old_values: dict[str, Any]
    new_values: dict[str, Any]
    actor: str = "operator"


class WorkspaceDeletedPayload(EventPayload):
    phase: Literal["deleted"] = "deleted"
    workspace_id: str
    tenant_id: str
    reason: str = ""
    actor: str = "operator"


class ConfirmationRequestedPayload(EventPayload):
    phase: Literal["requested"] = "requested"
    confirmation_id: str
    tool_call_id: str
    tool_name: str
    input: dict[str, Any]
    idempotency_key: str
    risk_level: str = "medium"
    step_id: str | None = None


class ConfirmationReceivedPayload(EventPayload):
    phase: Literal["received"] = "received"
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
    # ⑤: 高重要性项（失败/超时/guardrail_blocked/unsuccessful、决策 thought）的
    # 原文限长片段 —— 折叠为摘要时保留关键原始措辞，供 LLM 工作视图使用。
    preserved_excerpts: list[str] = Field(default_factory=list)

    def to_context_text(self) -> str:
        """Canonical single-source Episode → LLM working-view text.

        Used by every consumer that folds an Episode summary into model context
        (agent_kernel think, planner answer/revise builders). Adding an Episode
        field here — and only here — keeps all render sites in sync.
        """
        parts: list[str] = []
        if self.title:
            parts.append(f"Title: {self.title}")
        if self.summary:
            parts.append(f"Summary: {self.summary}")
        if self.key_decisions:
            parts.append(f"Key decisions: {', '.join(self.key_decisions)}")
        if self.tools_used:
            parts.append(f"Tools used: {', '.join(self.tools_used)}")
        if self.key_findings:
            parts.append(f"Key findings: {', '.join(self.key_findings)}")
        if self.errors_encountered:
            parts.append(f"Errors: {', '.join(self.errors_encountered)}")
        if self.current_plan:
            parts.append(f"Current plan: {self.current_plan}")
        if self.preserved_excerpts:
            parts.append("Preserved verbatim excerpts:\n- " + "\n- ".join(self.preserved_excerpts))
        return "\n".join(parts)


class ContextCheckpointedPayload(EventPayload):
    phase: Literal["checkpointed"] = "checkpointed"
    checkpoint_seq: int
    snapshot_ref: str


class RunPausedPayload(EventPayload):
    phase: Literal["paused"] = "paused"
    reason: str


class RunResumedPayload(EventPayload):
    phase: Literal["resumed"] = "resumed"
    resume_from_seq: int


class RunCompletedPayload(EventPayload):
    phase: Literal["completed"] = "completed"
    result_summary: str
    # v2.2 (D5): 机械达成证据 — 完成门=最终计划所有步骤 step_normal 聚合。
    # RUN_COMPLETED 携带 all_normal + unmet_step_ids，反查即得机械检查结果（洞 5 修复）。
    all_normal: bool = True
    unmet_step_ids: list[str] = Field(default_factory=list)
    # S06 (D-03/D-04): 交付契约维度 — 与机械完成正交分层。空契约（旧请求）显式
    # 标记 deliverable_status="unverified"，禁止宣称交付达成。
    # 布尔 deliverable_met 由此派生（== "met"），不再作为独立字段存储。
    deliverable_status: str = "unverified"  # "met" | "unverified" | "failed"
    deliverable_summary: list[dict[str, Any]] = Field(default_factory=list)


class RunFailedPayload(EventPayload):
    phase: Literal["failed"] = "failed"
    final_error: str
    event_count: int
    result_summary: str | None = None
    user_facing_message: str = "任务未能完成，请检查任务要求或稍后重试。"


class FeedbackCategory(str, Enum):
    TOOL_FAILURE = "tool_failure"
    REPEATED_CALL = "repeated_call"
    GUARDRAIL_TRIGGERED = "guardrail_triggered"
    OPERATOR_ADVICE = "operator_advice"
    CONDITION_RESOLVED = "condition_resolved"


class FeedbackSource(str, Enum):
    MONITOR = "monitor"
    OPERATOR = "operator"


class FeedbackInjectedPayload(EventPayload):
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


class PlanCreatedPayload(EventPayload):
    phase: Literal["created"] = "created"
    plan_id: str
    intent: str
    steps_summary: str
    layer_count: int = 0
    # v2.2 (C, D6): 计划结构落事件 — 事后可重建 DAG 蓝图
    steps: list[dict[str, Any]] = Field(default_factory=list)


class DagStepStartedPayload(EventPayload):
    phase: Literal["started"] = "started"
    plan_id: str
    step_id: str
    tool_name: str
    depends_on: list[str] = Field(default_factory=list)


class DagStepCompletedPayload(EventPayload):
    # extra=forbid: keeps the smart-union parse unambiguous vs SKIPPED/FAILED
    # when a raw payload lacks the explicit phase discriminator.
    model_config = ConfigDict(extra="forbid")

    phase: Literal["completed"] = "completed"
    plan_id: str
    step_id: str
    output_summary: str = ""
    status: str = "completed"
    error: str | None = None
    tool_call_id: str | None = None  # v2.2 (C, D6): 挂钩工具调用


class DagStepFailedPayload(EventPayload):
    phase: Literal["failed"] = "failed"
    plan_id: str
    step_id: str
    error: str
    retryable: bool = False
    tool_name: str = ""
    tool_call_id: str | None = None  # v2.2 (C, D6)


class DagStepSkippedPayload(EventPayload):
    """v2.2 (D9): 下游门控产生的 SKIPPED 记录 — 依赖步骤非 normal 时本步不执行。

    可观测、可审计：前端/分析 API 可区分"被系统跳过"与"执行失败"。
    """

    model_config = ConfigDict(extra="forbid")

    phase: Literal["skipped"] = "skipped"
    plan_id: str
    step_id: str
    reason: str = "dep_not_normal"
    tool_name: str = ""

class PlanRevisedPayload(EventPayload):
    phase: Literal["revised"] = "revised"
    plan_id: str
    revision_reason: str
    intent: str = ""
    remaining_steps_summary: str = ""
    # v2.2 (C, D6): 修订后计划结构落事件
    steps: list[dict[str, Any]] = Field(default_factory=list)
    # v2.2 (D11): LLM 的 task_state 审计便签落事件（供审计 + 未来 LLM vs 系统差异展示）。
    # 纯观测，不参与任何受信判定（约束 4）。
    step_tasks: dict[str, str] = Field(default_factory=dict)


class PlanCompletedPayload(EventPayload):
    phase: Literal["completed"] = "completed"
    plan_id: str
    completed_steps: int
    total_layers: int = 0
    summary: str = ""


class PlanFailedPayload(EventPayload):
    phase: Literal["failed"] = "failed"
    plan_id: str
    completed_steps: int
    total_layers: int = 0
    final_error: str


class RunOrphanedPayload(EventPayload):
    phase: Literal["orphaned"] = "orphaned"
    reason: str = "server_restart"
    detected_at: float = 0.0


class EpisodeArchivedPayload(EventPayload):
    phase: Literal["archived"] = "archived"
    episode: Episode
    keep_recent_count: int
    archived_event_refs: list[int]


class ContextPrunedPayload(EventPayload):
    phase: Literal["pruned"] = "pruned"
    pruned_event_refs: list[int]
    pruned_seq_count: int
    reason: str = "lazy_clear"


class LateEventRejectedPayload(EventPayload):
    """S09 (D-06 / L-03): Run 终态后尝试写入 run 事件流被拦截的结构化记录。

    ``seq`` 为尝试写入时 Run 已推进到的 seq；``phase``/``event_type`` 为被拦截
    事件的类型与 phase。仅作用于 run 事件流，EventStore 全局 append 不受影响。
    """

    phase: Literal["late_event_rejected"] = "late_event_rejected"
    seq: int
    event_type: str
    reason: str


class PhaseTimedOutPayload(EventPayload):
    """S10 (问题八): 单个执行阶段超时（classify/plan/revise/tool/answer）。"""

    phase: Literal["phase_timeout"] = "phase_timeout"
    timed_out_phase: str
    budget_ms: int


class TaskCleanupTimeoutPayload(EventPayload):
    """S10 (C-03 / D-06): 取消宽限期后子任务仍未回收 — 强制 cleanup 的结构化告警。"""

    phase: Literal["cleanup_timeout"] = "cleanup_timeout"
    pending_count: int
    grace_ms: int


class PlanRejectedPayload(EventPayload):
    """R6: a non-trusted planner output was rejected by a trusted component.

    The planner parsed the LLM output into a plan, but a trusted rule
    (PlanGuardrail, degenerate-revision guard, delivery invariants, or the
    post-merge re-check) refused it. ``stage`` names the rejecting seam,
    ``attempt`` the in-call try number, ``reasons`` the same trusted verdicts
    fed back to the LLM, and ``recovered`` whether the system kept driving
    (True) or immediately failed the run (False).
    """

    phase: Literal["plan_rejected"] = "plan_rejected"
    stage: Literal["plan", "revise", "merge", "degenerate", "invariant"]
    attempt: int
    reasons: list[str]
    recovered: bool


class StepLocalRepairStartedPayload(EventPayload):
    """v3.4 (F-6, ADR-011): Scheduler 开始对单个失败步骤做 bounded 局部修复。

    触发：DAG 步骤失败、工具级 retry 用尽、且该失败分级为非瞬时（step_repair）。
    边界字段（repair_round / budget_remaining / tool_whitelist）由受信预算计算，
    落事件供可观测与 Replay 审计。
    """

    phase: Literal["started"] = "started"
    step_id: str
    plan_id: str = ""
    repair_round: int = 1
    budget_remaining: int = 0
    step_tool: str = ""
    error: str | None = None
    tool_whitelist: list[str] = Field(default_factory=list)


class StepLocalRepairCompletedPayload(EventPayload):
    """v3.4 (F-6, ADR-011): 局部修复轮次结束。

    outcome 由受信预算决定（LLM 内容不参与判定）：
    - ``accepted``：建议动作经受信校验，已替换该步骤动作并重跑；
    - ``rejected``：建议被受信预算机械拒绝（越权工具 / mutating / 超预算）；
    - ``exhausted``：修复预算耗尽，升级到全局 revise。
    """

    phase: Literal["completed"] = "completed"
    step_id: str
    plan_id: str = ""
    repair_round: int = 1
    outcome: Literal["accepted", "rejected", "exhausted"] = "rejected"
    tool_name: str = ""
    proposed_tool: str = ""
    error: str | None = None
    reason: str = ""


# ── Aggregate payload unions ───────────────────────────────────

RunPayload: TypeAlias = Union[
    RunStartedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunOrphanedPayload,
]

PlanPayload: TypeAlias = Union[
    PlanCreatedPayload,
    PlanRevisedPayload,
    PlanFailedPayload,
    PlanCompletedPayload,
]

StepPayload: TypeAlias = Union[
    DagStepStartedPayload,
    DagStepFailedPayload,
    DagStepSkippedPayload,
    DagStepCompletedPayload,
]

StepRepairPayload: TypeAlias = Union[StepLocalRepairStartedPayload, StepLocalRepairCompletedPayload]

ToolPayload: TypeAlias = Union[
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolTimeoutPayload,
    GuardrailTriggeredPayload,
    OutputBlobStoredPayload,
]

ConfirmationPayload: TypeAlias = Union[ConfirmationRequestedPayload, ConfirmationReceivedPayload]

ContextPayload: TypeAlias = Union[ContextCheckpointedPayload, EpisodeArchivedPayload, ContextPrunedPayload]

ConversationPayload: TypeAlias = Union[ConversationStartedPayload, ConversationMessagePayload]

WorkspacePayload: TypeAlias = Union[WorkspaceCreatedPayload, WorkspaceUpdatedPayload, WorkspaceDeletedPayload]

DiagnosticPayload: TypeAlias = Union[
    LateEventRejectedPayload,
    PhaseTimedOutPayload,
    TaskCleanupTimeoutPayload,
    PlanRejectedPayload,
]


# ── Payload model registry ─────────────────────────────────────

PAYLOAD_MODEL_MAP: dict[EventType, Any] = {
    EventType.RUN: RunPayload,
    EventType.PLAN: PlanPayload,
    EventType.STEP: StepPayload,
    EventType.STEP_REPAIR: StepRepairPayload,
    EventType.TOOL: ToolPayload,
    EventType.CONFIRMATION: ConfirmationPayload,
    EventType.CONTEXT: ContextPayload,
    EventType.CONVERSATION: ConversationPayload,
    EventType.WORKSPACE: WorkspacePayload,
    EventType.DIAGNOSTIC: DiagnosticPayload,
    EventType.AGENT_THOUGHT: AgentThoughtPayload,
    EventType.FEEDBACK_INJECTED: FeedbackInjectedPayload,
    EventType.DELIVERY_CONTRACTS_RESOLVED: DeliveryContractsResolvedPayload,
}

# Pre-built validators (one per aggregate) — the single parse seam.
_PAYLOAD_ADAPTERS: dict[EventType, TypeAdapter] = {
    event_type: TypeAdapter(model) for event_type, model in PAYLOAD_MODEL_MAP.items()
}


def _build_payload_class_map() -> dict[type[BaseModel], EventType]:
    """Reverse index: concrete payload class → its aggregate EventType.

    Needed by tooling (fixtures/replay) that starts from a payload model and
    must recover the event type now that one EventType owns several phases.
    """
    mapping: dict[type[BaseModel], EventType] = {}
    import typing

    for event_type, model in PAYLOAD_MODEL_MAP.items():
        args = typing.get_args(model)
        if not args:
            args = (model,)
        for cls in args:
            mapping[cls] = event_type
    return mapping


PAYLOAD_CLASS_TO_TYPE: dict[type[BaseModel], EventType] = _build_payload_class_map()



def parse_event_payload(event_type: EventType, payload: dict[str, Any]) -> BaseModel:
    """Validate + materialize an event payload against its aggregate union.

    Single entry point for both write-time validation (EventStore) and
    read-time parsing (``Event.parsed_payload()``). Raises on unknown type.
    """
    adapter = _PAYLOAD_ADAPTERS.get(event_type)
    if adapter is None:
        raise ValueError(f"Unknown event type: {event_type}")
    return adapter.validate_python(payload)


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

    @property
    def phase(self) -> str | None:
        """The lifecycle discriminator of this event, if it has one."""
        return self.payload.get("phase")

    @model_validator(mode="after")
    def _normalize_payload_phase(self) -> "Event":
        """Backfill the ``phase`` discriminator for payloads built without it.

        Production writers always dump a payload model (phase included); this
        keeps directly-constructed ``Event`` objects (tests, fixtures, replay
        reconstruction of legacy rows) consistent so fold/consumers can rely on
        ``payload["phase"]``. Unparseable payloads are left untouched — write
        validation rejects them separately.
        """
        if "phase" not in self.payload:
            try:
                parsed = parse_event_payload(self.event_type, self.payload)
            except Exception:
                return self
            self.payload = parsed.model_dump(mode="json")
        return self

    def parsed_payload(self) -> BaseModel:
        return parse_event_payload(self.event_type, self.payload)
