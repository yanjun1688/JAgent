/**
 * The v8 runtime HTTP contract.
 *
 * Production: the Python process serves `ui/dist` and these routes on one
 * origin. Development: Vite proxies the same paths to 127.0.0.1:8021, so the
 * browser never needs CORS.
 *
 * The panel only projects what the runtime returns. It does not invent a run,
 * a step, or a verdict.
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
  slot?: string
  choices?: Choice[]
  run_id?: string
  result?: ResultView
  approval?: ApprovalView
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
  chat: (text: string, answers?: Record<string, string>) =>
    post<ChatReply>('/chat', { text, answers: answers ?? null }),

  /** Approve or reject the command the runtime is parked on. */
  decide: (runId: string, decision: Decision, comment = '') =>
    post<ChatReply>(`/runs/${encodeURIComponent(runId)}/decision`, { decision, comment }),

  getRun: (runId: string) => request<RunView>(`/runs/${encodeURIComponent(runId)}`),

  /** Newest first, already sorted by the runtime. */
  listRuns: (limit = 50) => request<RunsPage>(`/api/runs?limit=${limit}`),

  /** Incremental poll: pass the previous `next_seq` back in. */
  pollEvents: (runId: string, afterSeq = 0) =>
    request<EventsPage>(
      `/api/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`,
    ),
}
