// Event Replay Inspector (time-travel debugger) API client.
// Strictly read-only: only GET requests. Types come from the generated
// OpenAPI schema (src/api/schema.ts) — do not hand-edit shapes here.

import type {
  ReplayRunMeta,
  ReplayTimelineResponse,
  RunStateView,
  StateDiff,
} from './schema'

const BASE = '/api/v1/replay'

export class ReplayApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ReplayApiError'
  }
}

async function checkResponse(res: Response): Promise<void> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      const raw = body.detail ?? body.error ?? body.message ?? body
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
    } catch {
      /* use statusText */
    }
    throw new ReplayApiError(res.status, detail)
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  await checkResponse(res)
  return res.json() as Promise<T>
}

export async function getReplayRunMeta(runId: string): Promise<ReplayRunMeta> {
  return getJson<ReplayRunMeta>(`${BASE}/runs/${encodeURIComponent(runId)}/meta`)
}

export async function getReplayTimeline(
  runId: string,
  cursor = 0,
  limit = 1000,
): Promise<ReplayTimelineResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  if (cursor > 0) params.set('cursor', String(cursor))
  return getJson<ReplayTimelineResponse>(
    `${BASE}/runs/${encodeURIComponent(runId)}/timeline?${params}`,
  )
}

export async function getReplayState(runId: string, atSeq?: number): Promise<RunStateView> {
  const params = new URLSearchParams()
  if (atSeq !== undefined) params.set('at_seq', String(atSeq))
  const qs = params.toString()
  return getJson<RunStateView>(
    `${BASE}/runs/${encodeURIComponent(runId)}/state${qs ? '?' + qs : ''}`,
  )
}

export async function getReplayDiff(
  runId: string,
  fromSeq: number,
  toSeq: number,
): Promise<StateDiff> {
  const params = new URLSearchParams()
  params.set('from_seq', String(fromSeq))
  params.set('to_seq', String(toSeq))
  return getJson<StateDiff>(
    `${BASE}/runs/${encodeURIComponent(runId)}/diff?${params}`,
  )
}
