const BASE = '/api/v1/query'

interface ListMeta {
  page: number
  page_size: number
  total: number
  has_more: boolean
}

interface ApiResponse<T, M = ListMeta | null> {
  type: string
  data: T
  meta: M
}

interface IncludedResponse<T, I> extends ApiResponse<T, null> {
  _included: I
}

// ── type=runs ──

export interface RunsItem {
  run_id: string
  intent: string
  status: string
  event_count: number
  tool_call_count: number
  tool_success_count: number
  tool_failure_count: number
  tool_unsuccessful_count: number
  created_at: number
  updated_at: number
}

export interface RunsResponse {
  type: 'runs'
  data: RunsItem[]
  meta: ListMeta
}

// ── type=run ──

export interface ToolStatsEntry {
  call_count: number
  completed: number
  unsuccessful: number
  failed: number
  timeout: number
  guardrail_blocked: number
}

export interface ToolResult {
  tool_call_id: string
  tool_name: string
  status: string
  output: unknown
  error: string | null
  duration_ms: number
}

export interface PendingConfirmation {
  confirmation_id: string
  tool_name: string
  tool_call_id: string
  input: Record<string, unknown>
  risk_level: string
}

export interface PlanStep {
  step_id: string
  tool_name: string
  status: string
}

export interface Plan {
  plan_id: string
  intent: string
  steps_summary: string
  layer_count: number
  steps: PlanStep[]
  revision_reason: string | null
  remaining_steps_summary: string
}

export interface RunDetail {
  run_id: string
  status: string
  intent: string
  seq: number
  event_count: number
  event_type_counts: Record<string, number>
  total_tokens: number
  created_at: number | null
  completed_at: number | null
  last_error: string | null
  summary: string | null
  pause_reason: string | null
  tool_stats: Record<string, ToolStatsEntry>
  tool_results: ToolResult[]
  thought_count: number
  pending_confirmations: PendingConfirmation[]
  latest_plan: Plan | null
  plan_history: Plan[]
  feedback_count: number
  checkpoint_seq: number
}

// ── type=events ──

export interface EventItem {
  run_id: string
  seq: number
  event_type: string
  payload: Record<string, unknown>
  idempotency_key: string | null
  created_at: number
}

export interface EventsResponse {
  type: 'events'
  data: EventItem[]
  meta: ListMeta
}

// ── type=dashboard ──

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

// ── type=tool-stats ──

export interface ToolStat {
  tool_name: string
  call_count: number
  success_count: number
  failure_count: number
  timeout_count: number
  guardrail_blocked_count: number
  avg_duration_ms: number
}

export interface ToolStatsData {
  tools: ToolStat[]
}

// ── type=guardrail-stats ──

export interface GuardrailStat {
  guardrail_id: string
  trigger_count: number
  tools_affected: string[]
  recent_reason: string | null
}

export interface GuardrailStatsData {
  guardrails: GuardrailStat[]
}

// ── type=run-analysis ──

export interface RunAnalysis {
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

// ── type=timeline ──

export interface TimelineEvent {
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
}

export interface TimelineResponse {
  type: 'timeline'
  data: TimelineEvent[]
  meta: ListMeta
}

// ── type=tool-traces ──

export interface RetryableInfo {
  eligible: boolean
  ineligible_reason: string | null
}

export interface ToolTrace {
  tool_call_id: string
  tool_name: string
  status: string
  input: Record<string, unknown> | null
  output: unknown | null
  duration_ms: number
  guardrail_id: string | null
  retryable: RetryableInfo
}

export interface ToolTracesData {
  tool_traces: ToolTrace[]
}

// ── type=tool-defs ──

export interface ToolDef {
  tool_name: string
  definition: {
    name: string
    description: string
    input_schema: Record<string, unknown>
    output_schema: Record<string, unknown>
    side_effects: string[]
    timeout_ms: number
    retry_policy: Record<string, unknown>
    guardrails: string[]
    requires_confirmation: boolean
  }
}

// ── type=schedulers ──

export interface SchedulerEntry {
  run_id: string
  status: string
  intent: string
  seq: number
  event_count: number
  last_error: string | null
  pause_reason: string | null
  is_active: boolean
  is_paused: boolean
  config: {
    max_iterations: number
    max_consecutive_failures: number
    pause_timeout_ms: number
    confirm_timeout_ms: number
    max_confirm_retries: number
  }
  tool_stats: Record<string, ToolStatsEntry>
  latest_plan: Plan | null
}

// ── type=mcp ──

export interface McpServer {
  name: string
  command: string
  url: string
  enabled: boolean
  auto_register_tools: boolean
  timeout_ms: number
  connected: boolean
}

export interface McpData {
  servers: McpServer[]
  connected_count: number
}

// ── type=plans ──

export interface PlanBoundarySeq {
  seq: number
  event_type: string
}

export interface PlansData {
  run_id: string
  plan_history: Plan[]
  latest_plan: Plan | null
  plan_boundary_seqs: PlanBoundarySeq[]
}

// ── type=system ──

export interface SystemData {
  llm_client: {
    type: string
    model: string
    base_url: string
    total_calls: number
  }
  tool_registry: {
    tool_count: number
    tool_names: string[]
  }
  scheduler_config: Record<string, unknown>
  tool_defs_count: number
}

// ── type=ws-clients ──

export interface WsClientsGlobal {
  total_connections: number
  by_run: Record<string, number>
}

export interface WsClientsRun {
  run_id: string
  connected_clients: number
}

// ── included response for queryRun ──

export interface RunIncluded {
  events: EventsResponse
  timeline: TimelineResponse
  'tool-traces': { type: 'tool-traces'; data: ToolTrace[]; meta: null }
  'run-analysis': { type: 'run-analysis'; data: RunAnalysis; meta: null }
  plans: { type: 'plans'; data: PlansData; meta: null }
}

export type RunDetailResponse = IncludedResponse<RunDetail, RunIncluded>

// ── fetch helpers ──

class OpsApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'OpsApiError'
  }
}

async function checkResponse(res: Response): Promise<void> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      const raw = body.detail ?? body.error ?? body.message ?? body
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
    } catch { /* use statusText */ }
    throw new OpsApiError(res.status, detail)
  }
}

async function fetchQuery<T>(params: URLSearchParams): Promise<T> {
  const res = await fetch(`${BASE}?${params}`)
  await checkResponse(res)
  return res.json()
}

// ── query functions ──

export async function queryRuns(page = 1, page_size = 20): Promise<RunsResponse> {
  const params = new URLSearchParams()
  params.set('type', 'runs')
  params.set('page', String(page))
  params.set('page_size', String(page_size))
  return fetchQuery<RunsResponse>(params)
}

export async function queryRun(
  runId: string,
  include?: string,
): Promise<RunDetailResponse> {
  const params = new URLSearchParams()
  params.set('type', 'run')
  params.set('run_id', runId)
  if (include) params.set('include', include)
  return fetchQuery<RunDetailResponse>(params)
}

export async function queryEvents(
  runId: string,
  page = 1,
  page_size = 20,
): Promise<EventsResponse> {
  const params = new URLSearchParams()
  params.set('type', 'events')
  params.set('run_id', runId)
  params.set('page', String(page))
  params.set('page_size', String(page_size))
  return fetchQuery<EventsResponse>(params)
}

export async function queryDashboard(
  since?: number,
  until?: number,
): Promise<ApiResponse<DashboardOverview, null>> {
  const params = new URLSearchParams()
  params.set('type', 'dashboard')
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  return fetchQuery<ApiResponse<DashboardOverview, null>>(params)
}

export async function queryToolStats(
  since?: number,
  until?: number,
): Promise<ApiResponse<ToolStatsData, null>> {
  const params = new URLSearchParams()
  params.set('type', 'tool-stats')
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  return fetchQuery<ApiResponse<ToolStatsData, null>>(params)
}

export async function queryGuardrailStats(
  since?: number,
  until?: number,
): Promise<ApiResponse<GuardrailStatsData, null>> {
  const params = new URLSearchParams()
  params.set('type', 'guardrail-stats')
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  return fetchQuery<ApiResponse<GuardrailStatsData, null>>(params)
}

export async function queryRunAnalysis(runId: string): Promise<ApiResponse<RunAnalysis, null>> {
  const params = new URLSearchParams()
  params.set('type', 'run-analysis')
  params.set('run_id', runId)
  return fetchQuery<ApiResponse<RunAnalysis, null>>(params)
}

export async function queryTimeline(
  runId: string,
  page = 1,
  page_size = 50,
): Promise<TimelineResponse> {
  const params = new URLSearchParams()
  params.set('type', 'timeline')
  params.set('run_id', runId)
  params.set('page', String(page))
  params.set('page_size', String(page_size))
  return fetchQuery<TimelineResponse>(params)
}

export async function queryToolTraces(runId: string): Promise<ApiResponse<ToolTracesData, null>> {
  const params = new URLSearchParams()
  params.set('type', 'tool-traces')
  params.set('run_id', runId)
  return fetchQuery<ApiResponse<ToolTracesData, null>>(params)
}

export async function queryToolDefs(): Promise<ApiResponse<ToolDef[], null>> {
  const params = new URLSearchParams()
  params.set('type', 'tool-defs')
  return fetchQuery<ApiResponse<ToolDef[], null>>(params)
}

export async function querySchedulers(): Promise<ApiResponse<SchedulerEntry[], null>> {
  const params = new URLSearchParams()
  params.set('type', 'schedulers')
  return fetchQuery<ApiResponse<SchedulerEntry[], null>>(params)
}

export async function queryMcp(): Promise<ApiResponse<McpData, null>> {
  const params = new URLSearchParams()
  params.set('type', 'mcp')
  return fetchQuery<ApiResponse<McpData, null>>(params)
}

export async function queryPlans(runId: string): Promise<ApiResponse<PlansData, null>> {
  const params = new URLSearchParams()
  params.set('type', 'plans')
  params.set('run_id', runId)
  return fetchQuery<ApiResponse<PlansData, null>>(params)
}

export async function querySystem(): Promise<ApiResponse<SystemData, null>> {
  const params = new URLSearchParams()
  params.set('type', 'system')
  return fetchQuery<ApiResponse<SystemData, null>>(params)
}

export async function queryWsClients(runId?: string): Promise<ApiResponse<WsClientsGlobal | WsClientsRun, null>> {
  const params = new URLSearchParams()
  params.set('type', 'ws-clients')
  if (runId) params.set('run_id', runId)
  return fetchQuery<ApiResponse<WsClientsGlobal | WsClientsRun, null>>(params)
}

export { OpsApiError }
