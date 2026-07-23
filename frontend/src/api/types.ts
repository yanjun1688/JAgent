export type RunStatus = 'running' | 'paused' | 'completed' | 'failed' | 'pending' | 'idle'

export interface WsEvent {
  run_id: string
  seq: number
  event_type: string
  payload: Record<string, unknown>
  idempotency_key: string | null
  created_at: number
  tool_call_id: string | null
  tool_name: string | null
  input: Record<string, unknown> | null
  error: string | null
  duration_ms: number | null
  confirmation_id: string | null
}

export type WsConnectionStatus = 'connected' | 'disconnected' | 'connecting' | 'error'