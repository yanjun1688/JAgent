import { useMemo } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { History, Search } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import type { RunSummary } from '../../api/schema'
import { RunCard } from './RunCard'

export interface RunTimelineProps {
  runs: RunSummary[]
  selectedRunId: string | null
  searchQuery: string
  statusFilter: string | null
  isLoading: boolean
  error: string | null
  onSearchChange: (q: string) => void
  onFilterChange: (s: string | null) => void
  onSelect: (runId: string) => void
  className?: string
}

const FILTERS: Array<{ key: string; label: string }> = [
  { key: 'running', label: '运行中' },
  { key: 'paused', label: '已暂停' },
  { key: 'completed', label: '已完成' },
  { key: 'failed', label: '已失败' },
]

export function RunTimeline({
  runs,
  selectedRunId,
  searchQuery,
  statusFilter,
  isLoading,
  error,
  onSearchChange,
  onFilterChange,
  onSelect,
  className,
}: RunTimelineProps) {
  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    return runs.filter((r) => {
      if (statusFilter && r.status !== statusFilter) return false
      if (q && !(r.intent || '').toLowerCase().includes(q) && !r.run_id.includes(q))
        return false
      return true
    })
  }, [runs, searchQuery, statusFilter])

  return (
    <div className={cn('flex h-full w-full flex-col', className)}>
      <div className="flex items-center gap-2 px-4 pb-2 pt-3">
        <History size={15} className="text-accent-primary" />
        <h2 className="font-display text-sm font-semibold text-text-primary">Run 时间线</h2>
      </div>

      <div className="px-4 pb-2">
        <div className="relative">
          <Search
            size={13}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
          />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="搜索 Run..."
            className="w-full rounded-lg border border-border-soft bg-surface-1 py-1.5 pl-8 pr-3 text-xs text-text-primary placeholder:text-text-muted focus:border-accent-primary/50 focus:outline-none"
          />
        </div>
      </div>

      <div className="flex shrink-0 gap-1 px-4 pb-2">
        <button
          onClick={() => onFilterChange(null)}
          className={cn(
            'rounded-full px-2 py-0.5 text-[10px] transition-colors',
            statusFilter == null
              ? 'bg-accent-primary/20 text-accent-primary'
              : 'bg-surface-1 text-text-muted hover:text-text-primary',
          )}
        >
          全部
        </button>
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => onFilterChange(f.key)}
            className={cn(
              'rounded-full px-2 py-0.5 text-[10px] transition-colors',
              statusFilter === f.key
                ? 'bg-accent-primary/20 text-accent-primary'
                : 'bg-surface-1 text-text-muted hover:text-text-primary',
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 pb-3">
        {error && <p className="px-3 py-4 text-center text-xs text-status-error">{error}</p>}
        {isLoading && !error && (
          <div className="space-y-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-16 animate-pulse rounded-xl bg-surface-1" />
            ))}
          </div>
        )}
        {!isLoading && filtered.length === 0 && !error && (
          <p className="py-10 text-center text-xs text-text-muted">无匹配的 Run</p>
        )}
        <AnimatePresence initial={false}>
          {filtered.map((r) => (
            <motion.div
              key={r.run_id}
              layout
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
            >
              <RunCard run={r} isSelected={r.run_id === selectedRunId} onSelect={onSelect} />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}