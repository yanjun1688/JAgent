import type {
  DashboardResponse,
  ToolStatsResponse,
  GuardrailStatsResponse,
  RunAnalysisSummary,
  TimelineResponse,
  ToolTracesResponse,
} from './analysis-types'

const BASE = '/api/v1/analysis'

class AnalysisApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'AnalysisApiError'
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
    throw new AnalysisApiError(res.status, detail)
  }
}

export async function getDashboard(since?: number, until?: number): Promise<DashboardResponse> {
  const params = new URLSearchParams()
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  const qs = params.toString()
  const res = await fetch(`${BASE}/dashboard${qs ? '?' + qs : ''}`)
  await checkResponse(res)
  return res.json()
}

export async function getToolStats(since?: number, until?: number): Promise<ToolStatsResponse> {
  const params = new URLSearchParams()
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  const qs = params.toString()
  const res = await fetch(`${BASE}/tools${qs ? '?' + qs : ''}`)
  await checkResponse(res)
  return res.json()
}

export async function getGuardrailStats(since?: number, until?: number): Promise<GuardrailStatsResponse> {
  const params = new URLSearchParams()
  if (since !== undefined) params.set('since', String(since))
  if (until !== undefined) params.set('until', String(until))
  const qs = params.toString()
  const res = await fetch(`${BASE}/guardrails${qs ? '?' + qs : ''}`)
  await checkResponse(res)
  return res.json()
}

export async function getRunAnalysis(runId: string): Promise<RunAnalysisSummary> {
  const res = await fetch(`${BASE}/runs/${runId}`)
  await checkResponse(res)
  return res.json()
}

export async function getRunTimeline(
  runId: string,
  limit = 50,
  cursor = 0,
): Promise<TimelineResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  if (cursor > 0) params.set('cursor', String(cursor))
  const res = await fetch(`${BASE}/runs/${runId}/timeline?${params}`)
  await checkResponse(res)
  return res.json()
}

export async function getRunToolTraces(runId: string): Promise<ToolTracesResponse> {
  const res = await fetch(`${BASE}/runs/${runId}/tool-traces`)
  await checkResponse(res)
  return res.json()
}

export { AnalysisApiError }
