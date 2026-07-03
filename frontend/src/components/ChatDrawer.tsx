import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { colors } from '../api/analysis-styles'
import { getRunTimeline } from '../api/analysis-client'
import { createRun, confirmAction, pauseRun, resumeRun } from '../api/client'
import { useRunWebSocket, type WsEvent } from '../hooks/useRunWebSocket'
import ThinkingPanel from './ThinkingPanel'

interface Props {
  style?: React.CSSProperties
  initialRunId?: string
  onRunChange?: (runId: string | null) => void
}

export default function ChatDrawer({ style, initialRunId, onRunChange }: Props) {
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeRunStatus, setActiveRunStatus] = useState<string>('')
  const [timelineEvents, setTimelineEvents] = useState<WsEvent[]>([])
  const [input, setInput] = useState('')
  const [lastUserMessage, setLastUserMessage] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [thoughtOpen, setThoughtOpen] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  const { events: wsEvents, runStatus: wsRunStatus, isConnected } = useRunWebSocket(activeRunId)

  const allEvents = useMemo(() => {
    const tSeqs = new Set(timelineEvents.map((e) => e.seq))
    const newWs = wsEvents.filter((e) => !tSeqs.has(e.seq))
    return [...timelineEvents, ...newWs].sort((a, b) => a.seq - b.seq)
  }, [timelineEvents, wsEvents])

  useEffect(() => {
    if (wsRunStatus) setActiveRunStatus(wsRunStatus)
  }, [wsRunStatus])

  const finalAnswer = useMemo(() => {
    const completed = allEvents.find((e) => e.event_type === 'RunCompleted')
    if (completed) return String(completed.payload.result_summary || '')
    const failed = allEvents.find((e) => e.event_type === 'RunFailed')
    if (failed) return String(failed.payload.final_error || '')
    return null
  }, [allEvents])

  const pendingConfirmations = useMemo(() => {
    const received = new Set<string>()
    const requested: WsEvent[] = []
    for (const e of allEvents) {
      if (e.event_type === 'ConfirmationReceived' && e.confirmation_id) {
        received.add(e.confirmation_id)
      }
      if (e.event_type === 'ConfirmationRequested' && e.confirmation_id) {
        requested.push(e)
      }
    }
    return requested.filter((e) => e.confirmation_id && !received.has(e.confirmation_id!))
  }, [allEvents])

  const showConfirmationCard = useMemo(() => {
    if (pendingConfirmations.length === 0) return false
    for (let i = allEvents.length - 1; i >= 0; i--) {
      if (allEvents[i].event_type === 'RunResumed') return false
      if (allEvents[i].event_type === 'RunPaused' && allEvents[i].payload.reason === 'waiting_confirmation') return true
    }
    return false
  }, [allEvents, pendingConfirmations])

  async function handleConfirmResume(confirmationId: string, confirmed: boolean) {
    setLoading(true)
    try {
      await confirmAction(activeRunId!, confirmationId, confirmed, '')
      await resumeRun(activeRunId!)
    } finally {
      setLoading(false)
    }
  }

  function loadRun(runId: string) {
    setActiveRunId(runId)
    onRunChange?.(runId)
    setActiveRunStatus('')
    setLastUserMessage(null)
    setLoading(true)
    setError(null)
    setTimelineEvents([])
    getRunTimeline(runId, 200, 0)
      .then((timeline) => {
        setTimelineEvents(timeline.timeline as WsEvent[])
        const started = timeline.timeline.find((e) => e.event_type === 'RunStarted')
        if (started) setLastUserMessage(String(started.payload.intent || ''))
        const last = timeline.timeline[timeline.timeline.length - 1]
        if (last) {
          if (last.event_type === 'RunCompleted') setActiveRunStatus('completed')
          else if (last.event_type === 'RunFailed') setActiveRunStatus('failed')
          else setActiveRunStatus('running')
        }
        const finished = last?.event_type === 'RunCompleted' || last?.event_type === 'RunFailed'
        setThoughtOpen(!finished)
      })
      .catch((err) => {
        const msg = err instanceof Error ? err.message : String(err)
        console.error('[ChatDrawer] loadRun error:', err)
        setError(msg)
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (!initialRunId) return
    loadRun(initialRunId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [allEvents.length])

  async function handleSubmit() {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setLastUserMessage(text)
    setLoading(true)
    setActiveRunId(null)
    setActiveRunStatus('running')
    setTimelineEvents([])
    setError(null)
    setThoughtOpen(true)

    try {
      const { run_id } = await createRun(text)
      setActiveRunId(run_id)
      onRunChange?.(run_id)
      const timeline = await getRunTimeline(run_id, 200, 0)
      setTimelineEvents(timeline.timeline as WsEvent[])
      const last = timeline.timeline[timeline.timeline.length - 1]
      if (last) {
        if (last.event_type === 'RunCompleted') setActiveRunStatus('completed')
        else if (last.event_type === 'RunFailed') setActiveRunStatus('failed')
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      console.error('[ChatDrawer] handleSubmit error:', err)
      setError(msg)
    }
    setLoading(false)
  }

  const welcome = !activeRunId && allEvents.length === 0

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: '#fff',
        borderRadius: 12,
        border: `1px solid ${colors.border}`,
        overflow: 'hidden',
        ...style,
      }}
    >
      {/* header */}
      <div
        style={{
          padding: '12px 16px',
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          background: '#fafafa',
          flexShrink: 0,
        }}
      >
        <div>
          <span style={{ fontWeight: 700, fontSize: 14 }}>
            {activeRunId ? `Run ${activeRunId.slice(0, 8)}` : 'Agent Chat'}
          </span>
          {activeRunStatus && (
            <span
              style={{
                display: 'inline-block',
                padding: '2px 10px',
                borderRadius: 12,
                fontSize: 10,
                fontWeight: 700,
                color: '#fff',
                background:
                  activeRunStatus === 'running'
                    ? '#4fc3f7'
                    : activeRunStatus === 'completed'
                      ? '#66bb6a'
                      : activeRunStatus === 'failed'
                        ? '#ef5350'
                        : '#999',
                marginLeft: 8,
                verticalAlign: 'middle',
              }}
            >
              {activeRunStatus}
            </span>
          )}
        </div>
        <span style={{ fontSize: 11, color: colors.textSecondary }}>
          {allEvents.length} event{allEvents.length !== 1 ? 's' : ''}
          {activeRunStatus === 'running' && isConnected ? ' · live' : ''}
        </span>
      </div>

      {/* chat area */}
      <div
        style={{
          flex: 1,
          overflow: 'auto',
          padding: 12,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        {welcome ? (
          <div style={{ textAlign: 'center', padding: '40px 20px', color: colors.textSecondary }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>🤖</div>
            <div style={{ fontWeight: 600, fontSize: 15, color: colors.text, marginBottom: 4 }}>
              Start a conversation
            </div>
            <div style={{ fontSize: 13 }}>Type a task below and watch the agent work.</div>
          </div>
        ) : (
          <>
            {/* user message */}
            {lastUserMessage && (
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <div
                  style={{
                    maxWidth: '85%',
                    padding: '8px 14px',
                    borderRadius: 14,
                    borderBottomRightRadius: 4,
                    background: '#1a73e8',
                    color: '#fff',
                    fontSize: 13,
                    lineHeight: 1.5,
                    wordBreak: 'break-word',
                  }}
                >
                  {lastUserMessage}
                </div>
              </div>
            )}

            {/* thinking panel */}
            {activeRunId && (
              <ThinkingPanel
                events={allEvents}
                open={thoughtOpen}
                onToggle={() => setThoughtOpen(!thoughtOpen)}
                loading={activeRunStatus === 'running'}
              />
            )}

            {/* confirmation card */}
            {showConfirmationCard && pendingConfirmations.map((pc) => (
              <div
                key={pc.confirmation_id}
                style={{
                  background: '#fff3e0',
                  border: '1px solid #ffe0b2',
                  borderRadius: 10,
                  padding: 14,
                  margin: '4px 0',
                }}
              >
                <div style={{ fontSize: 13, fontWeight: 600, color: '#e65100', marginBottom: 6 }}>
                  ⚠ Confirmation Required
                </div>
                <div style={{ fontSize: 13, marginBottom: 6 }}>
                  <strong>{pc.tool_name}</strong>
                  {!!pc.payload?.risk_level && (
                    <span style={{ marginLeft: 6, fontSize: 11, color: String(pc.payload.risk_level) === 'high' ? '#d32f2f' : '#f57c00' }}>
                      (risk: {String(pc.payload.risk_level)})
                    </span>
                  )}
                </div>
                {pc.input && Object.keys(pc.input).length > 0 && (
                  <pre style={{ margin: '0 0 8px', fontSize: 11, background: '#f5f5f5', padding: 8, borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                    {JSON.stringify(pc.input, null, 2)}
                  </pre>
                )}
                <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                  <button
                    onClick={() => handleConfirmResume(pc.confirmation_id!, false)}
                    style={{ padding: '5px 14px', background: '#ef5350', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}
                  >
                    Deny & Continue
                  </button>
                  <button
                    onClick={() => handleConfirmResume(pc.confirmation_id!, true)}
                    style={{ padding: '5px 14px', background: '#66bb6a', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}
                  >
                    Approve & Continue
                  </button>
                </div>
              </div>
            ))}

            {/* final answer */}
            {finalAnswer && (
              <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
                <div
                  style={{
                    maxWidth: '92%',
                    padding: '10px 14px',
                    borderRadius: 8,
                    border: `1px solid ${colors.border}`,
                    background: '#fff',
                    color: colors.text,
                    fontSize: 14,
                    lineHeight: 1.6,
                    wordBreak: 'break-word',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {finalAnswer}
                </div>
              </div>
            )}

            {/* error */}
            {error && (
              <div
                style={{
                  margin: '8px 0',
                  padding: '8px 12px',
                  background: colors.redLight,
                  borderRadius: 6,
                  border: `1px solid ${colors.red}`,
                  fontSize: 12,
                  color: colors.red,
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: 2 }}>Error</div>
                <div style={{ wordBreak: 'break-all' }}>{error}</div>
              </div>
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      {/* input */}
      <form
        onSubmit={(e) => { e.preventDefault(); handleSubmit() }}
        style={{
          display: 'flex',
          gap: 8,
          padding: '10px 12px',
          borderTop: `1px solid ${colors.border}`,
          background: '#fafafa',
          flexShrink: 0,
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={activeRunStatus === 'running' ? 'Agent is working...' : 'Type a task...'}
          disabled={loading || activeRunStatus === 'running'}
          style={{
            flex: 1,
            padding: '8px 12px',
            border: `1px solid ${colors.border}`,
            borderRadius: 8,
            fontSize: 13,
            outline: 'none',
            background: activeRunStatus === 'running' ? colors.bg : '#fff',
          }}
        />
        {activeRunStatus === 'running' && (
          <button
            onClick={() => pauseRun(activeRunId!)}
            disabled={loading}
            style={{
              padding: '8px 14px',
              background: loading ? '#ffcc80' : '#ffb74d',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            ⏸ Pause
          </button>
        )}
        {activeRunStatus === 'paused' && (
          <button
            onClick={() => resumeRun(activeRunId!)}
            disabled={loading}
            style={{
              padding: '8px 14px',
              background: loading ? '#a5d6a7' : '#66bb6a',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            ▶ Resume
          </button>
        )}
        <button
          type="submit"
          disabled={loading || !input.trim() || activeRunStatus === 'running'}
          style={{
            padding: '8px 18px',
            background: !input.trim() || activeRunStatus === 'running' ? '#ccc' : '#6c5ce7',
            color: '#fff',
            border: 'none',
            borderRadius: 8,
            cursor: !input.trim() || activeRunStatus === 'running' ? 'default' : 'pointer',
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
