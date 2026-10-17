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

/**
 * Consolidated event taxonomy (41 -> 13): a single aggregate `event_type` owns
 * several lifecycle transitions, discriminated by `payload.phase`. These helpers
 * are the single frontend seam for reading the phase — do not compare
 * `event_type` against pre-consolidation names (e.g. 'RunCompleted').
 */
export function eventPhase(event: Pick<WsEvent, 'payload'>): string | null {
  const phase = event.payload?.['phase']
  return typeof phase === 'string' ? phase : null
}

export function isEvent(event: WsEvent, eventType: string, phase: string): boolean {
  return event.event_type === eventType && eventPhase(event) === phase
}

const TERMINAL_RUN_PHASES = new Set(['completed', 'failed', 'orphaned'])

export function isTerminalRunEvent(event: WsEvent): boolean {
  return event.event_type === 'Run' && TERMINAL_RUN_PHASES.has(eventPhase(event) ?? '')
}
