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
  total: number
}

export interface EventResponse {
  run_id: string
  seq: number
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

export interface RunDetailResponse {
  run_id: string
  status: string
  intent: string
  seq: number
  event_count: number
  last_error: string | undefined
  summary: string | undefined
  pause_reason: string | undefined
  pending_confirmations: object[] | undefined
}

export interface RunListResponse {
  runs: RunSummary[]
  total: number
}

export interface RunSummary {
  run_id: string
  intent: string | undefined
  status: string | undefined
  event_count: number | undefined
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
