import { motion } from 'motion/react'
import { Link } from 'react-router-dom'
import {
  X,
  Clock,
  Hash,
  Coins,
  Wrench,
  ShieldAlert,
  ChevronDown,
  ChevronRight,
  FlaskConical,
} from 'lucide-react'
import { useState } from 'react'
import { cn } from '../../design-system/utils/cn'
import { getRunAnalysis, getRunToolTraces } from '../../api/analysis-client'
import { useQuery } from '@tanstack/react-query'
import { StatusBadge, type Status } from '../../components/ui/StatusBadge'
import type { ToolTraceItem } from '../../api/analysis-types'

export interface RunDetailPanelProps {
  runId: string
  onClose: () => void
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

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const min = Math.floor(ms / 60_000)
  const sec = Math.floor((ms % 60_000) / 1000)
  return `${min}m${sec}s`
}

function TraceNode({ trace }: { trace: ToolTraceItem }) {
  const [open, setOpen] = useState(false)
  const failed = trace.status === 'failed' || trace.status === 'timeout'
  return (
    <div className="rounded-lg border border-border-softer bg-surface-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span
          className={cn(
            'flex h-5 w-5 items-center justify-center rounded-md',
            failed ? 'bg-status-error/15 text-status-error' : 'bg-accent-primary/15 text-accent-primary',
          )}
        >
          <Wrench size={11} />
        </span>
        <span className="truncate font-mono text-xs text-text-primary">{trace.tool_name}</span>
        <span
          className={cn(
            'ml-auto rounded-full px-2 py-0.5 text-[10px]',
            failed
              ? 'bg-status-error/15 text-status-error'
              : 'bg-status-success/15 text-status-success',
          )}
        >
          {trace.status}
        </span>
        <span className="text-[10px] text-text-muted">{formatDuration(trace.duration_ms)}</span>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} className="text-text-muted" />}
      </button>
      {open && (
        <div className="space-y-2 border-t border-border-softer px-3 py-2 text-xs">
          {trace.input && Object.keys(trace.input).length > 0 && (
            <div>
              <p className="mb-1 text-text-tertiary">输入</p>
              <pre className="max-h-40 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-text-secondary">
                {JSON.stringify(trace.input, null, 2)}
              </pre>
            </div>
          )}
          {trace.output != null && (
            <div>
              <p className="mb-1 text-text-tertiary">输出</p>
              <pre className="max-h-40 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-text-secondary">
                {typeof trace.output === 'string' ? trace.output : JSON.stringify(trace.output, null, 2)}
              </pre>
            </div>
          )}
          {trace.error && (
            <div>
              <p className="mb-1 text-status-error">错误</p>
              <pre className="overflow-auto rounded-lg bg-status-error/10 p-2 font-mono text-status-error">
                {trace.error}
              </pre>
            </div>
          )}
          {trace.guardrail_id && (
            <p className="text-status-warning">护栏 {trace.guardrail_id} 拦截</p>
          )}
        </div>
      )}
    </div>
  )
}

export function RunDetailPanel({ runId, onClose }: RunDetailPanelProps) {
  const { data: summary, isLoading } = useQuery({
    queryKey: ['analysis', 'run', runId],
    queryFn: () => getRunAnalysis(runId),
  })

  const { data: tracesResp } = useQuery({
    queryKey: ['analysis', 'run', runId, 'traces'],
    queryFn: () => getRunToolTraces(runId),
  })

  const traces = tracesResp?.tool_traces ?? []
  const status = normalizeStatus(summary?.status)

  return (
    <motion.div
      initial={{ x: 32, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 32, opacity: 0 }}
      className="flex h-full w-full flex-col overflow-hidden rounded-2xl glass-elevated"
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-border-soft px-4 py-3">
        <span
          className={cn(
            'flex h-8 w-8 items-center justify-center rounded-lg',
            'bg-accent-secondary/20 text-accent-secondary',
          )}
        >
          <Hash size={15} />
        </span>
        <div className="min-w-0">
          <p className="font-mono text-xs text-text-secondary">{runId.slice(0, 16)}</p>
          <p className="truncate text-[10px] text-text-muted">Run 详情</p>
        </div>
        <StatusBadge status={status} className="ml-2" />
        <Link
          to={`/replay/${encodeURIComponent(runId)}`}
          className="ml-auto flex items-center gap-1 rounded-lg bg-accent-primary/20 px-2.5 py-1 text-xs text-accent-primary hover:bg-accent-primary/30"
          title="在时间旅行调试器中打开"
        >
          <FlaskConical size={13} /> 调试
        </Link>
        <button
          onClick={onClose}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-text-muted hover:bg-surface-1 hover:text-text-primary"
          title="关闭"
        >
          <X size={15} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {isLoading && (
          <div className="space-y-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-10 animate-pulse rounded-lg bg-surface-1" />
            ))}
          </div>
        )}

        {summary && (
          <>
            <p className="mb-1 text-xs text-text-tertiary">意图</p>
            <p className="mb-3 text-sm text-text-primary">{summary.intent || '(无意图)'}</p>

            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <p className="flex items-center gap-1 text-[10px] text-text-tertiary">
                  <Hash size={10} /> 事件
                </p>
                <p className="font-display text-lg font-bold text-text-primary">
                  {summary.event_count}
                </p>
              </div>
              <div className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <p className="flex items-center gap-1 text-[10px] text-text-tertiary">
                  <Wrench size={10} /> 工具调用
                </p>
                <p className="font-display text-lg font-bold text-accent-primary">
                  {summary.tool_trace_count}
                </p>
              </div>
              <div className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <p className="flex items-center gap-1 text-[10px] text-text-tertiary">
                  <Coins size={10} /> Tokens
                </p>
                <p className="font-display text-lg font-bold text-text-primary">
                  {summary.total_tokens}
                </p>
              </div>
              <div className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <p className="flex items-center gap-1 text-[10px] text-text-tertiary">
                  <Clock size={10} /> 时长
                </p>
                <p className="font-display text-lg font-bold text-text-primary">
                  {formatDuration(summary.total_duration_ms)}
                </p>
              </div>
              <div className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <p className="flex items-center gap-1 text-[10px] text-text-tertiary">
                  <ShieldAlert size={10} /> 护栏
                </p>
                <p
                  className={cn(
                    'font-display text-lg font-bold',
                    summary.guardrail_event_count > 0
                      ? 'text-status-error'
                      : 'text-text-primary',
                  )}
                >
                  {summary.guardrail_event_count}
                </p>
              </div>
            </div>
          </>
        )}

        <div className="mt-4 mb-2 text-xs font-semibold text-text-secondary">
          工具追踪 ({traces.length})
        </div>
        <div className="space-y-1.5">
          {traces.length === 0 && (
            <p className="py-4 text-center text-xs text-text-muted">无工具调用</p>
          )}
          {traces.map((t) => (
            <TraceNode key={t.tool_call_id} trace={t} />
          ))}
        </div>
      </div>
    </motion.div>
  )
}