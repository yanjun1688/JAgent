import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThinkingPanel } from '../../src/components/chat/ThinkingPanel'
import type { WsEvent } from '../../src/api/types'

function mkEvent(event_type: string): WsEvent {
  return {
    run_id: 'r1',
    seq: 1,
    event_type,
    payload: {},
    idempotency_key: null,
    created_at: 0,
    tool_call_id: null,
    tool_name: null,
    input: null,
    error: null,
    duration_ms: null,
    confirmation_id: null,
  }
}

describe('ThinkingPanel', () => {
  it('shows header in idle/loading state', () => {
    render(
      <ThinkingPanel events={[]} open={false} loading={false} onToggle={() => undefined} />,
    )
    expect(screen.getByText('思考过程')).toBeInTheDocument()
  })

  it('shows loading label while computing', () => {
    render(
      <ThinkingPanel events={[]} open={false} loading onToggle={() => undefined} />,
    )
    expect(screen.getByText('Agent 正在思考…')).toBeInTheDocument()
  })

  it('counts recognized thinking steps', () => {
    render(
      <ThinkingPanel
        events={[
          mkEvent('ThinkStarted'),
          mkEvent('ThinkCompleted'),
          mkEvent('ActStarted'),
        ]}
        open
        loading={false}
        onToggle={() => undefined}
      />,
    )
    expect(screen.getByText('3 步')).toBeInTheDocument()
  })

  it('toggles on header click', async () => {
    const onToggle = vi.fn()
    render(
      <ThinkingPanel events={[]} open={false} loading={false} onToggle={onToggle} />,
    )
    await userEvent.click(screen.getByText('思考过程'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })
})