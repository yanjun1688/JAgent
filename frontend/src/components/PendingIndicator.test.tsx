import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import PendingIndicator from './PendingIndicator'

describe('PendingIndicator', () => {
  it('shows single pending message text', () => {
    render(<PendingIndicator count={1} />)
    expect(screen.getByText('1 message pending')).toBeInTheDocument()
  })

  it('shows multiple pending messages text', () => {
    render(<PendingIndicator count={3} />)
    expect(screen.getByText('3 messages pending')).toBeInTheDocument()
  })

  it('shows cancel button when onCancel provided', () => {
    render(<PendingIndicator count={2} onCancel={() => {}} />)
    expect(screen.getByText('Cancel all')).toBeInTheDocument()
  })

  it('does not render when count is 0', () => {
    const { container } = render(<PendingIndicator count={0} />)
    expect(container.firstChild).toBeNull()
  })

  it('does not render when count is negative', () => {
    const { container } = render(<PendingIndicator count={-1} />)
    expect(container.firstChild).toBeNull()
  })
})
