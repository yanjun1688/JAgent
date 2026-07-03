import { useEffect, useRef, useState } from 'react'
import { colors } from '../api/analysis-styles'
import type { WsEvent } from '../hooks/useRunWebSocket'

interface Props {
  events: WsEvent[]
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
          width: 8px;
          height: 8px;
          border-radius: 50%;
          margin-right: 6px;
          background: ${colors.primary};
          animation: tp-pulse 1.2s ease-in-out infinite;
        }
        .tp-dot:nth-child(2) { animation-delay: 0.2s; }
        .tp-dot:nth-child(3) { animation-delay: 0.4s; }
      `}</style>
      <div
        style={{
          background: '#f7f7fc',
          borderRadius: 10,
          border: `1px solid ${colors.border}`,
          margin: '4px 0',
          overflow: 'hidden',
        }}
      >
        <div
          onClick={onToggle}
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            padding: '8px 12px',
            cursor: 'pointer',
            userSelect: 'none',
            fontSize: 13,
            fontWeight: 600,
            color: colors.text,
            background: '#e8e8f4',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {loading && (
              <span style={{ display: 'inline-flex', alignItems: 'center' }}>
                <span className="tp-dot" />
                <span className="tp-dot" />
                <span className="tp-dot" />
              </span>
            )}
            Thinking {!open && internal.length > 0 ? `(${internal.length} steps)` : ''}
          </span>
          <span style={{ fontSize: 11, color: colors.textSecondary }}>
            {open ? '▲' : '▼'}
          </span>
        </div>
        {open && (
          <div
            style={{
              padding: '8px 10px',
              maxHeight: 320,
              overflow: 'auto',
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
            }}
          >
            {internal.length === 0 && loading && (
              <div style={{ textAlign: 'center', padding: 12, color: colors.textSecondary, fontSize: 12 }}>
                Agent is thinking...
              </div>
            )}
            {internal.length === 0 && !loading && (
              <div style={{ textAlign: 'center', padding: 12, color: colors.textSecondary, fontSize: 12 }}>
                No thinking steps yet.
              </div>
            )}
            {internal.map((event, idx) => (
              <ThinkingStep key={`${event.seq}-${idx}`} event={event} />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
    </>
  )
}

function eventLabel(event: WsEvent): string {
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
    case 'ContextCheckpointed':
      return 'Checkpoint saved'
    case 'FeedbackInjected':
      return `Feedback: ${String(event.payload.feedback_text || '').slice(0, 80)}`
    case 'PlanCreated':
      return 'Plan created'
    case 'PlanRevised':
      return 'Plan revised'
    case 'PlanCompleted':
      return 'Plan completed'
    case 'PlanFailed':
      return 'Plan failed'
    default:
      return event.event_type
  }
}

function ThinkingStep({ event }: { event: WsEvent }) {
  const [expanded, setExpanded] = useState(false)

  const isThought = event.event_type === 'AgentThought'
  const showExpand = event.event_type === 'AgentThought' || event.event_type === 'ToolCalled'

  const stepColors: Record<string, string> = {
    AgentThought: colors.primary,
    ToolCalled: '#ff9800',
    ToolCompleted: '#4caf50',
    ToolFailed: colors.red,
    ToolTimeout: colors.red,
    GuardrailTriggered: '#ff5722',
    ConfirmationRequested: '#ff9800',
    ConfirmationReceived: colors.success,
    ContextCompressed: '#607d8b',
    ContextCheckpointed: '#607d8b',
    PlanCreated: '#7c4dff',
    PlanRevised: '#ff9800',
    PlanCompleted: '#4caf50',
    PlanFailed: colors.red,
    FeedbackInjected: '#e91e63',
  }

  return (
    <div
      style={{
        fontSize: 12,
        padding: '4px 8px',
        borderRadius: 6,
        background: '#fff',
        border: `1px solid ${colors.border}`,
      }}
    >
      <div
        onClick={() => showExpand && setExpanded(!expanded)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          cursor: showExpand ? 'pointer' : 'default',
        }}
      >
        <span
          style={{
            display: 'inline-block',
            width: 6,
            height: 6,
            borderRadius: '50%',
            flexShrink: 0,
            background: stepColors[event.event_type] || '#999',
          }}
        />
        <span style={{ fontWeight: 600, color: colors.text }}>
          {eventLabel(event)}
        </span>
        {showExpand && (
          <span style={{ marginLeft: 'auto', fontSize: 10, color: colors.textSecondary }}>
            {expanded ? '▲' : '▼'}
          </span>
        )}
      </div>

      {/* Agent thought content */}
      {isThought && expanded && (
        <div
          style={{
            marginTop: 6,
            padding: '6px 8px',
            background: '#f0f0f8',
            borderRadius: 4,
            fontSize: 12,
            color: colors.textSecondary,
            fontStyle: 'italic',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            maxHeight: 200,
            overflow: 'auto',
          }}
        >
          {String(event.payload.thought || '').slice(0, 600)}
        </div>
      )}

      {/* Tool input */}
      {expanded && event.event_type === 'ToolCalled' && event.input && (
        <div style={{ marginTop: 4 }}>
          <div style={{ fontSize: 10, color: colors.textSecondary, marginBottom: 2 }}>Input:</div>
          <pre
            style={{
              margin: 0,
              fontSize: 11,
              background: '#f5f5f5',
              padding: '4px 6px',
              borderRadius: 4,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 120,
              overflow: 'auto',
            }}
          >
            {JSON.stringify(event.input, null, 1).slice(0, 300)}
          </pre>
        </div>
      )}
    </div>
  )
}
