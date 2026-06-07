import { useEffect, useRef, useState } from 'react'
import { colors } from '../api/analysis-styles'
import type { ParsedEventDetail } from '../api/analysis-types'

interface Props {
  events: ParsedEventDetail[]
  open: boolean
  onToggle: () => void
  loading?: boolean
}

export default function ThinkingPanel({ events, open, onToggle, loading }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const internal = events.filter(
    (e) => !['RunStarted', 'RunCompleted', 'RunFailed'].includes(e.event_type),
  )

  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [internal.length, open])

  return (
    <>
      <style>{`
        @keyframes tp-pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(0.8); }
        }
        .tp-dot {
          display: inline-block;
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: #6c5ce7;
          animation: tp-pulse 1.2s ease-in-out infinite;
        }
        .tp-dot-green {
          background: #66bb6a;
          animation-delay: 0.2s;
        }
      `}</style>

      <div
        style={{
          border: `1px solid ${colors.border}`,
          borderRadius: 8,
          background: '#f8f9fb',
          fontSize: 12,
          overflow: 'hidden',
        }}
      >
        <div
          onClick={onToggle}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '6px 10px',
            cursor: 'pointer',
            userSelect: 'none',
            borderBottom: open ? `1px solid ${colors.border}` : 'none',
            background: '#f0f1f4',
          }}
        >
          <span
            style={{
              fontSize: 10,
              color: '#999',
              transition: 'transform 0.15s',
              transform: open ? 'rotate(90deg)' : '',
            }}
          >
            ▶
          </span>
          <span style={{ fontWeight: 600, fontSize: 12, color: '#555' }}>Thinking Process</span>
          <span style={{ color: colors.textSecondary, fontSize: 10 }}>
            {internal.length} step{internal.length !== 1 ? 's' : ''}
          </span>
          {open && loading && (
            <span
              style={{
                marginLeft: 'auto',
                display: 'flex',
                alignItems: 'center',
                gap: 4,
                color: '#6c5ce7',
                fontSize: 10,
                fontWeight: 600,
              }}
            >
              <span className="tp-dot" />
              Live
            </span>
          )}
        </div>

        {open && (
          <div style={{ padding: '4px 0', maxHeight: 320, overflow: 'auto' }}>
            {internal.length === 0 && !loading && (
              <div style={{ padding: 12, color: '#999', textAlign: 'center' }}>No thinking steps yet</div>
            )}
            {internal.length === 0 && loading && (
              <div style={{ padding: 12, color: '#999', textAlign: 'center' }}>Initializing agent...</div>
            )}
            {internal.map((e) => (
              <ThinkingStep key={e.seq} event={e} />
            ))}
            {loading && (
              <div
                style={{
                  padding: '6px 10px',
                  color: '#999',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                }}
              >
                <span className="tp-dot" />
                Processing...
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
    </>
  )
}

function ThinkingStep({ event }: { event: ParsedEventDetail }) {
  const [expanded, setExpanded] = useState(false)

  const isError = ['ToolFailed', 'ToolTimeout'].includes(event.event_type)
  const hasDetail = event.event_type === 'AgentThought' || event.event_type === 'ToolCalled'

  const time = new Date(event.created_at * 1000).toLocaleTimeString()

  const dotColor = isError
    ? '#ef5350'
    : event.event_type === 'ToolCompleted'
      ? '#66bb6a'
      : event.event_type === 'AgentThought'
        ? '#6c5ce7'
        : event.event_type === 'ToolCalled'
          ? '#ffb74d'
          : '#ccc'

  const line1 = (() => {
    switch (event.event_type) {
      case 'AgentThought':
        return 'Thinking...'
      case 'ToolCalled':
        return `Calling tool: ${event.tool_name || 'unknown'}`
      case 'ToolCompleted':
        return `Tool ${event.tool_name || ''} completed (${event.duration_ms ? (event.duration_ms / 1000).toFixed(1) + 's' : 'ok'})`
      case 'ToolFailed':
        return `Tool ${event.tool_name || ''} failed`
      case 'ToolTimeout':
        return `Tool ${event.tool_name || ''} timed out`
      case 'GuardrailTriggered':
        return `Guardrail blocked: ${event.tool_name || ''}`
      case 'ConfirmationRequested':
        return `Awaiting confirmation: ${event.tool_name || ''}`
      case 'ConfirmationReceived':
        return `Confirmation: ${event.payload.confirmed ? 'Approved' : 'Denied'}`
      case 'ContextCompressed':
        return 'Context compressed'
      case 'RunPaused':
        return 'Run paused'
      case 'RunResumed':
        return 'Run resumed'
      case 'FeedbackInjected':
        return `Feedback: ${String(event.payload.feedback_text || '').slice(0, 80)}`
      default:
        return event.event_type
    }
  })()

  return (
    <div
      onClick={() => hasDetail && setExpanded(!expanded)}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        padding: '3px 10px',
        cursor: hasDetail ? 'pointer' : 'default',
        background: isError ? '#fff5f5' : 'transparent',
        borderLeft: `3px solid ${isError ? '#ef5350' : 'transparent'}`,
      }}
    >
      <span
        style={{
          flexShrink: 0,
          display: 'inline-block',
          width: 6,
          height: 6,
          borderRadius: '50%',
          marginTop: 7,
          background: dotColor,
        }}
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, lineHeight: '20px' }}>
          <span
            style={{
              fontSize: 12,
              color: isError ? '#ef5350' : event.event_type === 'ToolCompleted' ? '#66bb6a' : '#555',
              fontWeight: isError ? 600 : 400,
            }}
          >
            {line1}
          </span>
          <span style={{ color: '#bbb', fontSize: 9, flexShrink: 0 }}>{time}</span>
        </div>
        {expanded && event.event_type === 'AgentThought' && (
          <pre
            style={{
              margin: '2px 0 0',
              fontSize: 11,
              color: '#666',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 120,
              overflow: 'auto',
              lineHeight: 1.4,
              fontFamily: 'inherit',
            }}
          >
            {String(event.payload.thought || '').slice(0, 600)}
          </pre>
        )}
        {expanded && event.event_type === 'ToolCalled' && event.input && (
          <pre
            style={{
              margin: '2px 0 0',
              fontSize: 10,
              color: '#888',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 100,
              overflow: 'auto',
              lineHeight: 1.3,
              fontFamily: 'inherit',
            }}
          >
            {JSON.stringify(event.input, null, 1).slice(0, 300)}
          </pre>
        )}
      </div>
    </div>
  )
}
