import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getToolStats } from '../api/analysis-client'
import type { ToolStatItem } from '../api/analysis-types'
import {
  card, sectionTitle, table, th, td, colors, fmt, formatDuration, badge,
  formatTime,
} from '../api/analysis-styles'

export default function ToolsPanel() {
  const [tools, setTools] = useState<ToolStatItem[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    getToolStats()
      .then((res) => setTools(res.tools))
      .finally(() => setLoading(false))
  }, [])

  const failureLabel = (t: ToolStatItem): React.ReactNode => {
    const total = t.failure_count + t.timeout_count + t.guardrail_blocked_count
    if (total === 0) return <span style={{ color: colors.success }}>0</span>
    return (
      <span style={{ color: colors.red }}>
        {fmt(total)}
        <span style={{ fontSize: 11, color: colors.textSecondary, marginLeft: 4 }}>
          ({t.failure_count}f / {t.timeout_count}t / {t.guardrail_blocked_count}g)
        </span>
      </span>
    )
  }

  const rateBar = (success: number, total: number): React.ReactNode => {
    if (total === 0) return <span style={{ color: '#ccc' }}>—</span>
    const p = success / total
    const barW = Math.max(p * 100, 4)
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ flex: 1, height: 6, background: colors.grayLight, borderRadius: 3, overflow: 'hidden' }}>
          <div style={{ width: `${barW}%`, height: '100%', background: p > 0.8 ? colors.success : p > 0.5 ? colors.orange : colors.red, borderRadius: 3, transition: 'width 0.3s' }} />
        </div>
        <span style={{ fontSize: 12, fontWeight: 600, color: p > 0.8 ? colors.success : p > 0.5 ? colors.orange : colors.red, minWidth: 36, textAlign: 'right' }}>
          {Math.round(p * 100)}%
        </span>
      </div>
    )
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <Link to="/analysis" style={{ color: colors.blue, textDecoration: 'none', fontSize: 14 }}>← Dashboard</Link>
      </div>

      <h1 style={{ margin: '0 0 4px', fontSize: 24, fontWeight: 700 }}>Tool Usage</h1>
      <p style={{ color: colors.textSecondary, fontSize: 14, margin: '0 0 24px' }}>
        Per-tool performance statistics across all runs
      </p>

      {loading ? (
        <p style={{ color: colors.textSecondary }}>Loading tool statistics...</p>
      ) : tools.length === 0 ? (
        <p style={{ color: colors.textSecondary }}>No tool data available.</p>
      ) : (
        <div style={card}>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Tool</th>
                <th style={th}>Calls</th>
                <th style={th}>Success Rate</th>
                <th style={th}>Failures</th>
                <th style={th}>Avg Duration</th>
              </tr>
            </thead>
            <tbody>
              {tools.map((t) => {
                const totalResults = t.success_count + t.failure_count + t.timeout_count
                return (
                  <tr key={t.tool_name} style={{ borderBottom: `1px solid ${colors.border}` }}>
                    <td style={td}>
                      <span style={{ fontWeight: 600 }}>{t.tool_name}</span>
                    </td>
                    <td style={td}>{fmt(t.call_count)}</td>
                    <td style={td}>{rateBar(t.success_count, totalResults)}</td>
                    <td style={td}>{failureLabel(t)}</td>
                    <td style={td}>{t.avg_duration_ms > 0 ? formatDuration(t.avg_duration_ms) : '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
