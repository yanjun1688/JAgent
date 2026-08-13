// Auto-generated from OpenAPI schema. Run `npm run generate-api` to refresh.
// eslint-disable-next-line @typescript-eslint/no-unused-vars

export interface ConfirmRequest {
  confirmation_id: string
  confirmed: boolean
  operator_id: string
}

export interface ConfirmationResponse {
  success: boolean
  message?: string
}

export interface Conversation {
  conversation_id: string
  user_id: string
  title: string
  status: ConversationStatus
  created_at: number
  updated_at: number
  message_count: number
}

export interface ConversationDetail {
  conversation: Conversation
  messages: ConversationMessageItem[]
}

export interface ConversationListResponse {
  conversations: Conversation[]
  total: number
}

export interface ConversationMessageItem {
  seq: number
  run_id: string
  role: string
  content: string
  created_at: number
  status: string
}

export type ConversationStatus = "active" | "archived"

export interface CreateConversationRequest {
  title?: string
}

export interface CreateConversationResponse {
  conversation_id: string
  title: string
  created_at: number
}

export interface CreateRunRequest {
  intent: string
  conversation_id?: string
  client_request_id?: string
  workspace_id?: string
  required_operations?: RequiredOperationInput[]
}

export interface CreateRunResponse {
  run_id: string
}

export interface CreateWorkspaceRequest {
  name: string
  description: string
  scope: WorkspaceScope
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

export interface DeleteConversationResponse {
  success: boolean
}

export interface DeliveryOperationInput {
  tool: string
  input?: Record<string, unknown>
}

export interface EventListResponse {
  events: EventResponse[]
  total: number
}

export interface EventResponse {
  run_id: string
  seq: number
  event_type: string
  payload: Record<string, unknown>
  idempotency_key?: string
  created_at: number
  tenant_id: string
  workspace_id?: string
}

export interface ExecutionTarget {
  type: ExecutionTargetType
  filesystem_root?: string
  docker_image?: string
  host_mount_src?: string
  mount_root?: string
  host?: string
  port: number
  username?: string
  private_key_path?: string
  remote_root?: string
}

export type ExecutionTargetType = "directory" | "sandbox" | "remote"

export interface FeedbackResponse {
  status: string
  feedback_id: string
}

export interface GuardrailStatItem {
  guardrail_id: string
  trigger_count: number
  tools_affected: string[]
  recent_reason?: string
}

export interface GuardrailStatsResponse {
  guardrails: GuardrailStatItem[]
}

export interface HTTPValidationError {
  detail?: ValidationError[]
}

export interface OperatorFeedbackRequest {
  text: string
  priority: string
  suggestion?: string
  expires_in_seqs?: number
}

export interface ParsedEventDetail {
  run_id: string
  seq: number
  event_type: string
  created_at: number
  payload: Record<string, unknown>
  tool_call_id?: string
  tool_name?: string
  input?: Record<string, unknown>
  idempotency_key?: string
  confirmation_id?: string
  error?: string
  duration_ms?: number
  retryable?: RetryableInfo
}

export interface PauseRequest {
  reason: string
}

export interface PendingConfirmationItem {
  confirmation_id: string
  tool_name: string
  tool_call_id: string
  input: Record<string, unknown>
  risk_level: string
}

export interface RequiredOperationInput {
  tool: string
  input?: Record<string, unknown>
}

export interface RetryableInfo {
  eligible: boolean
  ineligible_reason?: string
  suggested_backoff_ms?: number
  requires_input_modification: boolean
}

export interface RunAnalysisSummary {
  run_id: string
  intent: string
  status: string
  event_count: number
  total_tokens: number
  total_duration_ms: number
  created_at?: number
  completed_at?: number
  tool_trace_count: number
  guardrail_event_count: number
  feedback_count: number
}

export interface RunControlResponse {
  success: boolean
}

export interface RunDetailResponse {
  run_id: string
  status: string
  intent: string
  seq: number
  event_count: number
  last_error?: string
  summary?: unknown
  pause_reason?: string
  pending_confirmations: PendingConfirmationItem[]
  conversation_id?: string
  orphaned: boolean
  workspace_id?: string
}

export interface RunListResponse {
  runs: RunSummary[]
  total: number
}

export interface RunSummary {
  run_id: string
  intent: string
  status: string
  event_count: number
  created_at: number
  updated_at: number
  orphaned: boolean
  workspace_id?: string
}

export interface SendMessageRequest {
  message: string
  client_request_id?: string
  workspace_id?: string
  required_operations?: DeliveryOperationInput[]
}

export interface SendMessageResponse {
  run_id: string
  conversation_id: string
  seq: number
  claimed: boolean
}

export interface SuccessResponse {
  success: boolean
}

export interface TimelineResponse {
  timeline: ParsedEventDetail[]
  next_cursor: number
  has_more: boolean
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

export interface ToolTraceItem {
  tool_call_id: string
  tool_name: string
  called_seq?: number
  input?: Record<string, unknown>
  idempotency_key?: string
  status: string
  completed_seq?: number
  output?: unknown
  error?: string
  duration_ms: number
  guardrail_id?: string
  guardrail_reason?: string
  retryable?: RetryableInfo
}

export interface ToolTracesResponse {
  tool_traces: ToolTraceItem[]
}

export interface UpdateConversationRequest {
  title?: string
  status?: string
}

export interface UpdateConversationResponse {
  success: boolean
}

export interface ValidationError {
  loc: any[]
  msg: string
  type: string
  input?: unknown
  ctx?: Record<string, unknown>
}

export interface WorkspaceListResponse {
  workspaces: WorkspaceResponse[]
  total: number
}

export interface WorkspaceResponse {
  workspace_id: string
  tenant_id: string
  name: string
  description: string
  scope: WorkspaceScope
  status: string
  run_count: number
  created_at: number
  updated_at: number
}

export interface WorkspaceScope {
  target: ExecutionTarget
  allowed_tools?: string[]
}

export interface WorkspaceUpdate {
  name?: string
  description?: string
  scope?: WorkspaceScope
}
