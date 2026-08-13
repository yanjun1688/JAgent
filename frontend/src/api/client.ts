const BASE = '/api/v1'

// Types are generated from OpenAPI schema via `npm run generate-api`.
// See schema.ts for the full set of generated interfaces.
import type { RunSummary, RunDetailResponse, EventResponse, WorkspaceListResponse, WorkspaceResponse, WorkspaceScope } from './schema'

export type { RunSummary }
export type RunDetail = RunDetailResponse
export type HarnessEvent = EventResponse

export async function listRuns(workspaceId?: string): Promise<{ runs: RunSummary[]; total: number }> {
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
  const res = await fetch(`${BASE}/runs${query}`)
  return res.json()
}

export async function getRun(runId: string): Promise<RunDetail> {
  const res = await fetch(`${BASE}/runs/${runId}`)
  if (!res.ok) throw new Error('Run not found')
  return res.json()
}

export async function getRunEvents(runId: string): Promise<{ events: HarnessEvent[]; total: number }> {
  const res = await fetch(`${BASE}/runs/${runId}/events`)
  return res.json()
}

export async function createRun(
  intent: string,
  workspaceId?: string,
  requiredOperations?: { tool: string; input: Record<string, unknown> }[],
): Promise<{ run_id: string }> {
  const res = await fetch(`${BASE}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ intent, workspace_id: workspaceId, required_operations: requiredOperations }),
  })
  return res.json()
}

export async function listWorkspaces(): Promise<WorkspaceListResponse> {
  const res = await fetch(`${BASE}/workspaces`)
  if (!res.ok) throw new Error(`Failed to list workspaces: ${res.statusText}`)
  return res.json()
}

export async function createWorkspace(payload: { name: string; description: string; scope: WorkspaceScope }): Promise<WorkspaceResponse> {
  const res = await fetch(`${BASE}/workspaces`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  if (!res.ok) throw new Error(`Failed to create workspace: ${res.statusText}`)
  return res.json()
}

export async function deleteWorkspace(workspaceId: string): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/workspaces/${workspaceId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Failed to delete workspace: ${res.statusText}`)
  return res.json()
}

export async function pauseRun(runId: string): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/runs/${runId}/pause`, { method: 'POST' })
  return res.json()
}

export async function resumeRun(runId: string): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/runs/${runId}/resume`, { method: 'POST' })
  return res.json()
}

export async function confirmAction(
  runId: string,
  confirmationId: string,
  confirmed: boolean,
  operatorId: string,
): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/runs/${runId}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmation_id: confirmationId, confirmed, operator_id: operatorId }),
  })
  return res.json()
}

export async function deleteRun(runId: string): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/runs/${runId}`, { method: 'DELETE' })
  return res.json()
}

export function connectEventStream(runId: string, onEvent: (event: HarnessEvent) => void): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/runs/${runId}/events`)

  ws.onmessage = (msg) => {
    try {
      const event: HarnessEvent = JSON.parse(msg.data)
      console.log(`[WS] #${event.seq} ${event.event_type}`, event.payload)
      onEvent(event)
    } catch {
      // ignore non-json messages (ping/pong)
    }
  }

  ws.onerror = () => {
    // Error handled by onclose which triggers reconnect in RunDetail
  }

  return ws
}
