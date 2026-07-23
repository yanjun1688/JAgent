import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GlowButton } from '../../src/components/ui/GlowButton'

describe('GlowButton', () => {
  it('renders children', () => {
    render(<GlowButton>提交</GlowButton>)
    expect(screen.getByText('提交')).toBeInTheDocument()
  })

  it('defaults to primary variant classes', () => {
    const { container } = render(<GlowButton>x</GlowButton>)
    expect(container.querySelector('button')).toHaveClass('from-accent-primary')
  })

  it('uses secondary variant when requested', () => {
    const { container } = render(<GlowButton variant="secondary">y</GlowButton>)
    expect(container.querySelector('button')).toHaveClass('glass-base')
  })

  it('triggers onClick', async () => {
    const onClick = vi.fn()
    render(<GlowButton onClick={onClick}>发送</GlowButton>)
    await userEvent.click(screen.getByText('发送'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})