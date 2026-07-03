import { motion } from 'motion/react'
import { ChevronRight, Clock } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import { StatusBadge, type Status } from '../ui/StatusBadge'
import type { RunSummary } from '../../api/schema'

export interface RunCardProps {
  run: RunSummary
  isSelected: boolean
  onSelect: (runId: string) => void
}

function normalizeStatus(s: string | undefined): Status {
  switch (s) {
    case 'running':
      return 'running'
    case 'paused':
      return 'paused'
    case 'completed':
      return 'completed'
    case 'failed':
      return 'failed'
    default:
      return 'running'
  }
}

function formatTime(ts: number | undefined): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function RunCard({ run, isSelected, onSelect }: RunCardProps) {
  const status = normalizeStatus(run.status)
  return (
    <motion.button
      layout
      onClick={() => onSelect(run.run_id)}
      whileHover={{ scale: 1.005 }}
      whileTap={{ scale: 0.99 }}
      className={cn(
        'flex w-full items-start gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors',
        isSelected
          ? 'border-accent-primary/40 bg-accent-primary/10'
          : 'border-border-softer bg-surface-0 hover:bg-surface-1',
      )}
    >
      <span
        className={cn(
          'mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
          statusClasses(status),
        )}
      >
        <span className="h-2 w-2 rounded-full bg-current" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-mono text-xs text-text-secondary">
            {run.run_id.slice(0, 12)}
          </span>
          <StatusBadge status={status} className="ml-auto" />
        </div>
        <p className="mt-1 line-clamp-2 text-xs text-text-primary">
          {run.intent || '(无意图)'}
        </p>
        <div className="mt-1 flex items-center gap-3 text-[10px] text-text-muted">
          <span className="inline-flex items-center gap-1">
            <Clock size={10} />
            {formatTime(run.created_at)}
          </span>
          <span>{run.event_count ?? 0} 事件</span>
        </div>
      </div>
      <ChevronRight
        size={14}
        className={cn('mt-1 shrink-0 text-text-muted', isSelected && 'text-accent-primary')}
      />
    </motion.button>
  )
}

function statusClasses(status: Status): string {
  switch (status) {
    case 'running':
      return 'bg-status-info/20 text-status-info'
    case 'paused':
      return 'bg-status-warning/20 text-status-warning'
    case 'completed':
      return 'bg-status-success/20 text-status-success'
    case 'failed':
      return 'bg-status-error/20 text-status-error'
  }
}