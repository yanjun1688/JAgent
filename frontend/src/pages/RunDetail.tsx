import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  getRun,
  getRunEvents,
  pauseRun,
  resumeRun,
  confirmAction,
  deleteRun,
  connectEventStream,
  RunDetail as RunDetailType,
  HarnessEvent,
} from '../api/client'
import type { PendingConfirmationItem } from '../api/schema'
import ConfirmDialog from '../components/ConfirmDialog'

const EVENT_COLORS: Record<string, string> = {
  RunStarted: '#4fc3f7',
  AgentThought: '#ce93d8',
  ToolCalled: '#ffb74d',
  ToolCompleted: '#66bb6a',
  ToolFailed: '#ef5350',
  ToolTimeout: '#ef5350',
  GuardrailTriggered: '#ff7043',
  ConfirmationRequested: '#ffa726',
  ConfirmationReceived: '#26a69a',
  RunPaused: '#78909c',
  RunResumed: '#26a69a',
  RunCompleted: '#66bb6a',
  RunFailed: '#ef5350',
}

export default function RunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [run, setRun] = useState<RunDetailType | null>(null)
  const [events, setEvents] = useState<HarnessEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [confirmDialog, setConfirmDialog] = useState<{
    confirmationId: string
    toolName: string
    input: Record<string, unknown> | undefined
    riskLevel: string | undefined
  } | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const lastSeqRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async () => {
    if (!runId) return
    try {
      const [runData, eventsData] = await Promise.all([getRun(runId), getRunEvents(runId)])
      setRun(runData)
      setEvents(eventsData.events)
      if (eventsData.events.length > 0) {
        lastSeqRef.current = eventsData.events[eventsData.events.length - 1].seq
      }
    } catch {
      navigate('/')
    } finally {
      setLoading(false)
    }
  }, [runId, navigate])

  useEffect(() => {
    load()
    if (!runId) return

    let mounted = true

    function startWs() {
      if (!mounted) return
      if (wsRef.current) {
        wsRef.current.close()
      }
      const ws = connectEventStream(runId!, (event) => {
        if (event.seq <= lastSeqRef.current) return
        lastSeqRef.current = event.seq
        setEvents((prev) => [...prev, event])
        getRun(runId!).then(setRun).catch(() => {})
      })
      wsRef.current = ws

      ws.onclose = () => {
        reconnectTimerRef.current = setTimeout(startWs, 1000)
      }
    }

    startWs()

    return () => {
      mounted = false
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [runId, load])

  async function handlePause() {
    if (!runId) return
    await pauseRun(runId)
    await load()
  }

  async function handleResume() {
    if (!runId) return
    await resumeRun(runId)
    await load()
  }

  async function handleDelete() {
    if (!runId || !confirm('Delete this run?')) return
    await deleteRun(runId)
    navigate('/')
  }

  async function handleConfirm(confirmed: boolean, operatorId: string) {
    if (!runId || !confirmDialog) return
    await confirmAction(runId, confirmDialog.confirmationId, confirmed, operatorId)
    setConfirmDialog(null)
    await load()
  }

  function formatTime(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString()
  }

  function eventSummary(e: HarnessEvent): string {
    const p = e.payload
    switch (e.event_type) {
      case 'RunStarted':
        return `Intent: ${p.intent}`
      case 'AgentThought':
        return (p.thought as string).slice(0, 120)
      case 'ToolCalled':
        return `${p.tool_name}(${JSON.stringify(p.input).slice(0, 80)})`
      case 'ToolCompleted':
        return `${p.tool_name} → ${JSON.stringify(p.output).slice(0, 80)}`
      case 'ToolFailed':
        return `${p.tool_name} ✗ ${p.error}`
      case 'ToolTimeout':
        return `${p.tool_name} ⏱ timeout ${p.timeout_ms}ms`
      case 'GuardrailTriggered':
        return `${p.tool_name} 🛡 ${p.guardrail_id}: ${p.reason}`
      case 'ConfirmationRequested':
        return `${p.tool_name} ⚠ requires confirmation`
      case 'ConfirmationReceived':
        return `→ ${p.confirmed ? 'confirmed' : 'denied'} by ${p.operator_id}`
      case 'RunPaused':
        return `Paused: ${p.reason}`
      case 'RunResumed':
        return `Resumed from seq ${p.resume_from_seq}`
      case 'RunCompleted':
        return p.result_summary as string
      case 'RunFailed':
        return `✗ ${p.final_error}`
      default:
        return JSON.stringify(p)
    }
  }

  if (loading) return <p>Loading...</p>
  if (!run) return <p>Run not found</p>

  return (
    <div>
      <button onClick={() => navigate('/')} style={{ marginBottom: 16, cursor: 'pointer' }}>
        ← Back to Runs
      </button>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <h1 style={{ margin: 0 }}>Run {run.run_id}</h1>
          <p style={{ color: '#666', margin: '4px 0' }}>{run.intent}</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {run.status === 'running' && (
            <button onClick={handlePause} style={{ cursor: 'pointer' }}>
              Pause
            </button>
          )}
          {run.status === 'paused' && (
            <button onClick={handleResume} style={{ cursor: 'pointer' }}>
              Resume
            </button>
          )}
          <button onClick={handleDelete} style={{ color: '#ef5350', cursor: 'pointer' }}>
            Delete
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 16, marginBottom: 24, fontSize: 14, color: '#666' }}>
        <span>Status: <strong>{run.status}</strong></span>
        <span>Events: <strong>{run.event_count}</strong></span>
        {run.last_error ? <span style={{ color: '#ef5350' }}>Error: {run.last_error}</span> : null}
        {run.pause_reason ? <span>Paused: {run.pause_reason}</span> : null}
      </div>

      {run.pending_confirmations && run.pending_confirmations.length > 0 && (
        <div style={{ background: '#fff3e0', padding: 16, borderRadius: 8, marginBottom: 16 }}>
          <h3 style={{ margin: '0 0 8px' }}>Pending Confirmations</h3>
          {run.pending_confirmations?.map((pc) => (
            <div key={pc.confirmation_id} style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 8 }}>
              <span>
                Tool: <strong>{pc.tool_name}</strong> (risk: {pc.risk_level})
              </span>
              <button
                onClick={() => setConfirmDialog({
                  confirmationId: pc.confirmation_id,
                  toolName: pc.tool_name,
                  input: pc.input,
                  riskLevel: pc.risk_level,
                })}
                style={{ cursor: 'pointer' }}
              >
                Review
              </button>
            </div>
          ))}
        </div>
      )}

      <h2>Event Stream</h2>
      <div style={{ fontFamily: 'monospace', fontSize: 13, lineHeight: 1.6 }}>
        {events.map((e) => (
          <div
            key={`${e.seq}-${e.event_type}`}
            style={{
              padding: '6px 12px',
              borderLeft: `3px solid ${EVENT_COLORS[e.event_type] || '#ddd'}`,
              marginBottom: 2,
              background: '#fafafa',
            }}
          >
            <span style={{ color: '#999', marginRight: 8 }}>
              #{e.seq}
            </span>
            <span
              style={{
                display: 'inline-block',
                padding: '1px 6px',
                borderRadius: 4,
                fontSize: 11,
                background: EVENT_COLORS[e.event_type] || '#eee',
                color: '#fff',
                marginRight: 8,
                fontWeight: 'bold',
              }}
            >
              {e.event_type}
            </span>
            <span style={{ color: '#999', marginRight: 8 }}>{formatTime(e.created_at)}</span>
            <span>{eventSummary(e)}</span>
          </div>
        ))}
      </div>

      {confirmDialog && (
        <ConfirmDialog
          toolName={confirmDialog.toolName}
          input={confirmDialog.input}
          riskLevel={confirmDialog.riskLevel}
          onConfirm={(operatorId) => handleConfirm(true, operatorId)}
          onDeny={(operatorId) => handleConfirm(false, operatorId)}
          onClose={() => setConfirmDialog(null)}
        />
      )}
    </div>
  )
}
