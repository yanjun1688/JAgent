import { useState } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { ChevronRight, CircleDot } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import type { WsEvent } from '../../api/types'

export interface EventItemProps {
  event: WsEvent
  isExpanded: boolean
  onToggle: () => void
}

const TERMINAL_TYPES = new Set(['RunCompleted', 'RunFailed'])

function categoryColor(eventType: string): string {
  if (eventType.startsWith('Tool')) return 'text-accent-primary'
  if (eventType.startsWith('Run')) return 'text-accent-secondary'
  if (eventType.startsWith('Confirm')) return 'text-status-warning'
  if (eventType.startsWith('Guardrail')) return 'text-status-error'
  if (eventType.startsWith('Think') || eventType.startsWith('Observe'))
    return 'text-status-info'
  return 'text-text-tertiary'
}

export function EventItem({ event, isExpanded, onToggle }: EventItemProps) {
  const [ownExpanded, setOwnExpanded] = useState(false)
  const expanded = isExpanded || ownExpanded
  const hasPayload = Boolean(event.payload && Object.keys(event.payload).length > 0)
  const isTerminal = TERMINAL_TYPES.has(event.event_type)

  return (
    <div className="text-xs">
      <div
        onClick={() => (hasPayload ? setOwnExpanded((v) => !v) : undefined)}
        className={cn(
          'flex items-center gap-2 rounded-lg px-2 py-1.5',
          hasPayload && 'cursor-pointer hover:bg-surface-1',
        )}
      >
        <span
          className={cn(
            'flex h-5 w-5 shrink-0 items-center justify-center',
            categoryColor(event.event_type),
          )}
        >
          <CircleDot size={10} />
        </span>
        <span className="text-text-muted">#{event.seq}</span>
        <span className={cn('font-mono', categoryColor(event.event_type))}>
          {event.event_type}
        </span>
        {isTerminal && (
          <span className="rounded-full bg-surface-1 px-1.5 text-[10px] text-text-muted">终态</span>
        )}
        {hasPayload && (
          <ChevronRight
            size={12}
            className={cn(
              'ml-auto text-text-muted transition-transform',
              expanded && 'rotate-90',
            )}
          />
        )}
      </div>
      <AnimatePresence initial={false}>
        {expanded && hasPayload && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
          >
            <pre className="ml-7 max-h-40 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-[11px] text-text-secondary">
              {JSON.stringify(event.payload, null, 2)}
            </pre>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}