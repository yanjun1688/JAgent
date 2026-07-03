import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GlassCard } from '../../src/components/ui/GlassCard'

describe('GlassCard', () => {
  it('renders children', () => {
    render(<GlassCard>内容</GlassCard>)
    expect(screen.getByText('内容')).toBeInTheDocument()
  })

  it('applies variant class', () => {
    const { container } = render(<GlassCard variant="elevated">x</GlassCard>)
    expect(container.firstChild).toHaveClass('glass-elevated')
  })

  it('merges custom className with cn', () => {
    const { container } = render(
      <GlassCard className="custom-cls" variant="base">
        y
      </GlassCard>,
    )
    expect(container.firstChild).toHaveClass('rounded-2xl')
    expect(container.firstChild).toHaveClass('custom-cls')
  })

  it('handles click events', async () => {
    const onClick = vi.fn()
    render(<GlassCard onClick={onClick}>点我</GlassCard>)
    await userEvent.click(screen.getByText('点我'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})