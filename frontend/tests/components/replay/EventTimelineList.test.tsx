import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EventTimelineList } from '../../../src/components/replay/EventTimelineList'
import type { ReplayTimelineEvent } from '../../../src/api/schema'

function ev(seq: number, event_type: string, extra: Partial<ReplayTimelineEvent> = {}): ReplayTimelineEvent {
  return {
    seq,
    event_type,
    created_at: 0,
    payload: {},
    is_terminal: false,
    ...extra,
  }
}

const events: ReplayTimelineEvent[] = [
  ev(1, 'RunStarted'),
  ev(6, 'DagStepCompleted', { step_id: 's1' }),
  ev(8, 'GuardrailTriggered', { tool_name: 'write' }),
  ev(11, 'RunFailed', { is_terminal: true }),
]

describe('EventTimelineList', () => {
  it('renders every event with its seq', () => {
    render(
      <EventTimelineList
        events={events}
        selectedSeq={null}
        compareMode={false}
        fromSeq={null}
        toSeq={null}
        onSelectSeq={() => {}}
      />,
    )
    expect(screen.getByText('#1')).toBeInTheDocument()
    expect(screen.getByText('#11')).toBeInTheDocument()
    expect(screen.getByText('终态')).toBeInTheDocument()
  })

  it('single-point mode: clicking an event selects that seq', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(
      <EventTimelineList
        events={events}
        selectedSeq={6}
        compareMode={false}
        fromSeq={null}
        toSeq={null}
        onSelectSeq={onSelect}
      />,
    )
    await user.click(screen.getByText('#8'))
    expect(onSelect).toHaveBeenCalledWith(8)
  })

  it('compare mode: marks from (A) and to (B) endpoints', () => {
    render(
      <EventTimelineList
        events={events}
        selectedSeq={null}
        compareMode
        fromSeq={6}
        toSeq={11}
        onSelectSeq={() => {}}
      />,
    )
    expect(screen.getByText('A')).toBeInTheDocument()
    expect(screen.getByText('B')).toBeInTheDocument()
  })

  it('compare mode: first click sets A and second sets B', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const { rerender } = render(
      <EventTimelineList
        events={events}
        selectedSeq={null}
        compareMode
        fromSeq={null}
        toSeq={null}
        onSelectSeq={onSelect}
      />,
    )
    await user.click(screen.getByText('#6'))
    expect(onSelect).toHaveBeenNthCalledWith(1, 6)

    rerender(
      <EventTimelineList
        events={events}
        selectedSeq={null}
        compareMode
        fromSeq={6}
        toSeq={null}
        onSelectSeq={onSelect}
      />,
    )
    await user.click(screen.getByText('#11'))
    expect(onSelect).toHaveBeenNthCalledWith(2, 11)
  })
})
