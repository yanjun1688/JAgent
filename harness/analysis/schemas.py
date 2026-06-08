from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── RetryableInfo：操作锚点核心 ──────────────────────────


class RetryableInfo(BaseModel):
    eligible: bool
    ineligible_reason: str | None = None
    suggested_backoff_ms: int | None = None
    requires_input_modification: bool = False


# ── ParsedEventDetail：单事件完整展开 ────────────────────


class ParsedEventDetail(BaseModel):
    run_id: str
    seq: int
    event_type: str
    created_at: float
    payload: dict[str, Any]

    tool_call_id: str | None = None
    tool_name: str | None = None
    input: dict[str, Any] | None = None
    idempotency_key: str | None = None
    confirmation_id: str | None = None
    error: str | None = None
    duration_ms: int | None = None

    retryable: RetryableInfo | None = None


# ── ToolTraceItem：工具调用全生命周期 ────────────────────


class ToolTraceItem(BaseModel):
    tool_call_id: str
    tool_name: str

    called_seq: int | None = None
    input: dict[str, Any] | None = None
    idempotency_key: str | None = None

    status: str = "unknown"
    completed_seq: int | None = None
    output: Any = None
    error: str | None = None
    duration_ms: int = 0

    guardrail_id: str | None = None
    guardrail_reason: str | None = None

    retryable: RetryableInfo = Field(default_factory=lambda: RetryableInfo(eligible=False))


# ── Dashboard ────────────────────────────────────────────


class DashboardOverview(BaseModel):
    total_runs: int = 0
    running_runs: int = 0
    paused_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    total_events: int = 0
    total_tool_calls: int = 0
    total_tool_failures: int = 0
    total_guardrail_triggers: int = 0
    total_tokens_consumed: int = 0
    avg_tool_success_rate: float = 0.0


class DashboardResponse(BaseModel):
    overview: DashboardOverview


# ── Tool Stats ───────────────────────────────────────────


class ToolStatItem(BaseModel):
    tool_name: str
    call_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    guardrail_blocked_count: int = 0
    avg_duration_ms: float = 0.0


class ToolStatsResponse(BaseModel):
    tools: list[ToolStatItem]


# ── Guardrail Stats ──────────────────────────────────────


class GuardrailStatItem(BaseModel):
    guardrail_id: str
    trigger_count: int = 0
    tools_affected: list[str] = []
    recent_reason: str | None = None


class GuardrailStatsResponse(BaseModel):
    guardrails: list[GuardrailStatItem]


# ── Run Analysis ─────────────────────────────────────────


class RunAnalysisSummary(BaseModel):
    run_id: str
    intent: str = ""
    status: str = "running"
    event_count: int = 0
    total_tokens: int = 0
    total_duration_ms: int = 0
    created_at: float | None = None
    completed_at: float | None = None
    tool_trace_count: int = 0
    guardrail_event_count: int = 0
    feedback_count: int = 0


class TimelineResponse(BaseModel):
    timeline: list[ParsedEventDetail]
    next_cursor: int = 0
    has_more: bool = False


class ToolTracesResponse(BaseModel):
    tool_traces: list[ToolTraceItem]
