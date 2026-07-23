import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConversationItem } from '../../src/components/conversation/ConversationItem'
import type { Conversation } from '../../src/api/conversation-client'

function mkConversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    conversation_id: 'c1',
    user_id: 'u1',
    title: '测试对话',
    status: 'active',
    created_at: 1700000000,
    updated_at: 1700000000,
    message_count: 3,
    ...overrides,
  }
}

describe('ConversationItem', () => {
  it('renders conversation title and message count', () => {
    render(
      <ConversationItem
        conversation={mkConversation({ title: '脚本任务', message_count: 5 })}
        isActive={false}
        onSelect={() => undefined}
      />,
    )
    expect(screen.getByText('脚本任务')).toBeInTheDocument()
    expect(screen.getByText('5 条消息')).toBeInTheDocument()
  })

  it('shows placeholder for missing title', () => {
    render(
      <ConversationItem
        conversation={mkConversation({ title: '' })}
        isActive={false}
        onSelect={() => undefined}
      />,
    )
    expect(screen.getByText('新对话')).toBeInTheDocument()
  })

  it('applies active highlight class when selected', () => {
    const { container } = render(
      <ConversationItem
        conversation={mkConversation()}
        isActive
        onSelect={() => undefined}
      />,
    )
    expect(container.firstChild).toHaveClass('bg-accent-primary/10')
  })

  it('calls onSelect with conversation id', async () => {
    const onSelect = vi.fn()
    render(
      <ConversationItem
        conversation={mkConversation({ conversation_id: 'conv-42' })}
        isActive={false}
        onSelect={onSelect}
      />,
    )
    await userEvent.click(screen.getByText('测试对话'))
    expect(onSelect).toHaveBeenCalledWith('conv-42')
  })

  it('calls onDelete without propagating to select', () => {
    const onSelect = vi.fn()
    const onDelete = vi.fn()
    render(
      <ConversationItem
        conversation={mkConversation({ conversation_id: 'cx' })}
        isActive={false}
        onSelect={onSelect}
        onDelete={onDelete}
      />,
    )
    const deleteBtn = screen.getByLabelText('删除对话')
    fireEvent.click(deleteBtn)
    expect(onDelete).toHaveBeenCalledWith('cx')
    expect(onSelect).not.toHaveBeenCalled()
  })
})