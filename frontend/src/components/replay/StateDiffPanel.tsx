import { AlertTriangle, ArrowRight, GitCompare, ListTree, Loader2, ShieldAlert, Wrench } from 'lucide-react'
import type { StateDiff } from '../../api/schema'
import { cn } from '../../design-system/utils/cn'
import { statusLabel, statusTone, toneClasses } from './statusColors'

export interface StateDiffPanelProps {
  diff: StateDiff | null
  isLoading: boolean
  error: string | null
  hasSelection: boolean
}

function DiffStatusPill({ status }: { status: string }) {
  const tone = toneClasses(statusTone(status))
  return <span className={cn('rounded-full px-2.5 py-0.5 text-xs font-medium', tone.badge)}>{statusLabel(status)}</span>
}

export function StateDiffPanel({ diff, isLoading, error, hasSelection }: StateDiffPanelProps) {
  if (!hasSelection) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-text-muted">
        <GitCompare size={28} />
        <p className="px-6 text-center text-sm">
          对比模式：在左侧时间线上依次点击 <span className="font-bold text-status-info">起点 A</span> 与{' '}
          <span className="font-bold text-status-success">终点 B</span>，查看两者间的状态差异
        </p>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-accent-primary">
        <Loader2 size={26} className="animate-spin" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <AlertTriangle size={26} className="text-status-error" />
        <p className="text-sm text-status-error">{error}</p>
      </div>
    )
  }

  if (!diff) return null

  return (
    <div className="space-y-4">
      <p className="font-mono text-[11px] text-text-muted">
        seq #{diff.from_seq} → #{diff.to_seq} · 区间内 {diff.events_in_range?.length ?? 0} 个事件
      </p>

      {/* Run status transition — prominent */}
      {diff.status_change ? (
        <div
          className={cn(
            'flex items-center gap-3 rounded-xl border px-4 py-3',
            diff.status_change.to_status === 'failed'
              ? 'border-status-error/40 bg-status-error/10'
              : 'border-accent-primary/40 bg-accent-primary/10',
          )}
          data-testid="diff-status-change"
        >
          <span className="text-xs text-text-tertiary">运行状态</span>
          <DiffStatusPill status={diff.status_change.from_status} />
          <ArrowRight size={16} className="text-text-muted" />
          <DiffStatusPill status={diff.status_change.to_status} />
        </div>
      ) : (
        <div className="rounded-xl border border-border-soft bg-surface-1 px-4 py-3 text-sm text-text-secondary">
          区间内运行状态未发生变化
        </div>
      )}

      {/* Error appearance */}
      {diff.error_change?.to_error && (
        <div className="rounded-lg bg-status-error/10 px-3 py-2">
          <p className="mb-1 flex items-center gap-1 text-[11px] font-semibold text-status-error">
            <AlertTriangle size={12} /> 出现错误
          </p>
          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-status-error">
            {diff.error_change.to_error}
          </pre>
        </div>
      )}

      {/* Step changes — prominent */}
      <div>
        <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
          <ListTree size={14} className="text-accent-primary" /> 步骤状态变化（{diff.steps_changed?.length ?? 0}）
        </div>
        {(!diff.steps_changed || diff.steps_changed.length === 0) && (
          <p className="rounded-lg border border-border-softer bg-surface-0 px-3 py-3 text-xs text-text-muted">
            区间内没有步骤状态发生变化
          </p>
        )}
        <div className="space-y-1.5">
          {diff.steps_changed?.map((c) => (
            <div
              key={c.step_id}
              className={cn(
                'flex items-center gap-2 rounded-lg border px-3 py-2',
                c.to_status === 'failed'
                  ? 'border-status-error/40 bg-status-error/10'
                  : 'border-border-softer bg-surface-0',
              )}
              data-testid="diff-step-change"
            >
              <span className="font-mono text-xs text-text-primary">{c.step_id}</span>
              <span className="ml-auto flex items-center gap-2">
                {c.from_status && <DiffStatusPill status={c.from_status} />}
                {!c.from_status && <span className="text-[10px] text-text-muted">新增</span>}
                <ArrowRight size={13} className="text-text-muted" />
                <DiffStatusPill status={c.to_status ?? 'unknown'} />
              </span>
              {c.error && <p className="w-full text-[11px] text-status-error">{c.error}</p>}
            </div>
          ))}
        </div>
      </div>

      {/* Guardrails fired */}
      {diff.guardrails_triggered && diff.guardrails_triggered.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
            <ShieldAlert size={14} className="text-accent-primary" /> 区间内护栏拦截（
            {diff.guardrails_triggered.length}）
          </div>
          <div className="space-y-1.5">
            {diff.guardrails_triggered.map((g, i) => (
              <div
                key={i}
                className="rounded-lg border border-status-warning/30 bg-status-warning/10 px-3 py-2"
              >
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-status-warning">{g.guardrail_id}</span>
                  <span className="ml-auto font-mono text-[10px] text-text-muted">#{g.event_seq}</span>
                </div>
                <p className="mt-1 text-[11px] text-text-secondary">{g.reason}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Tool results added */}
      {diff.tool_results_added && diff.tool_results_added.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
            <Wrench size={14} className="text-accent-primary" /> 区间内工具结果（
            {diff.tool_results_added.length}）
          </div>
          <div className="space-y-1.5">
            {diff.tool_results_added.map((t) => (
              <div key={t.tool_call_id} className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-xs text-text-primary">{t.tool_name}</span>
                  <span
                    className={cn(
                      'ml-auto rounded-full px-2 py-0.5 text-[10px]',
                      toneClasses(statusTone(t.status)).badge,
                    )}
                  >
                    {statusLabel(t.status)}
                  </span>
                  <span className="font-mono text-[10px] text-text-muted">#{t.event_seq}</span>
                </div>
                {t.error && <p className="mt-1 text-[11px] text-status-error">{t.error}</p>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
