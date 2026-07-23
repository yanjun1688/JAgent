import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ConfirmationCard from './ConfirmationCard'

describe('ConfirmationCard', () => {
  const onApprove = vi.fn()
  const onDeny = vi.fn()

  it('renders confirmation UI with tool name', () => {
    render(
      <ConfirmationCard
        confirmationId="conf-1"
        toolName="file_write"
        riskLevel="high"
        onApprove={onApprove}
        onDeny={onDeny}
      />,
    )
    expect(screen.getByText('Confirmation Required')).toBeInTheDocument()
    expect(screen.getByText('file_write')).toBeInTheDocument()
    expect(screen.getByText('(risk: high)')).toBeInTheDocument()
  })

  it('calls onApprove when approve button clicked', () => {
    render(
      <ConfirmationCard
        confirmationId="conf-1"
        toolName="file_write"
        onApprove={onApprove}
        onDeny={onDeny}
      />,
    )
    fireEvent.click(screen.getByText('Approve & Continue'))
    expect(onApprove).toHaveBeenCalledWith('conf-1')
  })

  it('calls onDeny when deny button clicked', () => {
    render(
      <ConfirmationCard
        confirmationId="conf-2"
        toolName="shell_exec"
        onApprove={onApprove}
        onDeny={onDeny}
      />,
    )
    fireEvent.click(screen.getByText('Deny & Continue'))
    expect(onDeny).toHaveBeenCalledWith('conf-2')
  })

  it('shows tool input when provided', () => {
    const input = { path: '/etc/config', content: 'secret' }
    render(
      <ConfirmationCard
        confirmationId="conf-3"
        toolName="file_write"
        input={input}
        onApprove={onApprove}
        onDeny={onDeny}
      />,
    )
    expect(screen.getByText(/\/etc\/config/)).toBeInTheDocument()
  })

  it('disables buttons when loading', () => {
    render(
      <ConfirmationCard
        confirmationId="conf-4"
        toolName="file_write"
        onApprove={onApprove}
        onDeny={onDeny}
        loading={true}
      />,
    )
    expect(screen.getByText('Approve & Continue')).toBeDisabled()
    expect(screen.getByText('Deny & Continue')).toBeDisabled()
  })
})
