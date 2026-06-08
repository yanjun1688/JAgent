import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { colors } from '../api/analysis-styles'
import type { ParsedEventDetail } from '../api/analysis-types'
import { getRunTimeline } from '../api/analysis-client'
import { createRun } from '../api/client'
import ThinkingPanel from './ThinkingPanel'

interface Props {
  style?: React.CSSProperties
  initialRunId?: string
}

export default function ChatDrawer({ style, initialRunId }: Props) {
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeRunStatus, setActiveRunStatus] = useState<string>('')
  const [events, setEvents] = useState<ParsedEventDetail[]>([])
  const [input, setInput] = useState('')
  const [lastUserMessage, setLastUserMessage] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [thoughtOpen, setThoughtOpen] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const finalAnswer = useMemo(() => {
    const completed = events.find((e) => e.event_type === 'RunCompleted')
    if (completed) return String(completed.payload.result_summary || '')
    const failed = events.find((e) => e.event_type === 'RunFailed')
    if (failed) return String(failed.payload.final_error || '')
    return null
  }, [events])

  const connectWs = useCallback((runId: string) => {
    if (wsRef.current) {
      wsRef.current.onclose = null
      wsRef.current.close()
    }
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/runs/${runId}/events`)
    wsRef.current = ws

    ws.onmessage = (msg) => {
      try {
        const raw = JSON.parse(msg.data)
        const event: ParsedEventDetail = {
          run_id: raw.run_id || runId,
          seq: raw.seq,
          event_type: raw.event_type,
          created_at: raw.created_at,
          payload: raw.payload || {},
          tool_call_id: raw.payload?.tool_call_id || null,
          tool_name: raw.payload?.tool_name || null,
          input: raw.payload?.input || null,
          idempotency_key: raw.idempotency_key || null,
          confirmation_id: raw.payload?.confirmation_id || null,
          error: raw.payload?.error || null,
          duration_ms: raw.payload?.duration_ms || null,
          retryable: null,
        }
        setEvents((prev) => [...prev, event])
        if (raw.event_type === 'RunCompleted' || raw.event_type === 'RunFailed') {
          setActiveRunStatus(raw.event_type === 'RunCompleted' ? 'completed' : 'failed')
        }
      } catch { /* ignore malformed ws msg */ }
    }
    ws.onclose = () => { wsRef.current = null }
  }, [])

  useEffect(() => {
    if (!initialRunId) return
    setActiveRunId(initialRunId)
    setActiveRunStatus('')
    setLastUserMessage(null)
    setLoading(true)
    setError(null)
    getRunTimeline(initialRunId, 200, 0)
      .then((timeline) => {
        setEvents(timeline.timeline)
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
        if (!finished) connectWs(initialRunId)
      })
      .catch((err) => {
        const msg = err instanceof Error ? err.message : String(err)
        console.error('[ChatDrawer] initialRunId load error:', err)
        setError(msg)
      })
      .finally(() => setLoading(false))
    return () => {
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  async function handleSubmit() {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setLastUserMessage(text)
    setLoading(true)
    setActiveRunId(null)
    setActiveRunStatus('running')
    setEvents([])
    setError(null)
    setThoughtOpen(true)

    try {
      const { run_id } = await createRun(text)
      setActiveRunId(run_id)
      const timeline = await getRunTimeline(run_id, 200, 0)
      setEvents(timeline.timeline)
      const last = timeline.timeline[timeline.timeline.length - 1]
      if (last) {
        if (last.event_type === 'RunCompleted') setActiveRunStatus('completed')
        else if (last.event_type === 'RunFailed') setActiveRunStatus('failed')
      }
      connectWs(run_id)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      console.error('[ChatDrawer] handleSubmit error:', err)
      setError(msg)
    }
    setLoading(false)
  }

  const welcome = !activeRunId && events.length === 0

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
          {events.length} event{events.length !== 1 ? 's' : ''}
          {activeRunStatus === 'running' ? ' · live' : ''}
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
                events={events}
                open={thoughtOpen}
                onToggle={() => setThoughtOpen(!thoughtOpen)}
                loading={activeRunStatus === 'running'}
              />
            )}

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
