import React, { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  getRun,
  pauseRun,
  resumeRun,
  confirmAction,
  deleteRun,
  RunDetail as RunDetailType,
} from '../api/client'
import type { WsEvent } from '../hooks/useRunWebSocket'
import { useRunWebSocket } from '../hooks/useRunWebSocket'
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
  ContextCompressed: '#26c6da',
  ContextCheckpointed: '#78909c',
  DagStepStarted: '#42a5f5',
  DagStepCompleted: '#66bb6a',
  DagStepFailed: '#ef5350',
  PlanCreated: '#7c4dff',
  PlanRevised: '#ffa726',
  PlanCompleted: '#66bb6a',
  PlanFailed: '#ef5350',
  FeedbackInjected: '#ec407a',
  RunPaused: '#78909c',
  RunResumed: '#26a69a',
  RunCompleted: '#66bb6a',
  RunFailed: '#ef5350',
}

export default function RunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [run, setRun] = useState<RunDetailType | null>(null)
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState(false)
  const [confirmDialog, setConfirmDialog] = useState<{
    confirmationId: string
    toolName: string
    input: Record<string, unknown> | undefined
    riskLevel: string | undefined
  } | null>(null)

  const [expandedSeqs, setExpandedSeqs] = useState<Set<number>>(new Set())
  const toggleExpand = useCallback((seq: number) => {
    setExpandedSeqs((prev) => {
      const next = new Set(prev)
      if (next.has(seq)) next.delete(seq); else next.add(seq)
      return next
    })
  }, [])

  const { events, runStatus, isConnected } = useRunWebSocket(runId ?? null)

  const load = useCallback(async () => {
    if (!runId) return
    try {
      const runData = await getRun(runId)
      setRun(runData)
    } catch {
      navigate('/')
    } finally {
      setLoading(false)
    }
  }, [runId, navigate])

  useEffect(() => {
    setLoading(true)
    load()
  }, [load])

  useEffect(() => {
    if (!runId || !runStatus) return
    if (runStatus !== run?.status) {
      getRun(runId).then(setRun).catch(() => {})
    }
  }, [runId, runStatus, run?.status])

  async function handlePause() {
    if (!runId) return
    setActing(true)
    try {
      await pauseRun(runId)
      await load()
    } finally {
      setActing(false)
    }
  }

  async function handleResume() {
    if (!runId) return
    setActing(true)
    try {
      await resumeRun(runId)
      await load()
    } finally {
      setActing(false)
    }
  }

  async function handleDelete() {
    if (!runId || !confirm('Delete this run?')) return
    await deleteRun(runId)
    navigate('/')
  }

  async function handleConfirm(confirmed: boolean, operatorId: string) {
    if (!runId || !confirmDialog) return
    setActing(true)
    try {
      await confirmAction(runId, confirmDialog.confirmationId, confirmed, operatorId)
      setConfirmDialog(null)
      if (run?.pause_reason === 'waiting_confirmation') {
        try {
          await resumeRun(runId)
        } catch {
          console.warn('Resume after confirm failed, relying on event stream to refresh status')
        }
      }
      await load()
    } finally {
      setActing(false)
    }
  }

  async function handleInlineConfirm(confirmationId: string, confirmed: boolean) {
    if (!runId) return
    setActing(true)
    try {
      await confirmAction(runId, confirmationId, confirmed, '')
      try {
        await resumeRun(runId)
      } catch {
        console.warn('Resume after confirm failed, relying on event stream to refresh status')
      }
      await load()
    } finally {
      setActing(false)
    }
  }

  function formatTime(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString()
  }

  function eventSummary(e: WsEvent): string {
    const p = e.payload
    switch (e.event_type) {
      case 'RunStarted':
        return `Intent: ${p.intent}`
      case 'AgentThought':
        return (p.thought as string).slice(0, 120)
      case 'ToolCalled':
        return `${e.tool_name}(${JSON.stringify(e.input).slice(0, 80)})`
      case 'ToolCompleted':
        return `${e.tool_name} → ${JSON.stringify(p.output).slice(0, 80)}`
      case 'ToolFailed':
        return `${e.tool_name} ✗ ${e.error}`
      case 'ToolTimeout':
        return `${e.tool_name} ⏱ timeout ${p.timeout_ms}ms`
      case 'GuardrailTriggered':
        return `${e.tool_name} 🛡 ${p.guardrail_id}: ${p.reason}`
      case 'ConfirmationRequested':
        return `${e.tool_name} ⚠ requires confirmation`
      case 'ConfirmationReceived':
        return `→ ${p.confirmed ? 'confirmed' : 'denied'} by ${p.operator_id}`
      case 'DagStepStarted':
        return `${e.tool_name} step ${p.step_id}`
      case 'DagStepCompleted':
        return `${e.tool_name} ✓ (${e.duration_ms}ms)`
      case 'DagStepFailed':
        return `${e.tool_name} ✗ ${e.error}`
      case 'PlanCreated':
        return `Plan: ${JSON.stringify(p).slice(0, 100)}`
      case 'PlanRevised':
        return `Revised: ${p.revision_reason as string}`
      case 'PlanCompleted':
        return `Plan completed ✓`
      case 'PlanFailed':
        return `Plan failed ✗ ${p.final_error as string}`
      case 'FeedbackInjected':
        return `Feedback: ${(p.message as string)?.slice(0, 100)}`
      case 'ContextCompressed':
        return `Compressed ${p.original_tokens as number}→${p.compressed_tokens as number} tokens`
      case 'ContextCheckpointed':
        return `Checkpoint at seq ${p.checkpoint_seq as number}`
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
          {run.workspace_id ? <p style={{ color: '#8b8b9a', margin: '4px 0', fontSize: 12 }}>Workspace: {run.workspace_id}</p> : null}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {run.status === 'running' && (
            <button onClick={handlePause} disabled={acting} style={{ cursor: acting ? 'not-allowed' : 'pointer', opacity: acting ? 0.6 : 1 }}>
              Pause
            </button>
          )}
          {run.status === 'paused' && run.pause_reason !== 'waiting_confirmation' && (
            <button onClick={handleResume} disabled={acting} style={{ cursor: acting ? 'not-allowed' : 'pointer', opacity: acting ? 0.6 : 1 }}>
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
        <span>Events: <strong>{events.length}</strong></span>
        {run.last_error ? <span style={{ color: '#ef5350' }}>Error: {run.last_error}</span> : null}
        {run.pause_reason ? <span>Paused: {run.pause_reason}</span> : null}
        <span style={{ color: isConnected ? '#66bb6a' : '#ef5350' }}>
          {isConnected ? '● live' : '○ disconnected'}
        </span>
      </div>

      {run.status === 'paused' && run.pause_reason === 'waiting_confirmation' && run.pending_confirmations && run.pending_confirmations.length > 0 && (
        <div style={{ background: '#fff3e0', border: '1px solid #ffe0b2', borderRadius: 12, padding: 20, marginBottom: 16 }}>
          <h3 style={{ margin: '0 0 4px', color: '#e65100', fontSize: 16 }}>⚠ Confirmation Required</h3>
          <p style={{ margin: '0 0 16px', color: '#666', fontSize: 14 }}>
            The following tool needs your approval before the run can continue:
          </p>
          {run.pending_confirmations.map((pc) => (
            <div key={pc.confirmation_id} style={{ background: '#fff', border: '1px solid #ffe0b2', borderRadius: 8, padding: 12, marginBottom: 12 }}>
              <div style={{ marginBottom: 8 }}>
                <strong>{pc.tool_name}</strong>
                <span style={{ marginLeft: 8, fontSize: 12, color: pc.risk_level === 'high' ? '#d32f2f' : '#f57c00' }}>
                  (risk: {pc.risk_level})
                </span>
              </div>
              {pc.input && Object.keys(pc.input).length > 0 && (
                <pre style={{ margin: '0 0 8px', fontSize: 12, background: '#f5f5f5', padding: 8, borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                  {JSON.stringify(pc.input, null, 2)}
                </pre>
              )}
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button
                  onClick={() => handleInlineConfirm(pc.confirmation_id, false)}
                  style={{ padding: '6px 16px', background: '#ef5350', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 13 }}
                >
                  Deny & Continue
                </button>
                <button
                  onClick={() => handleInlineConfirm(pc.confirmation_id, true)}
                  style={{ padding: '6px 16px', background: '#66bb6a', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 13 }}
                >
                  Approve & Continue
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <h2>Event Stream</h2>
      <div style={{ fontFamily: 'monospace', fontSize: 13, lineHeight: 1.6 }}>
        {events.map((e) => (
          <div key={`${e.seq}-${e.event_type}`}>
            <div
              onClick={() => toggleExpand(e.seq)}
              style={{
                padding: '6px 12px',
                borderLeft: `3px solid ${EVENT_COLORS[e.event_type] || '#ddd'}`,
                marginBottom: expandedSeqs.has(e.seq) ? 0 : 2,
                background: expandedSeqs.has(e.seq) ? '#f0f0f0' : '#fafafa',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              <span style={{ color: '#999', minWidth: 36, fontSize: 12 }}>
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
                  minWidth: 100,
                  textAlign: 'center' as const,
                }}
              >
                {e.event_type}
              </span>
              <span style={{ color: '#999', minWidth: 65, marginRight: 8 }}>{formatTime(e.created_at)}</span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{eventSummary(e)}</span>
              <span style={{ color: '#ccc', fontSize: 11, flexShrink: 0 }}>{expandedSeqs.has(e.seq) ? '▲' : '▼'}</span>
            </div>
            {expandedSeqs.has(e.seq) && (
              <div
                style={{
                  padding: '10px 16px',
                  background: '#f8f9fa',
                  borderLeft: '3px solid #ddd',
                  fontSize: 12,
                  marginBottom: 2,
                }}
              >
                <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 300, overflow: 'auto' }}>
                  {JSON.stringify(e.payload, null, 2)}
                </pre>
              </div>
            )}
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
