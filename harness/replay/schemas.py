"""Pydantic v2 response models for the Event Replay Inspector (read-only).

These are OpenAPI wire shapes. The TypeScript frontend types are generated
from these models (see scripts/generate_openapi.py), so they are the single
source of truth for the replay API contract (AGENTS.md §4.1).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ── Timeline ────────────────────────────────────────────────────


class ReplayTimelineEvent(BaseModel):
    """A compact event-list entry for the scrubbable timeline."""

    seq: int
    event_type: str
    created_at: float
    payload: dict[str, Any]

    # Convenience anchors pulled from payload so the UI can label rows
    # without re-parsing every payload.
    tool_name: str | None = None
    tool_call_id: str | None = None
    step_id: str | None = None
    is_terminal: bool = False


class ReplayTimelineResponse(BaseModel):
    run_id: str
    latest_seq: int = 0
    total: int = 0
    timeline: list[ReplayTimelineEvent] = Field(default_factory=list)
    next_cursor: int = 0
    has_more: bool = False


class ReplayRunMeta(BaseModel):
    """Header/metadata for a run, including the (reserved) Langfuse link."""

    run_id: str
    status: str = "running"
    intent: str = ""
    latest_seq: int = 0
    event_count: int = 0
    created_at: float | None = None

    # Reserved for the future Langfuse cross-reference. Always ``None`` in
    # this read-only release; the UI only renders a jump link when this is
    # populated. See REPLAY_INSPECTOR_v1.0.md §Known limitations.
    langfuse_trace_url: str | None = None


# ── State at a point in time ────────────────────────────────────


class PlanStepView(BaseModel):
    step_id: str
    status: str = "pending"
    tool_name: str | None = None
    output_summary: str | None = None
    error: str | None = None
    reason: str | None = None
    tool_call_id: str | None = None


class PlanView(BaseModel):
    plan_id: str | None = None
    intent: str = ""
    status: str | None = None
    summary: str | None = None
    final_error: str | None = None
    steps: list[PlanStepView] = Field(default_factory=list)


class ToolResultView(BaseModel):
    tool_call_id: str
    tool_name: str
    status: str
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    event_seq: int = 0


class StepEvidenceView(BaseModel):
    """v3.4 (F-1, ADR-011): 受信执行态证据投影的只读视图.

    Mirrors :class:`harness.core.fold.StepEvidence` (wire-safe, self-contained —
    this module stays free of execution imports so the read path stays
    statically auditable). The evidence projection is folded from PLAN_*/DAG_STEP_*/
    TOOL_* events and is **never trimmed by compression** — exposing it here is
    what makes Replay's time-travel view survive EpisodeArchived/ContextPruned
    (AC-5). ``step_normal`` is the trusted gate predicate (completed/idempotent,
    or probe-UNSUCCESSFUL), reproduced for the UI.
    """

    step_id: str
    plan_id: str = ""
    tool_name: str = ""
    exec_state: str = "pending"
    depends_on: list[str] = Field(default_factory=list)
    output_summary: str = ""
    error: str | None = None
    tool_call_id: str | None = None
    output_ref: str | None = None
    output: Any = None
    probe: bool = False
    skip_reason: str | None = None
    updated_seq: int = 0
    step_normal: bool = False


class GuardrailBlockView(BaseModel):
    guardrail_id: str
    reason: str
    event_seq: int
    tool_call_id: str | None = None
    tool_name: str | None = None
    step_id: str | None = None


class PendingConfirmationView(BaseModel):
    confirmation_id: str
    tool_name: str
    risk_level: str = "medium"
    event_seq: int = 0


class RunStateView(BaseModel):
    """Complete reconstructed system state as-of ``at_seq``.

    This is a projection of the folded ``RunState`` (the single source of
    truth via ``fold_events``) into a stable, UI-independent read shape.
    """

    run_id: str
    at_seq: int
    latest_seq: int
    is_latest: bool
    status: str
    intent: str = ""

    last_error: str | None = None
    user_facing_message: str | None = None
    pause_reason: str | None = None
    completion_summary: str | None = None
    completion_evidence: dict[str, Any] = Field(default_factory=dict)

    plan: PlanView | None = None
    tool_results: list[ToolResultView] = Field(default_factory=list)
    guardrail_blocks: list[GuardrailBlockView] = Field(default_factory=list)
    pending_confirmations: list[PendingConfirmationView] = Field(default_factory=list)

    # v3.4 (F-1, ADR-011): 受信执行态证据投影（按 step_id 排序，确定性）。
    step_evidence: list[StepEvidenceView] = Field(default_factory=list)

    thought_count: int = 0
    orphaned: bool = False
    workspace_id: str | None = None
    conversation_id: str | None = None


# ── Diff between two points in time ─────────────────────────────


class StatusChangeView(BaseModel):
    from_status: str
    to_status: str


class StepChangeView(BaseModel):
    step_id: str
    from_status: str | None = None
    to_status: str | None = None
    error: str | None = None


class ErrorChangeView(BaseModel):
    from_error: str | None = None
    to_error: str | None = None


class ToolResultChangeView(BaseModel):
    tool_call_id: str
    tool_name: str
    status: str
    event_seq: int
    error: str | None = None


class StateDiff(BaseModel):
    """Structured difference between state@from_seq and state@to_seq.

    ``status_change`` and ``steps_changed`` are the prominently-highlighted
    surfaces ("how did run status change" / "which steps changed").
    """

    run_id: str
    from_seq: int
    to_seq: int

    status_change: StatusChangeView | None = None
    steps_changed: list[StepChangeView] = Field(default_factory=list)
    tool_results_added: list[ToolResultChangeView] = Field(default_factory=list)
    guardrails_triggered: list[GuardrailBlockView] = Field(default_factory=list)
    error_change: ErrorChangeView | None = None
    events_in_range: list[ReplayTimelineEvent] = Field(default_factory=list)
