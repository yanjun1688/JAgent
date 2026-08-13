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

export async function createConversation(title?: string): Promise<{ conversation_id: string; title: string; created_at: number }> {
  const res = await fetch(`${BASE}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(`Failed to create conversation: ${res.statusText}`)
  return res.json()
}

export async function listConversations(): Promise<ConversationListResponse> {
  const res = await fetch(`${BASE}/conversations`)
  if (!res.ok) throw new Error(`Failed to list conversations: ${res.statusText}`)
  return res.json()
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const res = await fetch(`${BASE}/conversations/${id}`)
  if (!res.ok) throw new Error(`Failed to get conversation: ${res.statusText}`)
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
  if (!res.ok) throw new Error(`Failed to send message: ${res.statusText}`)
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
  if (!res.ok) throw new Error(`Failed to delete conversation: ${res.statusText}`)
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
  if (!res.ok) throw new Error(`Failed to update conversation: ${res.statusText}`)
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
