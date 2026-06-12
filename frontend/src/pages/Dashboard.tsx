import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts'
import { getDashboard } from '../api/analysis-client'
import type { DashboardOverview } from '../api/analysis-types'
import {
  card, colors, fmt, pct,
} from '../api/analysis-styles'
import ChatDrawer from '../components/ChatDrawer'

const drawerWidth = 480

function DonutChart({ data, colors: palette, centerText }: {
  data: { name: string; value: number }[]
  colors: string[]
  centerText: string
}) {
  return (
    <ResponsiveContainer width="100%" height={180}>
      <PieChart>
        <Pie
          data={data.filter(d => d.value > 0)}
          cx="50%"
          cy="50%"
          innerRadius={48}
          outerRadius={72}
          paddingAngle={2}
          dataKey="value"
        >
          {data.filter(d => d.value > 0).map((_, i) => (
            <Cell key={i} fill={palette[i % palette.length]} stroke="transparent" />
          ))}
        </Pie>
        <text x="50%" y="46%" textAnchor="middle" dominantBaseline="middle" style={{ fontSize: 14, fontWeight: 700, fill: '#1a1a2e' }}>
          {centerText}
        </text>
        <Tooltip formatter={(v: unknown) => [fmt(Number(v) || 0), '']} />
        <Legend
          verticalAlign="bottom"
          height={24}
          iconSize={8}
          formatter={(value: string) => <span style={{ fontSize: 11, color: '#555' }}>{value}</span>}
        />
      </PieChart>
    </ResponsiveContainer>
  )
}

export default function Dashboard() {
  const navigate = useNavigate()
  const [data, setData] = useState<DashboardOverview | null>(null)
  const [timeWindow, setTimeWindow] = useState<'24h' | '7d' | 'all'>('24h')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    const until = Date.now() / 1000
    let since: number | undefined
    if (timeWindow === '24h') since = until - 86400
    else if (timeWindow === '7d') since = until - 86400 * 7
    else since = 0
    getDashboard(since, until)
      .then((res) => setData(res.overview))
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [timeWindow])

  const runStatusData = data ? [
    { name: 'Completed', value: data.completed_runs },
    { name: 'Running', value: data.running_runs },
    { name: 'Failed', value: data.failed_runs },
    { name: 'Paused', value: data.paused_runs },
  ] : []

  const toolOutcomeData = data ? [
    { name: 'Success', value: Math.max(0, data.total_tool_calls - data.total_tool_failures) },
    { name: 'Failed', value: data.total_tool_failures },
  ] : []

  const chartColors1 = ['#66bb6a', '#4fc3f7', '#ef5350', '#ffb74d']
  const chartColors2 = ['#66bb6a', '#ef5350']

  return (
    <div style={{ display: 'flex', gap: 20, height: 'calc(100vh - 60px)' }}>
      <div style={{ flex: 1, minWidth: 0, overflow: 'auto', paddingBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700 }}>Dashboard</h1>
          <div style={{ display: 'flex', gap: 4, background: colors.bg, borderRadius: 6, padding: 2 }}>
            {(['24h', '7d', 'all'] as const).map((w) => (
              <button
                key={w}
                onClick={() => setTimeWindow(w)}
                style={{
                  padding: '4px 12px',
                  border: 'none',
                  borderRadius: 4,
                  cursor: 'pointer',
                  fontSize: 12,
                  fontWeight: 600,
                  background: timeWindow === w ? colors.card : 'transparent',
                  color: timeWindow === w ? colors.text : colors.textSecondary,
                  boxShadow: timeWindow === w ? '0 1px 2px rgba(0,0,0,0.08)' : 'none',
                }}
              >
                {w === '24h' ? '24h' : w === '7d' ? '7d' : 'All'}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <p style={{ color: colors.textSecondary, fontSize: 13 }}>Loading...</p>
        ) : data ? (
          <>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
              <div style={{ ...card, padding: '12px 14px', flex: '1 1 100px', minWidth: 90 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: colors.text }}>{fmt(data.total_runs)}</div>
                <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Runs</div>
              </div>
              <div style={{ ...card, padding: '12px 14px', flex: '1 1 100px', minWidth: 90 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: '#4fc3f7' }}>{fmt(data.running_runs)}</div>
                <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Active</div>
              </div>
              <div style={{ ...card, padding: '12px 14px', flex: '1 1 100px', minWidth: 90 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: '#66bb6a' }}>{fmt(data.completed_runs)}</div>
                <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Done</div>
              </div>
              <div style={{ ...card, padding: '12px 14px', flex: '1 1 100px', minWidth: 90 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: '#ef5350' }}>{fmt(data.failed_runs)}</div>
                <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Failed</div>
              </div>
              <div style={{ ...card, padding: '12px 14px', flex: '1 1 100px', minWidth: 90 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: '#3ECF8E' }}>{fmt(data.total_tool_calls)}</div>
                <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Tools</div>
              </div>
              <div style={{ ...card, padding: '12px 14px', flex: '1 1 100px', minWidth: 90 }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: '#ce93d8' }}>{fmt(data.total_tokens_consumed)}</div>
                <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Tokens</div>
              </div>
            </div>

            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 16 }}>
              <div style={{ ...card, flex: '1 1 280px', minWidth: 240, padding: '14px 16px' }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Run Status</div>
                <DonutChart data={runStatusData} colors={chartColors1} centerText={fmt(data.total_runs)} />
              </div>
              <div style={{ ...card, flex: '1 1 280px', minWidth: 240, padding: '14px 16px' }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Tool Outcome</div>
                <DonutChart data={toolOutcomeData} colors={chartColors2} centerText={pct(data.avg_tool_success_rate)} />
              </div>
              <div style={{ ...card, flex: '1 1 200px', minWidth: 180, padding: '14px 16px' }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Quick Nav</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div onClick={() => navigate('/analysis/tools')} style={{ padding: '8px 12px', borderRadius: 6, background: colors.bg, cursor: 'pointer', fontSize: 12, border: `1px solid ${colors.border}` }}>
                    <span style={{ fontWeight: 600 }}>Tool Usage</span>
                    <span style={{ color: colors.textSecondary, marginLeft: 6 }}>{fmt(data.total_tool_calls)} calls</span>
                  </div>
                  <div onClick={() => navigate('/analysis/guardrails')} style={{ padding: '8px 12px', borderRadius: 6, background: colors.bg, cursor: 'pointer', fontSize: 12, border: `1px solid ${colors.border}` }}>
                    <span style={{ fontWeight: 600 }}>Guardrails</span>
                    <span style={{ color: colors.textSecondary, marginLeft: 6 }}>{fmt(data.total_guardrail_triggers)} triggers</span>
                  </div>
                  <div onClick={() => navigate('/')} style={{ padding: '8px 12px', borderRadius: 6, background: colors.bg, cursor: 'pointer', fontSize: 12, border: `1px solid ${colors.border}` }}>
                    <span style={{ fontWeight: 600 }}>All Runs</span>
                    <span style={{ color: colors.textSecondary, marginLeft: 6 }}>{fmt(data.total_runs)} total</span>
                  </div>
                </div>
              </div>
            </div>
          </>
        ) : (
          <p style={{ color: colors.textSecondary }}>No data.</p>
        )}
      </div>

      <div style={{ width: drawerWidth, flexShrink: 0 }}>
        <ChatDrawer />
      </div>
    </div>
  )
}
