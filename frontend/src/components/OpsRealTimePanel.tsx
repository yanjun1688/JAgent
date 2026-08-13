import React, { useMemo } from 'react'
import type { WsEvent } from '../hooks/useRunWebSocket'
import {
  card,
  sectionTitle,
  colors,
  fmt,
  formatTime,
  formatDuration,
  statusBadge,
  eventTypeBadge,
  table,
  th,
  td,
} from '../api/analysis-styles'

interface Props {
  runId: string | null
  events: WsEvent[]
  runStatus: string
  isConnected: boolean
}

interface ToolTrace {
  tool_call_id: string
  tool_name: string
  status: string
  input: Record<string, unknown> | null
  output: unknown | null
  error: string | null
  duration_ms: number
  guardrail_id: string | null
  called_at: number
}

interface ToolStat {
  tool_name: string
  call_count: number
  completed: number
  unsuccessful: number
  failed: number
  timeout: number
  guardrail_blocked: number
}

interface PlanStep {
  step_id: string
  tool_name: string
  status: string
}

interface PlanInfo {
  plan_id: string
  intent: string
  steps_summary: string
  layer_count: number
  steps: PlanStep[]
  revision_reason: string | null
  remaining_steps_summary: string
}

const KEY_EVENT_TYPES = new Set([
  'ToolCalled', 'ToolCompleted', 'ToolFailed', 'ToolTimeout',
  'GuardrailTriggered', 'ConfirmationRequested', 'ConfirmationReceived',
  'PlanCreated', 'PlanRevised', 'PlanCompleted', 'PlanFailed',
  'RunPaused', 'RunResumed',
])

export default function OpsRealTimePanel({ runId, events, runStatus, isConnected }: Props) {
  const computed = useMemo(() => {
    const eventTypeCounts: Record<string, number> = {}
    const toolStatsMap: Record<string, ToolStat> = {}
    const toolCallMap = new Map<string, { tool_name: string; input: Record<string, unknown> | null; called_at: number }>()
    const traces: ToolTrace[] = []
    const planList: PlanInfo[] = []

    for (const e of events) {
      eventTypeCounts[e.event_type] = (eventTypeCounts[e.event_type] || 0) + 1

      if (e.event_type === 'ToolCalled' && e.tool_call_id) {
        const name = e.tool_name || 'unknown'
        if (!toolStatsMap[name]) {
          toolStatsMap[name] = { tool_name: name, call_count: 0, completed: 0, unsuccessful: 0, failed: 0, timeout: 0, guardrail_blocked: 0 }
        }
        toolStatsMap[name].call_count++
        toolCallMap.set(e.tool_call_id, { tool_name: name, input: e.input, called_at: e.created_at })
      }

      if (e.event_type === 'ToolCompleted' && e.tool_call_id) {
        const callInfo = toolCallMap.get(e.tool_call_id)
        const name = callInfo?.tool_name || e.tool_name || 'unknown'
        if (!toolStatsMap[name]) {
          toolStatsMap[name] = { tool_name: name, call_count: 0, completed: 0, unsuccessful: 0, failed: 0, timeout: 0, guardrail_blocked: 0 }
        }
        // v2.2 (D2/D3): result_type=unsuccessful（跑了没拿到）独立计数，不再算 completed。
        if (e.payload.result_type === 'unsuccessful') {
          toolStatsMap[name].unsuccessful++
        } else {
          toolStatsMap[name].completed++
        }
        const duration = e.duration_ms != null ? e.duration_ms : callInfo ? (e.created_at - callInfo.called_at) * 1000 : 0
        traces.push({
          tool_call_id: e.tool_call_id,
          tool_name: name,
          status: 'completed',
          input: callInfo?.input || null,
          output: e.payload.output ?? null,
          error: null,
          duration_ms: duration,
          guardrail_id: null,
          called_at: callInfo?.called_at || e.created_at,
        })
      }

      if (e.event_type === 'ToolFailed' && e.tool_call_id) {
        const callInfo = toolCallMap.get(e.tool_call_id)
        const name = callInfo?.tool_name || e.tool_name || 'unknown'
        if (!toolStatsMap[name]) {
          toolStatsMap[name] = { tool_name: name, call_count: 0, completed: 0, unsuccessful: 0, failed: 0, timeout: 0, guardrail_blocked: 0 }
        }
        toolStatsMap[name].failed++
        traces.push({
          tool_call_id: e.tool_call_id,
          tool_name: name,
          status: 'failed',
          input: callInfo?.input || null,
          output: null,
          error: e.error,
          duration_ms: callInfo ? (e.created_at - callInfo.called_at) * 1000 : 0,
          guardrail_id: null,
          called_at: callInfo?.called_at || e.created_at,
        })
      }

      if (e.event_type === 'ToolTimeout' && e.tool_call_id) {
        const callInfo = toolCallMap.get(e.tool_call_id)
        const name = callInfo?.tool_name || e.tool_name || 'unknown'
        if (!toolStatsMap[name]) {
          toolStatsMap[name] = { tool_name: name, call_count: 0, completed: 0, unsuccessful: 0, failed: 0, timeout: 0, guardrail_blocked: 0 }
        }
        toolStatsMap[name].timeout++
        traces.push({
          tool_call_id: e.tool_call_id,
          tool_name: name,
          status: 'timeout',
          input: callInfo?.input || null,
          output: null,
          error: String(e.payload.timeout_ms ? `Timeout after ${e.payload.timeout_ms}ms` : 'Timeout'),
          duration_ms: Number(e.payload.timeout_ms) || 0,
          guardrail_id: null,
          called_at: callInfo?.called_at || e.created_at,
        })
      }

      if (e.event_type === 'GuardrailTriggered') {
        const name = e.tool_name || (e.payload.tool_name as string) || 'unknown'
        if (!toolStatsMap[name]) {
          toolStatsMap[name] = { tool_name: name, call_count: 0, completed: 0, unsuccessful: 0, failed: 0, timeout: 0, guardrail_blocked: 0 }
        }
        toolStatsMap[name].guardrail_blocked++
        if (e.tool_call_id) {
          const callInfo = toolCallMap.get(e.tool_call_id)
          const existing = traces.find(t => t.tool_call_id === e.tool_call_id)
          if (existing) {
            existing.guardrail_id = (e.payload.guardrail_id as string) || null
          } else {
            traces.push({
              tool_call_id: e.tool_call_id,
              tool_name: name,
              status: 'guardrail_blocked',
              input: callInfo?.input || null,
              output: null,
              error: (e.payload.reason as string) || null,
              duration_ms: callInfo ? (e.created_at - callInfo.called_at) * 1000 : 0,
              guardrail_id: (e.payload.guardrail_id as string) || null,
              called_at: callInfo?.called_at || e.created_at,
            })
          }
        }
      }

      if (e.event_type === 'PlanCreated' || e.event_type === 'PlanRevised') {
        const p = e.payload
        const planData = (p.plan || p) as Record<string, unknown>
        const rawSteps = (planData.steps || []) as Array<Record<string, unknown>>
        const steps: PlanStep[] = rawSteps.map((s) => ({
          step_id: String(s.step_id || s.id || ''),
          tool_name: String(s.tool_name || s.tool || ''),
          status: String(s.status || 'pending'),
        }))
        planList.push({
          plan_id: String(planData.plan_id || ''),
          intent: String(planData.intent || planData.description || ''),
          steps_summary: String(planData.steps_summary || `${steps.length} steps`),
          layer_count: Number(planData.layer_count) || 1,
          steps,
          revision_reason: e.event_type === 'PlanRevised' ? String(p.revision_reason || null) : null,
          remaining_steps_summary: String(planData.remaining_steps_summary || ''),
        })
      }
    }

    const toolStats = Object.values(toolStatsMap)
    const latestPlan = planList.length > 0 ? planList[planList.length - 1] : null
    const thoughtCount = eventTypeCounts['AgentThought'] || 0
    const totalToolCalls = toolStats.reduce((s, t) => s + t.call_count, 0)

    return { eventTypeCounts, toolStats, traces: traces.reverse(), latestPlan, planList, thoughtCount, totalToolCalls }
  }, [events])

  const keyEvents = useMemo(
    () => events.filter((e) => KEY_EVENT_TYPES.has(e.event_type)),
    [events],
  )

  const eventSummary = (e: WsEvent): string => {
    if (e.tool_name) {
      const parts = [e.tool_name]
      if (e.event_type === 'ToolCompleted' && e.duration_ms != null) parts.push(`(${formatDuration(e.duration_ms)})`)
      if (e.error) parts.push(`ERROR: ${e.error}`)
      return parts.join(' ')
    }
    if (e.event_type === 'PlanCreated') return `Plan: ${JSON.stringify(e.payload).slice(0, 60)}`
    if (e.event_type === 'PlanRevised') return `Revised: ${e.payload.revision_reason || ''}`
    if (e.event_type === 'RunPaused') return `Paused: ${e.payload.reason || ''}`
    if (e.event_type === 'RunResumed') return 'Resumed'
    if (e.event_type === 'GuardrailTriggered') return `${e.payload.guardrail_id || 'guard'}: ${e.payload.reason || ''}`
    return ''
  }

  if (!runId) {
    return (
      <div style={{ ...card, flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <p style={{ color: colors.textSecondary, fontSize: 13 }}>No active run. Start a conversation to see ops data.</p>
      </div>
    )
  }

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, padding: '0 4px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontWeight: 700, fontSize: 14 }}>Run {runId.slice(0, 8)}</span>
          <span style={statusBadge(runStatus || 'running')}>{runStatus || 'running'}</span>
          <span style={{ fontSize: 11, color: isConnected ? colors.success : colors.red }}>
            {isConnected ? '● live' : '○ disconnected'}
          </span>
        </div>
        <span style={{ fontSize: 11, color: colors.textSecondary }}>
          {fmt(events.length)} events
        </span>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
        <div style={{ ...card, padding: '8px 12px', flex: '1 1 60px', minWidth: 60 }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: colors.text }}>{fmt(events.length)}</div>
          <div style={{ fontSize: 9, color: colors.textSecondary, textTransform: 'uppercase' }}>Events</div>
        </div>
        <div style={{ ...card, padding: '8px 12px', flex: '1 1 60px', minWidth: 60 }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: colors.primary }}>{fmt(computed.totalToolCalls)}</div>
          <div style={{ fontSize: 9, color: colors.textSecondary, textTransform: 'uppercase' }}>Tools</div>
        </div>
        <div style={{ ...card, padding: '8px 12px', flex: '1 1 60px', minWidth: 60 }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: colors.purple }}>{fmt(computed.thoughtCount)}</div>
          <div style={{ fontSize: 9, color: colors.textSecondary, textTransform: 'uppercase' }}>Thoughts</div>
        </div>
      </div>

      <div style={{ ...card, padding: '12px 14px', marginBottom: 10 }}>
        <div style={{ ...sectionTitle, marginBottom: 8, fontSize: 14 }}>Tool Execution</div>
        {computed.toolStats.length === 0 ? (
          <p style={{ color: colors.textSecondary, fontSize: 12 }}>No tool calls yet</p>
        ) : (
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Tool</th>
                <th style={th}>Calls</th>
                <th style={th}>Done</th>
                <th style={th}>Unsucc</th>
                <th style={th}>Fail</th>
                <th style={th}>Block</th>
              </tr>
            </thead>
            <tbody>
              {computed.toolStats.map((t) => (
                <tr key={t.tool_name}>
                  <td style={{ ...td, fontWeight: 600, fontSize: 12 }}>{t.tool_name}</td>
                  <td style={{ ...td, fontSize: 12 }}>{fmt(t.call_count)}</td>
                  <td style={{ ...td, fontSize: 12, color: colors.success }}>{fmt(t.completed)}</td>
                  <td style={{ ...td, fontSize: 12, color: t.unsuccessful > 0 ? '#ffa726' : colors.textSecondary }}>{fmt(t.unsuccessful)}</td>
                  <td style={{ ...td, fontSize: 12, color: t.failed > 0 ? colors.red : colors.textSecondary }}>{fmt(t.failed)}</td>
                  <td style={{ ...td, fontSize: 12, color: t.guardrail_blocked > 0 ? '#ff7043' : colors.textSecondary }}>{fmt(t.guardrail_blocked)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div style={{ ...card, padding: '12px 14px', marginBottom: 10 }}>
        <div style={{ ...sectionTitle, marginBottom: 8, fontSize: 14 }}>Tool Traces ({computed.traces.length})</div>
        {computed.traces.length === 0 ? (
          <p style={{ color: colors.textSecondary, fontSize: 12 }}>No completed traces</p>
        ) : (
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Tool</th>
                <th style={th}>Status</th>
                <th style={th}>Duration</th>
                <th style={th}>Guard</th>
              </tr>
            </thead>
            <tbody>
              {computed.traces.slice(0, 20).map((t) => (
                <tr key={t.tool_call_id}>
                  <td style={{ ...td, fontWeight: 600, fontSize: 12 }}>{t.tool_name}</td>
                  <td style={{ ...td, fontSize: 12 }}>
                    <span style={statusBadge(t.status)}>{t.status}</span>
                    {t.error && <span style={{ fontSize: 10, color: colors.red, marginLeft: 4 }}>{t.error.slice(0, 30)}</span>}
                  </td>
                  <td style={{ ...td, fontSize: 12 }}>{t.duration_ms > 0 ? formatDuration(t.duration_ms) : '-'}</td>
                  <td style={{ ...td, fontSize: 12, color: t.guardrail_id ? '#ff7043' : colors.textSecondary }}>
                    {t.guardrail_id || '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {computed.latestPlan && (
        <div style={{ ...card, padding: '12px 14px', marginBottom: 10 }}>
          <div style={{ ...sectionTitle, marginBottom: 8, fontSize: 14 }}>DAG Plan</div>
          <div style={{ fontSize: 12, marginBottom: 4 }}>
            <span style={{ fontWeight: 600 }}>Intent: </span>
            <span>{computed.latestPlan.intent}</span>
          </div>
          {computed.latestPlan.revision_reason && (
            <div style={{ fontSize: 11, padding: '2px 6px', background: colors.orangeLight, borderRadius: 4, color: colors.orange, marginBottom: 4 }}>
              Revised: {computed.latestPlan.revision_reason}
            </div>
          )}
          {computed.latestPlan.steps.length > 0 && (
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Step</th>
                  <th style={th}>Tool</th>
                  <th style={th}>Status</th>
                </tr>
              </thead>
              <tbody>
                {computed.latestPlan.steps.map((s) => (
                  <tr key={s.step_id}>
                    <td style={{ ...td, fontSize: 12 }}>{s.step_id}</td>
                    <td style={{ ...td, fontSize: 12 }}>{s.tool_name}</td>
                    <td style={{ ...td, fontSize: 12 }}>
                      <span style={statusBadge(s.status)}>{s.status}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div style={{ ...card, padding: '12px 14px', marginBottom: 10 }}>
        <div style={{ ...sectionTitle, marginBottom: 8, fontSize: 14 }}>Key Events</div>
        <div style={{ fontFamily: 'monospace', fontSize: 11, lineHeight: 1.5, maxHeight: 300, overflow: 'auto' }}>
          {keyEvents.slice(-50).map((e) => (
            <div key={`${e.seq}-${e.event_type}`} style={{ display: 'flex', gap: 6, padding: '2px 4px', borderLeft: `2px solid ${eventTypeBadge(e.event_type).background || '#ddd'}`, marginBottom: 1, background: '#fafafa' }}>
              <span style={{ color: '#999', minWidth: 28 }}>#{e.seq}</span>
              <span style={{ display: 'inline-block', padding: '0 4px', borderRadius: 3, fontSize: 9, background: eventTypeBadge(e.event_type).background || '#eee', color: '#fff', fontWeight: 700, minWidth: 70, textAlign: 'center', lineHeight: '16px', height: 16, overflow: 'hidden' }}>
                {e.event_type}
              </span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#333' }}>
                {eventSummary(e)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
