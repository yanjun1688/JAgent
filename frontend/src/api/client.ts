const BASE = '/api/v1'

// Types are generated from OpenAPI schema via `npm run generate-api`.
// See schema.ts for the full set of generated interfaces.
import type { RunSummary, WorkspaceListResponse, WorkspaceResponse, WorkspaceScope } from './schema'

export async function listRuns(workspaceId?: string): Promise<{ runs: RunSummary[]; total: number }> {
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
  const res = await fetch(`${BASE}/runs${query}`)
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
