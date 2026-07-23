import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge } from '../../src/components/ui/StatusBadge'

describe('StatusBadge', () => {
  it('renders the localized label for running', () => {
    render(<StatusBadge status="running" />)
    expect(screen.getByText('运行中')).toBeInTheDocument()
  })

  it.each(['running', 'paused', 'completed', 'failed'] as const)(
    'applies a status color class for %s',
    (status) => {
      const { container } = render(<StatusBadge status={status} />)
      expect(container.firstChild).toHaveClass('inline-flex')
    },
  )

  it('merges className', () => {
    const { container } = render(
      <StatusBadge status="completed" className="extra-cls" />,
    )
    expect(container.firstChild).toHaveClass('extra-cls')
    expect(container.firstChild).toHaveClass('bg-status-success')
  })
})