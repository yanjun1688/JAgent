import { useEffect, useState } from 'react'
import { api, type SessionMetrics } from '../lib/api'

interface MetricStripProps {
  sessionId: string
}

function compactNumber(value: number): string {
  if (value >= 100000) return `${Math.round(value / 1000)}k`
  if (value >= 10000) return `${(value / 1000).toFixed(1)}k`
  return String(value)
}

function rateLabel(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`
}

function Cell({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <span className="hidden items-baseline gap-1 font-mono text-[10px] tabular-nums lg:flex">
      <span className="text-text-tertiary">{label}</span>
      <span className={accent ? 'text-neon-cyan' : 'text-text-secondary'}>{value}</span>
    </span>
  )
}

/**
 * D-031 S2 metrics, one thin strip: cost / efficiency / quality at a glance.
 * Pure projection — a failed read must never break the header.
 */
export function MetricStrip({ sessionId }: MetricStripProps) {
  const [metrics, setMetrics] = useState<SessionMetrics | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const data = await api.sessionMetrics(sessionId)
        if (!cancelled) setMetrics(data)
      } catch {
        // metrics are decoration; the conversation continues without them
      }
      if (!cancelled) timer = window.setTimeout(tick, 10_000)
    }

    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [sessionId])

  if (!metrics) {
    return (
      <span className="hidden font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary lg:inline">
        METRICS …
      </span>
    )
  }

  const tokens = metrics.tokens_in + metrics.tokens_out

  return (
    <span
      className="hidden items-center gap-2.5 border border-night-500 bg-night-700 px-2.5 py-1 xl:flex"
      title="会话指标（D-031 S2 纯派生）：轮次 / 任务 / 完成率 / 模型调用 / tokens / 修复"
    >
      <Cell label="T" value={String(metrics.turns)} />
      <Cell label="RUN" value={String(metrics.runs)} />
      <Cell label="完成" value={rateLabel(metrics.completion_rate)} accent />
      <Cell label="MODEL" value={String(metrics.model_calls)} />
      <Cell label="TOK" value={compactNumber(tokens)} />
      {metrics.repairs > 0 ? <Cell label="修复" value={compactNumber(metrics.repairs)} /> : null}
    </span>
  )
}
