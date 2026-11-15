/**
 * The v8 runtime HTTP contract.
 *
 * Production: the Python process serves `ui/dist` and these routes on one
 * origin. Development: Vite proxies the same paths to 127.0.0.1:8021, so the
 * browser never needs CORS.
 *
 * The panel only projects what the runtime returns. It does not invent a run,
 * a step, or a verdict. Logs (`/api/logs`) are operational telemetry — they
 * are NOT events; the event stream and the log stream never merge.
 */

export type ChatStatus = 'needs_input' | 'answered' | 'waiting_human'

export interface Choice {
  label: string
  value: string
}

/** A step as the API reports it. `status` is the runtime's own word. */
export interface StepView {
  id: string
  status: string
}

export interface ResultView {
  verdict: string
  summary: string
  steps: StepView[]
}

/** What the human is about to approve. `what` must be shown verbatim. */
export interface ApprovalView {
  step_id: string
  what: string
}

export interface PendingQuestion {
  text: string
  options: string[]
  slot: string
}

export interface ChatReply {
  status: ChatStatus
  message: string
  session_id?: string
  slot?: string
  choices?: Choice[]
  run_id?: string
  result?: ResultView
  /** 输出分级（宪法 v2 I3）：这段文本的可信档位，由后端代码给。 */
  delivery?: DeliveryView
  approval?: ApprovalView
}

/** 三档可信度：verified（代码事实）/ grounded（复述事实并过检查）/ unverified（无判据）。 */
export type Regime = 'verified' | 'grounded' | 'unverified'

export interface DeliveryView {
  regime: Regime
  checks: string[]
  grounds: number[]
  fallback: { bool?: boolean; reason?: string }
}

export interface RunView {
  status: string
  summary: string
  steps: StepView[]
  needs_human: boolean
  question: PendingQuestion | null
  approval: ApprovalView | null
}

export interface RunRow {
  run_id: string
  status: string
  summary: string
  updated_seq: number
}

/** One fact from the append-only log. No token ever reaches this type. */
export interface ServerEvent {
  seq: number
  type: string
  at: number
  payload: Record<string, unknown>
}

export interface EventsPage {
  events: ServerEvent[]
  next_seq: number
}

export interface RunsPage {
  runs: RunRow[]
}

export type Decision = 'approve' | 'reject'

// --------------------------------------------------------------------------- //
// 多对话（D-034）
// --------------------------------------------------------------------------- //

export interface SessionSummary {
  session_id: string
  turns: number
  runs: number
  first_at: number
  last_at: number
  title: string
}

export interface SessionsPage {
  sessions: SessionSummary[]
}

// --------------------------------------------------------------------------- //
// 指标（D-031 S2，纯派生）
// --------------------------------------------------------------------------- //

export interface PurposeTokens {
  calls: number
  tokens_in: number
  tokens_out: number
}

export interface SessionMetrics {
  turns: number
  runs: number
  verdicts: { completed: number; failed: number; cancelled: number }
  completion_rate: number | null
  human_interventions: number
  human_intervention_rate: number | null
  model_calls: number
  tokens_in: number
  tokens_out: number
  model_tokens_by_purpose: Record<string, PurposeTokens>
  repairs: number
  invalidations: number
  memory_proposed: number
  memory_confirmed: number
}

// --------------------------------------------------------------------------- //
// 验证对账（D-032：声称 vs 证据）
// --------------------------------------------------------------------------- //

export interface VerifyCheck {
  [key: string]: unknown
}

export interface VerifyStep {
  step_id: string
  tool: string
  args: Record<string, unknown>
  status: string
  attempts: number
  verified: string
  checks: VerifyCheck[]
  observations: Record<string, unknown>
  error_code: string | null
  failure: { class?: unknown; error_code?: unknown; reason?: unknown } | null
  invalidated: boolean
}

export interface VerifyView {
  run_id: string
  claimed: { verdict: string; fail_reason: string }
  steps: VerifyStep[]
  model_calls: Array<Record<string, unknown>>
  invalidations: Array<{ step_id?: unknown; reason?: unknown }>
  consistent: boolean
  problems: string[]
}

// --------------------------------------------------------------------------- //
// 进程日志（D-036：运维观测，不是事件）
// --------------------------------------------------------------------------- //

export interface LogEntry {
  id: number
  at: number
  level: string
  logger: string
  message: string
}

export interface LogsPage {
  logs: LogEntry[]
  next: number
}

/** A friendly API refusal. The runtime always sends `{status, message}`. */
export class ApiError extends Error {
  constructor(
    readonly detail: string,
    readonly status: number,
  ) {
    super(detail)
    this.name = 'ApiError'
  }
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError('连不上本地服务，请确认它还在运行。', 0)
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = (await response.json()) as { message?: unknown; detail?: unknown }
      const message = body.message ?? body.detail
      if (message) detail = String(message)
    } catch {
      // A non-JSON refusal is still a refusal; keep the status line.
    }
    throw new ApiError(detail, response.status)
  }

  return (await response.json()) as T
}

const post = <T,>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  health: () => request<{ ok: boolean }>('/healthz'),

  /** One conversational turn. `answers` fills the question asked last time. */
  chat: (text: string, answers?: Record<string, string>, sessionId?: string) =>
    post<ChatReply>('/chat', {
      text,
      answers: answers ?? null,
      session_id: sessionId ?? '',
    }),

  /** Approve or reject the command the runtime is parked on. */
  decide: (runId: string, decision: Decision, comment = '') =>
    post<ChatReply>(`/runs/${encodeURIComponent(runId)}/decision`, { decision, comment }),

  /** Cancel a live run (D-028). Terminal runs answer 409. */
  cancel: (runId: string, reason = '') =>
    post<ChatReply>(`/runs/${encodeURIComponent(runId)}/cancel`, { reason }),

  getRun: (runId: string) => request<RunView>(`/runs/${encodeURIComponent(runId)}`),

  /** Newest first, already sorted by the runtime. */
  listRuns: (limit = 50) => request<RunsPage>(`/api/runs?limit=${limit}`),

  /** Incremental poll: pass the previous `next_seq` back in. */
  pollEvents: (runId: string, afterSeq = 0) =>
    request<EventsPage>(
      `/api/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`,
    ),

  /** Sessions (D-034). New conversations register their stack on creation. */
  listSessions: () => request<SessionsPage>('/api/sessions'),
  createSession: () => post<{ session_id: string }>('/api/sessions', {}),

  /** Session metrics projection (D-031 S2); empty `sessionId` = all. */
  sessionMetrics: (sessionId = '') =>
    request<SessionMetrics>(
      `/api/metrics/session${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`,
    ),

  /** Claimed vs evidence, side by side (D-032). */
  verifyRun: (runId: string) =>
    request<VerifyView>(`/api/runs/${encodeURIComponent(runId)}/verify`),

  /** Process log tail (D-036). `after=-1` returns the whole ring window. */
  pollLogs: (after: number) => request<LogsPage>(`/api/logs?after=${after}`),
}
