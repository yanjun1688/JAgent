export interface RetryableInfo {
  eligible: boolean
  ineligible_reason: string | null
  suggested_backoff_ms: number | null
  requires_input_modification: boolean
}

export interface DashboardOverview {
  total_runs: number
  running_runs: number
  paused_runs: number
  completed_runs: number
  failed_runs: number
  total_events: number
  total_tool_calls: number
  total_tool_failures: number
  total_guardrail_triggers: number
  total_tokens_consumed: number
  avg_tool_success_rate: number
}

export interface DashboardResponse {
  overview: DashboardOverview
}

export interface ToolStatItem {
  tool_name: string
  call_count: number
  success_count: number
  failure_count: number
  timeout_count: number
  guardrail_blocked_count: number
  avg_duration_ms: number
}

export interface ToolStatsResponse {
  tools: ToolStatItem[]
}

export interface GuardrailStatItem {
  guardrail_id: string
  trigger_count: number
  tools_affected: string[]
  recent_reason: string | null
}

export interface GuardrailStatsResponse {
  guardrails: GuardrailStatItem[]
}

export interface RunAnalysisSummary {
  run_id: string
  intent: string
  status: string
  event_count: number
  total_tokens: number
  total_duration_ms: number
  created_at: number | null
  completed_at: number | null
  tool_trace_count: number
  guardrail_event_count: number
  feedback_count: number
}

export interface ParsedEventDetail {
  run_id: string
  seq: number
  event_type: string
  created_at: number
  payload: Record<string, unknown>
  tool_call_id: string | null
  tool_name: string | null
  input: Record<string, unknown> | null
  idempotency_key: string | null
  confirmation_id: string | null
  error: string | null
  duration_ms: number | null
  retryable: RetryableInfo | null
}

export interface TimelineResponse {
  timeline: ParsedEventDetail[]
  next_cursor: number
  has_more: boolean
}

export interface ToolTraceItem {
  tool_call_id: string
  tool_name: string
  called_seq: number | null
  input: Record<string, unknown> | null
  idempotency_key: string | null
  status: string
  completed_seq: number | null
  output: unknown | null
  error: string | null
  duration_ms: number
  guardrail_id: string | null
  guardrail_reason: string | null
  retryable: RetryableInfo
}

export interface ToolTracesResponse {
  tool_traces: ToolTraceItem[]
}
