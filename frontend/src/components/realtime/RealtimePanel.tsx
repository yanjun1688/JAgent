import { useMemo } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { Activity, WifiOff, Trash2 } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import type { WsEvent } from '../../api/types'
import { EventItem } from './EventItem'

export interface RealtimePanelProps {
  events: WsEvent[]
  runId: string | null
  runStatus: string | null
  isConnected: boolean
  onClear?: () => void
  className?: string
}

function buildStats(events: WsEvent[]) {
  return {
    tool: events.filter((e) => e.event_type.startsWith('Tool')).length,
    confirm: events.filter((e) => e.event_type.startsWith('Confirm')).length,
    guardrail: events.filter((e) => e.event_type.startsWith('Guardrail')).length,
  }
}

export function RealtimePanel({
  events,
  runId,
  runStatus,
  isConnected,
  onClear,
  className,
}: RealtimePanelProps) {
  const stats = useMemo(() => buildStats(events), [events])

  return (
    <div
      className={cn(
        'flex h-full w-full flex-col overflow-hidden rounded-2xl glass-elevated',
        className,
      )}
    >
      {/* Header */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border-soft px-4 py-3">
        <span
          className={cn(
            'flex h-7 w-7 items-center justify-center rounded-lg',
            isConnected ? 'bg-status-success/20 text-status-success' : 'bg-surface-1 text-text-muted',
          )}
        >
          <Activity size={15} />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-text-primary">实时事件流</p>
          <p className="truncate text-[10px] text-text-muted">
            {runId ? `Run ${runId.slice(0, 8)}` : '等待 Run 启动'}
          </p>
        </div>
        <span
          className={cn(
            'ml-auto inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium',
            isConnected
              ? 'bg-status-success/15 text-status-success'
              : 'bg-surface-1 text-text-muted',
          )}
        >
          {isConnected ? '已连接' : (
            <>
              <WifiOff size={10} /> 离线
            </>
          )}
        </span>
      </div>

      {/* Stats */}
      <div className="grid shrink-0 grid-cols-3 gap-2 border-b border-border-softer px-4 py-3">
        {(
          [
            ['工具', stats.tool, 'text-accent-primary'],
            ['确认', stats.confirm, 'text-status-warning'],
            ['护栏', stats.guardrail, 'text-status-error'],
          ] as const
        ).map(([label, value, color]) => (
          <div key={label} className="rounded-lg bg-surface-2 px-2 py-1.5 text-center">
            <p className={cn('text-base font-semibold', color)}>{value}</p>
            <p className="text-[10px] text-text-muted">{label}</p>
          </div>
        ))}
      </div>

      {/* Events list */}
      {onClear && (
        <div className="flex shrink-0 items-center justify-between px-4 py-1.5">
          <span className="text-[11px] text-text-muted">
            共 {events.length} 条事件 · 状态 {runStatus || '—'}
          </span>
          <button
            onClick={onClear}
            className="inline-flex items-center gap-1 text-[10px] text-text-muted hover:text-status-error"
          >
            <Trash2 size={11} /> 清空
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 py-2">
        {events.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-text-muted">
            <Activity size={22} />
            <p className="text-xs">发送任务后，事件将在此实时显示</p>
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {events.map((e) => (
              <motion.div
                key={`${e.run_id}-${e.seq}`}
                initial={{ opacity: 0, x: 12 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.18 }}
              >
                <EventItem
                  event={e}
                  isExpanded={false}
                  onToggle={() => undefined}
                />
              </motion.div>
            ))}
          </AnimatePresence>
        )}
      </div>
    </div>
  )
}