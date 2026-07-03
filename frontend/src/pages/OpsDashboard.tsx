import React, { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import {
  queryDashboard,
  queryRuns,
  querySchedulers,
  queryToolStats,
  queryMcp,
  queryGuardrailStats,
} from '../api/ops-client'
import type {
  DashboardOverview,
  RunsItem,
  SchedulerEntry,
  ToolStat,
  McpServer,
  GuardrailStat,
} from '../api/ops-client'
import {
  card,
  colors,
  fmt,
  formatTime,
  statusBadge,
  sectionTitle,
  table,
  th,
  td,
  badge,
} from '../api/analysis-styles'

const POLL_INTERVAL_MS = 2000

export default function OpsDashboard() {
  const navigate = useNavigate()

  const [dashboardData, setDashboardData] = useState<DashboardOverview | null>(null)
  const [runs, setRuns] = useState<RunsItem[]>([])
  const [schedulers, setSchedulers] = useState<SchedulerEntry[]>([])
  const [toolStats, setToolStats] = useState<ToolStat[]>([])
  const [mcpServers, setMcpServers] = useState<McpServer[]>([])
  const [mcpConnectedCount, setMcpConnectedCount] = useState(0)
  const [guardrailStats, setGuardrailStats] = useState<GuardrailStat[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [backendDown, setBackendDown] = useState(false)
  const failCountRef = useRef(0)

  const loadAll = async () => {
    try {
      const [dashRes, runsRes, schedRes, toolsRes, mcpRes, grRes] = await Promise.allSettled([
        queryDashboard(),
        queryRuns(1, 10),
        querySchedulers(),
        queryToolStats(),
        queryMcp(),
        queryGuardrailStats(),
      ])

      if (dashRes.status === 'fulfilled') setDashboardData(dashRes.value.data)
      if (runsRes.status === 'fulfilled') setRuns(runsRes.value.data)
      if (schedRes.status === 'fulfilled') setSchedulers(schedRes.value.data)
      if (toolsRes.status === 'fulfilled') setToolStats(toolsRes.value.data.tools)
      if (mcpRes.status === 'fulfilled') {
        setMcpServers(mcpRes.value.data.servers)
        setMcpConnectedCount(mcpRes.value.data.connected_count)
      }
      if (grRes.status === 'fulfilled') setGuardrailStats(grRes.value.data.guardrails)
      setError(null)
    } catch {
      setError('Failed to load dashboard data')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAll()
    const interval = setInterval(() => {
      if (backendDown) return
      Promise.allSettled([queryDashboard(), querySchedulers()]).then(
        ([dashRes, schedRes]) => {
          const bothFailed = dashRes.status === 'rejected' && schedRes.status === 'rejected'
          if (bothFailed) {
            failCountRef.current++
            if (failCountRef.current >= 3) setBackendDown(true)
          } else {
            failCountRef.current = 0
            setBackendDown(false)
            setError(null)
          }
          if (dashRes.status === 'fulfilled') setDashboardData(dashRes.value.data)
          if (schedRes.status === 'fulfilled') setSchedulers(schedRes.value.data)
        },
      )
    }, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [backendDown])

  const kpiCards: { label: string; value: string; color: string }[] = [
    { label: 'Total Runs', value: dashboardData ? fmt(dashboardData.total_runs) : '-', color: colors.text },
    { label: 'Running', value: dashboardData ? fmt(dashboardData.running_runs) : '-', color: colors.blue },
    { label: 'Completed', value: dashboardData ? fmt(dashboardData.completed_runs) : '-', color: colors.success },
    { label: 'Failed', value: dashboardData ? fmt(dashboardData.failed_runs) : '-', color: colors.red },
    { label: 'Tool Calls', value: dashboardData ? fmt(dashboardData.total_tool_calls) : '-', color: colors.primary },
    { label: 'Guardrails', value: dashboardData ? fmt(dashboardData.total_guardrail_triggers) : '-', color: '#ff7043' },
  ]

  if (loading) {
    return <p style={{ color: colors.textSecondary, fontSize: 13 }}>Loading ops dashboard...</p>
  }

  if (error) {
    return <p style={{ color: colors.red }}>{error}</p>
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700 }}>Ops Dashboard</h1>
        <button
          onClick={() => navigate('/ops/chat')}
          style={{
            padding: '8px 18px',
            background: colors.primary,
            color: '#fff',
            border: 'none',
            borderRadius: 8,
            cursor: 'pointer',
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          Chat + Ops
        </button>
      </div>

      {backendDown && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 12,
          padding: '10px 16px', marginBottom: 16,
          background: colors.orangeLight, borderRadius: 8,
          border: `1px solid ${colors.orange}`, fontSize: 13,
        }}>
          <span style={{ color: colors.orange, fontWeight: 600 }}>Backend unavailable — showing cached data</span>
          <button
            onClick={() => { setBackendDown(false); failCountRef.current = 0; loadAll() }}
            style={{
              padding: '4px 14px', background: colors.orange, color: '#fff',
              border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600,
            }}
          >
            Retry
          </button>
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
        {kpiCards.map((kpi) => (
          <div key={kpi.label} style={{ ...card, padding: '14px 18px', flex: '1 1 120px', minWidth: 110 }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: kpi.color }}>{kpi.value}</div>
            <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px' }}>
              {kpi.label}
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 24 }}>
        <div style={{ ...card, flex: '1 1 560px', minWidth: 360 }}>
          <div style={sectionTitle}>Active Schedulers</div>
          {schedulers.length === 0 ? (
            <p style={{ color: colors.textSecondary, fontSize: 13 }}>No active schedulers</p>
          ) : (
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Run ID</th>
                  <th style={th}>Status</th>
                  <th style={th}>Intent</th>
                  <th style={th}>Events</th>
                  <th style={th}>Tools</th>
                  <th style={th}>Error</th>
                </tr>
              </thead>
              <tbody>
                {schedulers.map((s) => (
                  <tr key={s.run_id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/ops/runs/${s.run_id}`)}>
                    <td style={{ ...td, color: colors.blue, fontWeight: 600 }}>{s.run_id}</td>
                    <td style={td}>
                      <span style={statusBadge(s.status)}>{s.status}</span>
                    </td>
                    <td style={{ ...td, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {s.intent}
                    </td>
                    <td style={td}>{fmt(s.event_count)}</td>
                    <td style={td}>
                      {Object.keys(s.tool_stats || {}).join(', ') || '-'}
                    </td>
                    <td style={{ ...td, color: colors.red, maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {s.last_error || '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div style={{ ...card, flex: '1 1 300px', minWidth: 260 }}>
          <div style={sectionTitle}>Tool Call Leaderboard</div>
          {toolStats.length === 0 ? (
            <p style={{ color: colors.textSecondary, fontSize: 13 }}>No tool data available</p>
          ) : (
            <>
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={toolStats.slice(0, 10)} layout="vertical" margin={{ top: 0, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={colors.border} />
                  <XAxis type="number" tick={{ fontSize: 10 }} />
                  <YAxis type="category" dataKey="tool_name" width={80} tick={{ fontSize: 10 }} />
                  <Tooltip
                    contentStyle={{ fontSize: 12, border: `1px solid ${colors.border}`, borderRadius: 8 }}
                    formatter={(v: unknown) => [fmt(Number(v) || 0), '']}
                  />
                  <Bar dataKey="call_count" fill={colors.primary} radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
              <table style={{ ...table, marginTop: 8 }}>
                <thead>
                  <tr>
                    <th style={th}>Tool</th>
                    <th style={th}>Calls</th>
                    <th style={th}>Success</th>
                    <th style={th}>Fail</th>
                  </tr>
                </thead>
                <tbody>
                  {toolStats.slice(0, 10).map((t) => (
                    <tr key={t.tool_name}>
                      <td style={td}>{t.tool_name}</td>
                      <td style={td}>{fmt(t.call_count)}</td>
                      <td style={{ ...td, color: colors.success }}>{fmt(t.success_count)}</td>
                      <td style={{ ...td, color: t.failure_count > 0 ? colors.red : colors.textSecondary }}>{fmt(t.failure_count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>

      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 24 }}>
        <div style={{ ...card, flex: '1 1 400px', minWidth: 320 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <div style={sectionTitle}>Recent Runs</div>
            <button
              onClick={() => navigate('/')}
              style={{
                background: 'none', border: 'none', color: colors.blue, cursor: 'pointer', fontSize: 12, fontWeight: 600,
              }}
            >
              View All
            </button>
          </div>
          {runs.length === 0 ? (
            <p style={{ color: colors.textSecondary, fontSize: 13 }}>No runs yet</p>
          ) : (
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Run ID</th>
                  <th style={th}>Status</th>
                  <th style={th}>Intent</th>
                  <th style={th}>Tools</th>
                  <th style={th}>Time</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/ops/runs/${r.run_id}`)}>
                    <td style={{ ...td, color: colors.blue, fontWeight: 600 }}>{r.run_id}</td>
                    <td style={td}>
                      <span style={statusBadge(r.status)}>{r.status}</span>
                    </td>
                    <td style={{ ...td, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {r.intent}
                    </td>
                    <td style={td}>{fmt(r.tool_call_count)}</td>
                    <td style={{ ...td, fontSize: 12, color: colors.textSecondary }}>{formatTime(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div style={{ ...card, flex: '1 1 300px', minWidth: 260 }}>
          <div style={sectionTitle}>MCP Server Status</div>
          {mcpServers.length === 0 ? (
            <p style={{ color: colors.textSecondary, fontSize: 13 }}>No MCP servers registered</p>
          ) : (
            <div>
              <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
                <div style={{ ...card, padding: '10px 14px', flex: 1 }}>
                  <div style={{ fontSize: 20, fontWeight: 700, color: colors.primary }}>{fmt(mcpConnectedCount)}</div>
                  <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Connected</div>
                </div>
                <div style={{ ...card, padding: '10px 14px', flex: 1 }}>
                  <div style={{ fontSize: 20, fontWeight: 700, color: colors.red }}>{fmt(mcpServers.length - mcpConnectedCount)}</div>
                  <div style={{ fontSize: 10, color: colors.textSecondary, textTransform: 'uppercase', fontWeight: 600 }}>Disconnected</div>
                </div>
              </div>
              <table style={table}>
                <thead>
                  <tr>
                    <th style={th}>Server</th>
                    <th style={th}>Status</th>
                    <th style={th}>URL</th>
                  </tr>
                </thead>
                <tbody>
                  {mcpServers.map((s) => (
                    <tr key={s.name}>
                      <td style={td}>{s.name}</td>
                      <td style={td}>
                        <span style={badge(s.connected ? colors.success : colors.red)}>
                          {s.connected ? 'connected' : 'disconnected'}
                        </span>
                      </td>
                      <td style={{ ...td, fontSize: 12, maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {s.url || '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div style={{ ...card, marginBottom: 24 }}>
        <div style={sectionTitle}>Guardrail Triggers</div>
        {guardrailStats.length === 0 ? (
          <p style={{ color: colors.textSecondary, fontSize: 13 }}>No guardrail triggers</p>
        ) : (
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Guardrail</th>
                <th style={th}>Triggers</th>
                <th style={th}>Tools Affected</th>
                <th style={th}>Recent Reason</th>
              </tr>
            </thead>
            <tbody>
              {guardrailStats.map((g) => (
                <tr key={g.guardrail_id}>
                  <td style={{ ...td, fontWeight: 600 }}>{g.guardrail_id}</td>
                  <td style={{ ...td, color: g.trigger_count > 0 ? colors.red : colors.textSecondary }}>
                    {fmt(g.trigger_count)}
                  </td>
                  <td style={td}>{g.tools_affected.join(', ') || '-'}</td>
                  <td style={{ ...td, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {g.recent_reason || '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
