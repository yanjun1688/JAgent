import { useState } from 'react'
import { FlaskConical, Loader2, Search } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { listRuns } from '../../api/client'
import { cn } from '../../design-system/utils/cn'
import { statusTone, toneClasses, statusLabel } from './statusColors'

export interface ReplayRunPickerProps {
  selectedRunId: string | null
  onSelect: (runId: string) => void
}

export function ReplayRunPicker({ selectedRunId, onSelect }: ReplayRunPickerProps) {
  const [search, setSearch] = useState('')
  const [manualId, setManualId] = useState('')
  const { data, isLoading, error } = useQuery({
    queryKey: ['runs', 'replay-picker'],
    queryFn: () => listRuns(),
    refetchInterval: 10000,
  })

  const runs = (data?.runs ?? []).filter(
    (r) =>
      r.run_id.toLowerCase().includes(search.toLowerCase()) ||
      r.intent.toLowerCase().includes(search.toLowerCase()),
  )

  const submitManual = (e: React.FormEvent) => {
    e.preventDefault()
    const id = manualId.trim()
    if (id) onSelect(id)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-2 border-b border-border-soft px-3 py-3">
        <form onSubmit={submitManual} className="flex items-center gap-2">
          <input
            value={manualId}
            onChange={(e) => setManualId(e.target.value)}
            placeholder="粘贴 run_id 直接调试…"
            className="min-w-0 flex-1 rounded-lg border border-border-soft bg-surface-1 px-2.5 py-1.5 font-mono text-xs text-text-primary placeholder:text-text-muted focus:border-accent-primary focus:outline-none"
            aria-label="输入 run_id"
          />
          <button
            type="submit"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent-primary/20 text-accent-primary hover:bg-accent-primary/30"
            title="打开该 run"
          >
            <FlaskConical size={14} />
          </button>
        </form>
        <div className="flex items-center gap-2 rounded-lg border border-border-softer bg-surface-1 px-2.5 py-1.5">
          <Search size={13} className="shrink-0 text-text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="筛选 run / 意图…"
            className="min-w-0 flex-1 bg-transparent text-xs text-text-primary placeholder:text-text-muted focus:outline-none"
            aria-label="筛选 run"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {isLoading && runs.length === 0 && (
          <div className="flex items-center justify-center py-8 text-text-muted">
            <Loader2 size={18} className="animate-spin" />
          </div>
        )}
        {error && (
          <p className="px-2 py-4 text-center text-xs text-status-error">
            加载 run 列表失败：{error instanceof Error ? error.message : String(error)}
          </p>
        )}
        {!isLoading && runs.length === 0 && (
          <p className="px-2 py-6 text-center text-xs text-text-muted">没有可调试的 run</p>
        )}
        <div className="space-y-1">
          {runs.map((run) => {
            const active = run.run_id === selectedRunId
            const tone = toneClasses(statusTone(run.status))
            return (
              <button
                key={run.run_id}
                onClick={() => onSelect(run.run_id)}
                className={cn(
                  'w-full rounded-xl border px-3 py-2 text-left transition-colors',
                  active
                    ? 'border-accent-primary/40 bg-accent-primary/10'
                    : 'border-border-softer bg-surface-0 hover:bg-surface-1',
                )}
              >
                <div className="flex items-center gap-2">
                  <span className={cn('h-2 w-2 shrink-0 rounded-full', tone.dot.split(' ')[0])} />
                  <span className="truncate font-mono text-xs text-text-primary">
                    {run.run_id.slice(0, 16)}
                  </span>
                  <span className={cn('ml-auto shrink-0 rounded-full px-2 py-0.5 text-[10px]', tone.badge)}>
                    {statusLabel(run.status)}
                  </span>
                </div>
                <p className="mt-1 truncate text-[11px] text-text-muted">{run.intent || '(无意图)'}</p>
                <p className="mt-0.5 text-[10px] text-text-tertiary">{run.event_count} 事件</p>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
