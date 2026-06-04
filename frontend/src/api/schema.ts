// Auto-generated from OpenAPI schema. Run `npm run generate-api` to refresh.
// eslint-disable-next-line @typescript-eslint/no-unused-vars

export interface ConfirmRequest {
  confirmation_id: string
  confirmed: boolean
  operator_id: string | undefined
}

export interface CreateRunRequest {
  intent: string
}

export interface EventListResponse {
  events: EventResponse[]
  total: integer
}

export interface EventResponse {
  run_id: string
  seq: integer
  event_type: string
  payload: Record<string, unknown>
  idempotency_key: unknown | undefined
  created_at: number
}

export interface HTTPValidationError {
  detail: ValidationError[] | undefined
}

export interface PauseRequest {
  reason: string | undefined
}

export interface PendingConfirmationItem {
  confirmation_id: string
  tool_name: string
  tool_call_id: string
  input: Record<string, unknown>
  risk_level: string
}

export interface RunDetailResponse {
  run_id: string
  status: string
  intent: string
  seq: integer
  event_count: integer
  last_error: unknown | undefined
  summary: unknown | undefined
  pause_reason: unknown | undefined
  pending_confirmations: PendingConfirmationItem[] | undefined
}

export interface RunListResponse {
  runs: RunSummary[]
  total: integer
}

export interface RunSummary {
  run_id: string
  intent: string | undefined
  status: string | undefined
  event_count: integer | undefined
  created_at: number | undefined
  updated_at: number | undefined
}

export interface ValidationError {
  loc: any[]
  msg: string
  type: string
  input: unknown | undefined
  ctx: Record<string, unknown> | undefined
}
