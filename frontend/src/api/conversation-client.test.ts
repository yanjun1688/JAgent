import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { persistCurrentConversationId, restoreCurrentConversationId } from './conversation-client'

describe('conversation-client (localStorage helpers)', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('persists and restores conversation id', () => {
    persistCurrentConversationId('conv-123')
    expect(restoreCurrentConversationId()).toBe('conv-123')
  })

  it('returns null when no conversation stored', () => {
    expect(restoreCurrentConversationId()).toBeNull()
  })

  it('removes key when null is persisted', () => {
    persistCurrentConversationId('conv-456')
    persistCurrentConversationId(null)
    expect(restoreCurrentConversationId()).toBeNull()
  })
})
