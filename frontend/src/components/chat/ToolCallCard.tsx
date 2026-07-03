import { useState } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import {
  Wrench,
  Check,
  X,
  Clock,
  ShieldAlert,
  ChevronRight,
  Loader2,
  type LucideIcon,
} from 'lucide-react'
import { cn } from '../../design-system/utils/cn'

export type ToolCallStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'timeout'
  | 'guardrail_blocked'

export interface ToolCallCardProps {
  toolName: string
  status: ToolCallStatus
  input?: Record<string, unknown> | null
  output?: unknown
  error?: string | null
  durationMs?: number | null
}

interface StatusConfig {
  icon: LucideIcon
  label: string
  colorClass: string
  badgeClass: string
}

const statusConfig: Record<ToolCallStatus, StatusConfig> = {
  running: {
    icon: Loader2,
    label: '执行中',
    colorClass: 'text-status-info',
    badgeClass: 'bg-status-info/15 text-status-info',
  },
  completed: {
    icon: Check,
    label: '已完成',
    colorClass: 'text-status-success',
    badgeClass: 'bg-status-success/15 text-status-success',
  },
  failed: {
    icon: X,
    label: '失败',
    colorClass: 'text-status-error',
    badgeClass: 'bg-status-error/15 text-status-error',
  },
  timeout: {
    icon: Clock,
    label: '超时',
    colorClass: 'text-status-warning',
    badgeClass: 'bg-status-warning/15 text-status-warning',
  },
  guardrail_blocked: {
    icon: ShieldAlert,
    label: '已被护栏拦截',
    colorClass: 'text-status-error',
    badgeClass: 'bg-status-error/15 text-status-error',
  },
}

function formatDuration(ms: number | null): string {
  if (ms == null) return ''
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function ToolCallCard({
  toolName,
  status,
  input,
  output,
  error,
  durationMs,
}: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false)
  const cfg = statusConfig[status]
  const Icon = cfg.icon
  const hasDetail = Boolean((input && Object.keys(input).length > 0) || output != null || error)

  return (
    <div className="overflow-hidden rounded-xl border border-border-soft bg-surface-2">
      <div
        onClick={() => hasDetail && setExpanded((v) => !v)}
        className={cn(
          'flex items-center gap-2 px-3 py-2.5',
          hasDetail && 'cursor-pointer hover:bg-surface-1',
        )}
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent-primary/15 text-accent-primary">
          <Wrench size={13} />
        </span>
        <span className="text-sm font-medium text-text-primary">{toolName}</span>
        <span
          className={cn(
            'ml-auto inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
            cfg.badgeClass,
          )}
        >
          <Icon
            size={11}
            className={cn(status === 'running' && 'animate-spin')}
          />
          {cfg.label}
        </span>
        {durationMs != null && (
          <span className="text-[10px] text-text-muted">{formatDuration(durationMs)}</span>
        )}
        {hasDetail && (
          <ChevronRight
            size={14}
            className={cn(
              'text-text-muted transition-transform',
              expanded && 'rotate-90',
            )}
          />
        )}
      </div>
      <AnimatePresence initial={false}>
        {expanded && hasDetail && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <div className="space-y-2 border-t border-border-softer px-3 py-2.5 text-xs">
              {input && Object.keys(input).length > 0 && (
                <div>
                  <p className="mb-1 text-text-tertiary">输入</p>
                  <pre className="max-h-40 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-text-secondary">
                    {JSON.stringify(input, null, 2)}
                  </pre>
                </div>
              )}
              {output != null && (
                <div>
                  <p className="mb-1 text-text-tertiary">输出</p>
                  <pre className="max-h-40 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-text-secondary">
                    {typeof output === 'string' ? output : JSON.stringify(output, null, 2)}
                  </pre>
                </div>
              )}
              {error && (
                <div>
                  <p className="mb-1 text-status-error">错误</p>
                  <pre className="overflow-auto rounded-lg bg-status-error/10 p-2 font-mono text-status-error">
                    {error}
                  </pre>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}