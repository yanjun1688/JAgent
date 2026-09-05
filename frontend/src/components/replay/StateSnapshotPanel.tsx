import {
  AlertTriangle,
  CheckCircle2,
  ListTree,
  ShieldAlert,
  Loader2,
  Wrench,
  Hourglass,
  History,
} from 'lucide-react'
import type { RunStateView } from '../../api/schema'
import { cn } from '../../design-system/utils/cn'
import { statusLabel, statusTone, toneClasses } from './statusColors'

export interface StateSnapshotPanelProps {
  state: RunStateView | null
  isLoading: boolean
  error: string | null
  selectedSeq: number | null
}

function SectionTitle({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
      <span className="text-accent-primary">{icon}</span>
      {children}
    </div>
  )
}

function StatusPill({ status }: { status: string }) {
  const tone = toneClasses(statusTone(status))
  return (
    <span className={cn('rounded-full px-2.5 py-0.5 text-xs font-medium', tone.badge)}>
      {statusLabel(status)}
    </span>
  )
}

export function StateSnapshotPanel({ state, isLoading, error, selectedSeq }: StateSnapshotPanelProps) {
  if (selectedSeq == null) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-text-muted">
        <History size={28} />
        <p className="text-sm">点击左侧时间线上的任意事件，重建该时刻的系统状态</p>
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

  if (!state) return null

  const tone = toneClasses(statusTone(state.status))

  return (
    <div className="space-y-4">
      {/* Status banner */}
      <div
        className={cn(
          'flex items-center gap-3 rounded-xl border px-4 py-3',
          state.status === 'failed'
            ? 'border-status-error/40 bg-status-error/10'
            : state.status === 'completed'
              ? 'border-status-success/40 bg-status-success/10'
              : 'border-border-soft bg-surface-1',
        )}
      >
        <span className={cn('flex h-9 w-9 items-center justify-center rounded-lg', tone.dot)}>
          {state.status === 'failed' ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-display text-base font-bold text-text-primary">
              {statusLabel(state.status)}
            </span>
            <StatusPill status={state.status} />
          </div>
          <p className="font-mono text-[11px] text-text-muted">
            seq #{state.at_seq} / {state.latest_seq}
            {!state.is_latest && ' · 历史时刻（非最新）'}
          </p>
        </div>
      </div>

      {state.last_error && (
        <div className="rounded-lg bg-status-error/10 px-3 py-2">
          <p className="mb-1 flex items-center gap-1 text-[11px] font-semibold text-status-error">
            <AlertTriangle size={12} /> 错误
          </p>
          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-status-error">
            {state.last_error}
          </pre>
        </div>
      )}

      {/* Plan + steps */}
      <div>
        <SectionTitle icon={<ListTree size={14} />}>
          计划与步骤 {state.plan?.status ? `· ${statusLabel(state.plan.status)}` : ''}
        </SectionTitle>
        {!state.plan ? (
          <p className="rounded-lg border border-border-softer bg-surface-0 px-3 py-3 text-xs text-text-muted">
            该时刻尚未生成计划
          </p>
        ) : (
          <div className="space-y-1.5">
            {state.plan.steps?.map((step) => {
              const stepTone = toneClasses(statusTone(step.status))
              return (
                <div key={step.step_id} className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs text-text-primary">{step.step_id}</span>
                    {step.tool_name && <span className="text-[11px] text-text-muted">{step.tool_name}</span>}
                    <span className={cn('ml-auto rounded-full px-2 py-0.5 text-[10px]', stepTone.badge)}>
                      {statusLabel(step.status)}
                    </span>
                  </div>
                  {step.output_summary && (
                    <p className="mt-1 truncate text-[11px] text-text-secondary">{step.output_summary}</p>
                  )}
                  {step.error && <p className="mt-1 text-[11px] text-status-error">{step.error}</p>}
                  {step.reason && <p className="mt-1 text-[11px] text-status-warning">{step.reason}</p>}
                </div>
              )
            })}
            {state.plan.final_error && (
              <p className="rounded-lg bg-status-error/10 px-3 py-2 font-mono text-xs text-status-error">
                {state.plan.final_error}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Guardrail blocks */}
      {state.guardrail_blocks && state.guardrail_blocks.length > 0 && (
        <div>
          <SectionTitle icon={<ShieldAlert size={14} />}>护栏拦截（{state.guardrail_blocks.length}）</SectionTitle>
          <div className="space-y-1.5">
            {state.guardrail_blocks.map((g, i) => (
              <div key={i} className="rounded-lg border border-status-warning/30 bg-status-warning/10 px-3 py-2">
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

      {/* Tool results */}
      <div>
        <SectionTitle icon={<Wrench size={14} />}>工具结果（{state.tool_results?.length ?? 0}）</SectionTitle>
        {(!state.tool_results || state.tool_results.length === 0) && (
          <p className="rounded-lg border border-border-softer bg-surface-0 px-3 py-3 text-xs text-text-muted">
            该时刻暂无工具结果
          </p>
        )}
        <div className="space-y-1.5">
          {state.tool_results?.map((t) => {
            const tTone = toneClasses(statusTone(t.status))
            return (
              <div key={t.tool_call_id} className="rounded-lg border border-border-softer bg-surface-0 px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-xs text-text-primary">{t.tool_name}</span>
                  <span className={cn('ml-auto rounded-full px-2 py-0.5 text-[10px]', tTone.badge)}>
                    {statusLabel(t.status)}
                  </span>
                  <span className="font-mono text-[10px] text-text-muted">#{t.event_seq}</span>
                </div>
                {t.error && <p className="mt-1 text-[11px] text-status-error">{t.error}</p>}
              </div>
            )
          })}
        </div>
      </div>

      {/* Pending confirmations */}
      {state.pending_confirmations && state.pending_confirmations.length > 0 && (
        <div>
          <SectionTitle icon={<Hourglass size={14} />}>
            待确认（{state.pending_confirmations.length}）
          </SectionTitle>
          <div className="space-y-1.5">
            {state.pending_confirmations.map((c) => (
              <div key={c.confirmation_id} className="rounded-lg border border-status-warning/30 bg-surface-0 px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-text-primary">{c.tool_name}</span>
                  <span className="ml-auto rounded-full bg-status-warning/15 px-2 py-0.5 text-[10px] text-status-warning">
                    风险 {c.risk_level}
                  </span>
                </div>
                <p className="mt-1 font-mono text-[10px] text-text-muted">{c.confirmation_id}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
