import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getGuardrailStats } from '../api/analysis-client'
import type { GuardrailStatItem } from '../api/analysis-types'
import {
  card, sectionTitle, table, th, td, colors, fmt, badge,
} from '../api/analysis-styles'

const GUARDRAIL_COLORS: Record<string, string> = {
  destructive_op: '#ef5350',
  rate_limit: '#ff7043',
  schema: '#ffa726',
  scope: '#26a69a',
  dependency: '#78909c',
}

export default function GuardrailPanel() {
  const [guardrails, setGuardrails] = useState<GuardrailStatItem[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    getGuardrailStats()
      .then((res) => setGuardrails(res.guardrails))
      .catch(() => setGuardrails([]))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <Link to="/analysis" style={{ color: colors.blue, textDecoration: 'none', fontSize: 14 }}>← Dashboard</Link>
      </div>

      <h1 style={{ margin: '0 0 4px', fontSize: 24, fontWeight: 700 }}>Guardrail Triggers</h1>
      <p style={{ color: colors.textSecondary, fontSize: 14, margin: '0 0 24px' }}>
        All guardrail interception events across all runs
      </p>

      {loading ? (
        <p style={{ color: colors.textSecondary }}>Loading guardrail statistics...</p>
      ) : guardrails.length === 0 ? (
        <p style={{ color: colors.textSecondary }}>No guardrail triggers recorded.</p>
      ) : (
        <div style={card}>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Guardrail</th>
                <th style={th}>Triggers</th>
                <th style={th}>Affected Tools</th>
                <th style={th}>Recent Reason</th>
              </tr>
            </thead>
            <tbody>
              {guardrails.map((g) => (
                <tr key={g.guardrail_id} style={{ borderBottom: `1px solid ${colors.border}` }}>
                  <td style={td}>
                    <span
                      style={{
                        ...badge(GUARDRAIL_COLORS[g.guardrail_id] || '#78909c'),
                        fontFamily: 'monospace',
                        fontSize: 11,
                      }}
                    >
                      {g.guardrail_id}
                    </span>
                  </td>
                  <td style={{ ...td, fontSize: 18, fontWeight: 700, color: colors.red }}>
                    {fmt(g.trigger_count)}
                  </td>
                  <td style={td}>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {g.tools_affected.map((t) => (
                        <span key={t} style={{ ...badge('#eee', '#555'), fontSize: 11 }}>
                          {t}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td style={{ ...td, maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis', color: '#555' }}>
                    {g.recent_reason || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
