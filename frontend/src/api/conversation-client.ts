const BASE = '/api/v1'

export interface Conversation {
  conversation_id: string
  user_id: string
  title: string
  status: 'active' | 'archived'
  created_at: number
  updated_at: number
  message_count: number
}

export interface ConversationMessageItem {
  seq: number
  run_id: string
  role: 'user' | 'assistant'
  content: string
  created_at: number
  status: string
}

export interface ConversationDetail {
  conversation: Conversation
  messages: ConversationMessageItem[]
}

export interface ConversationListResponse {
  conversations: Conversation[]
  total: number
}

export interface SendMessageResponse {
  run_id: string
  conversation_id: string
  seq: number
}

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
): Promise<SendMessageResponse> {
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
  if (!res.ok) throw new Error(`Failed to send message: ${res.statusText}`)
  return res.json()
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
