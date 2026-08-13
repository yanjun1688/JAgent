import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  persistCurrentConversationId,
  restoreCurrentConversationId,
  sendMessage,
  createClientRequestId,
} from './conversation-client'

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
describe('sendMessage (P0-06/M3 client_request_id)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends client_request_id when provided', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve({ run_id: 'run-1', conversation_id: 'conv-1', seq: 3 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const resp = await sendMessage('conv-1', 'hello', 'idem-123')

    expect(resp.run_id).toBe('run-1')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/conversations/conv-1/messages')
    expect(JSON.parse(init.body)).toEqual({ message: 'hello', client_request_id: 'idem-123' })
  })

  it('omits client_request_id when not provided', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: () => Promise.resolve({ run_id: 'run-2', conversation_id: 'conv-1', seq: 4 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await sendMessage('conv-1', 'hello')

    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({ message: 'hello' })
  })

  it('generates a unique client request id', () => {
    const a = createClientRequestId()
    const b = createClientRequestId()
    expect(a).toBeTruthy()
    expect(a).not.toBe(b)
  })
})
