const BASE = '/api/v1'

// Single source of truth (AGENTS.md §4.1): all shared data types come from the
// OpenAPI-generated schema.ts. These re-exports keep existing imports stable.
import type {
  ConversationDetail,
  ConversationListResponse,
  SendMessageResponse,
} from './schema'

export type {
  Conversation,
  ConversationDetail,
  ConversationListResponse,
  ConversationMessageItem,
  SendMessageResponse,
} from './schema'

// Structured API error carrying the HTTP status so callers can distinguish a
// deterministic client error (e.g. 404 conversation gone) from a transient
// failure (network / 5xx). Mirrors AnalysisApiError / ReplayApiError.
export class ConversationApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ConversationApiError'
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
    throw new ConversationApiError(res.status, detail)
  }
}

// Only transient failures justify an idempotent retry. Deterministic 4xx
// (404 conversation deleted/reset, 400/422 bad request, 401/403) will fail
// the same way on every attempt, so retrying just hammers the API and spams
// the log with duplicate 404s (root cause of the repeated POST .../messages 404).
export function isRetryableConversationError(err: unknown): boolean {
  if (err instanceof ConversationApiError) {
    return err.status >= 500
  }
  // Network errors (fetch rejected with TypeError) are transient.
  return true
}

export async function createConversation(title?: string): Promise<{ conversation_id: string; title: string; created_at: number }> {
  const res = await fetch(`${BASE}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  await checkResponse(res)
  return res.json()
}

export async function listConversations(): Promise<ConversationListResponse> {
  const res = await fetch(`${BASE}/conversations`)
  await checkResponse(res)
  return res.json()
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const res = await fetch(`${BASE}/conversations/${id}`)
  await checkResponse(res)
  return res.json()
}

export async function sendMessage(
  conversationId: string,
  message: string,
  clientRequestId?: string,
  workspaceId?: string,
): Promise<SendMessageResponse> {
  const body: Record<string, unknown> = { message }
  if (clientRequestId) body.client_request_id = clientRequestId
  if (workspaceId) body.workspace_id = workspaceId
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  await checkResponse(res)
  return res.json()
}

/**
 * Generate a client-side idempotency key for a message submission.
 * The backend dedups by (conversation, client_request_id) so the same logical
 * submit cannot create duplicate runs (P0-06 §7.5 / JAGENT-2026-P0-07).
 */
export function createClientRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export async function deleteConversation(id: string): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/conversations/${id}`, { method: 'DELETE' })
  await checkResponse(res)
  return res.json()
}

export async function updateConversation(
  id: string,
  payload: { title?: string; status?: string },
): Promise<{ success: boolean }> {
  const res = await fetch(`${BASE}/conversations/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  await checkResponse(res)
  return res.json()
}

const CONVERSATION_KEY = 'harness_current_conversation_id'

export function persistCurrentConversationId(id: string | null): void {
  if (id) {
    localStorage.setItem(CONVERSATION_KEY, id)
  } else {
    localStorage.removeItem(CONVERSATION_KEY)
  }
}

export function restoreCurrentConversationId(): string | null {
  return localStorage.getItem(CONVERSATION_KEY)
}
