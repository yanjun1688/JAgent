import { memo, useState } from 'react'
import { ChevronDown, ChevronRight, CircleDot } from 'lucide-react'
import type { ReplayTimelineEvent } from '../../api/schema'
import { cn } from '../../design-system/utils/cn'
import { eventTypeColor } from './statusColors'

export interface EventTimelineListProps {
  events: ReplayTimelineEvent[]
  // Point under inspection in single-point mode (state as-of this seq).
  selectedSeq: number | null
  // Compare mode: first click = A (from), second = B (to).
  compareMode: boolean
  fromSeq: number | null
  toSeq: number | null
  onSelectSeq: (seq: number) => void
}

type RowState = 'none' | 'selected' | 'from' | 'to' | 'in-range'

function markerFor(state: RowState): string | null {
  if (state === 'selected') return '●'
  if (state === 'from') return 'A'
  if (state === 'to') return 'B'
  return null
}

interface RowProps {
  event: ReplayTimelineEvent
  state: RowState
  onSelect: (seq: number) => void
}

const TimelineRow = memo(function TimelineRow({ event, state, onSelect }: RowProps) {
  const [open, setOpen] = useState(false)
  const marker = markerFor(state)
  return (
    <div
      className={cn(
        'rounded-lg border transition-colors',
        state === 'selected' && 'border-accent-primary/50 bg-accent-primary/10',
        state === 'from' && 'border-status-info/50 bg-status-info/10',
        state === 'to' && 'border-status-success/50 bg-status-success/10',
        state === 'in-range' && 'border-border-softer bg-surface-1/60',
        state === 'none' && 'border-transparent hover:bg-surface-1',
      )}
    >
      <div
        role="button"
        tabIndex={0}
        onClick={() => onSelect(event.seq)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onSelect(event.seq)
          }
        }}
        className="flex w-full cursor-pointer items-center gap-2 px-2 py-1.5 text-left"
      >
        <button
          onClick={(e) => {
            e.stopPropagation()
            setOpen((v) => !v)
          }}
          className="flex h-4 w-4 shrink-0 items-center justify-center text-text-muted hover:text-text-primary"
          aria-label={open ? '收起 payload' : '展开 payload'}
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </button>
        <CircleDot size={11} className="shrink-0 text-text-tertiary" />
        <span className="shrink-0 font-mono text-[10px] text-text-muted">#{event.seq}</span>
        <span className={cn('truncate font-mono text-[11px]', eventTypeColor(event.event_type))}>
          {event.event_type}
        </span>
        {event.tool_name && (
          <span className="hidden truncate text-[10px] text-text-tertiary sm:inline">
            {event.tool_name}
          </span>
        )}
        {event.is_terminal && (
          <span className="ml-auto shrink-0 rounded-full bg-status-error/15 px-1.5 py-0.5 text-[9px] text-status-error">
            终态
          </span>
        )}
        {marker && (
          <span
            className={cn(
              'ml-auto flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold',
              state === 'selected' && 'bg-accent-primary text-white',
              state === 'from' && 'bg-status-info text-white',
              state === 'to' && 'bg-status-success text-white',
            )}
          >
            {marker}
          </span>
        )}
      </div>
      {open && (
        <pre className="mx-2 mb-2 max-h-44 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-[10px] text-text-secondary">
          {JSON.stringify(event.payload, null, 2)}
        </pre>
      )}
    </div>
  )
})

export function EventTimelineList({
  events,
  selectedSeq,
  compareMode,
  fromSeq,
  toSeq,
  onSelectSeq,
}: EventTimelineListProps) {
  const lo = fromSeq != null && toSeq != null ? Math.min(fromSeq, toSeq) : null
  const hi = fromSeq != null && toSeq != null ? Math.max(fromSeq, toSeq) : null

  const rowState = (seq: number): RowState => {
    if (compareMode) {
      if (seq === fromSeq) return 'from'
      if (seq === toSeq) return 'to'
      if (lo != null && hi != null && seq > lo && seq < hi) return 'in-range'
      return 'none'
    }
    return seq === selectedSeq ? 'selected' : 'none'
  }

  return (
    <div className="space-y-0.5">
      {events.map((event) => (
        <TimelineRow key={event.seq} event={event} state={rowState(event.seq)} onSelect={onSelectSeq} />
      ))}
    </div>
  )
}
