import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import ConversationSidebar from './ConversationSidebar'

const mockListConversations = vi.fn()

vi.mock('../api/conversation-client', () => ({
  listConversations: () => mockListConversations(),
  deleteConversation: vi.fn().mockResolvedValue({ success: true }),
  persistCurrentConversationId: vi.fn(),
  restoreCurrentConversationId: vi.fn().mockReturnValue(null),
}))

describe('ConversationSidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading state initially', () => {
    mockListConversations.mockReturnValue(new Promise(() => {}))
    render(<ConversationSidebar activeConversationId={null} onSelect={vi.fn()} onNew={vi.fn()} />)
    expect(screen.getByText('Loading...')).toBeInTheDocument()
  })

  it('shows empty state when no conversations', async () => {
    mockListConversations.mockResolvedValue({ conversations: [], total: 0 })
    render(<ConversationSidebar activeConversationId={null} onSelect={vi.fn()} onNew={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('No conversations yet')).toBeInTheDocument()
    })
  })

  it('renders conversation list', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [
        {
          conversation_id: 'c1',
          user_id: 'default',
          title: 'Test Chat 1',
          status: 'active' as const,
          created_at: 1,
          updated_at: 3,
          message_count: 5,
        },
        {
          conversation_id: 'c2',
          user_id: 'default',
          title: 'Test Chat 2',
          status: 'active' as const,
          created_at: 2,
          updated_at: 2,
          message_count: 1,
        },
      ],
      total: 2,
    })
    render(<ConversationSidebar activeConversationId={null} onSelect={vi.fn()} onNew={vi.fn()} />)
    await waitFor(() => {
      expect(screen.getByText('Test Chat 1')).toBeInTheDocument()
      expect(screen.getByText('Test Chat 2')).toBeInTheDocument()
    })
  })

  it('highlights active conversation', async () => {
    mockListConversations.mockResolvedValue({
      conversations: [
        {
          conversation_id: 'c1',
          user_id: 'default',
          title: 'Active Chat',
          status: 'active' as const,
          created_at: 1,
          updated_at: 1,
          message_count: 2,
        },
      ],
      total: 1,
    })
    render(<ConversationSidebar activeConversationId="c1" onSelect={vi.fn()} onNew={vi.fn()} />)
    await waitFor(() => {
      const item = screen.getByText('Active Chat').closest('div[style*="cursor: pointer"]')
      expect(item).toBeTruthy()
    })
  })
})
