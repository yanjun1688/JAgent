import { useState } from 'react'
import { colors } from '../api/analysis-styles'

export type ToolCallStatus = 'running' | 'completed' | 'failed' | 'timeout' | 'guardrail_blocked'

interface Props {
  toolName: string
  status: ToolCallStatus
  input?: Record<string, unknown> | null
  output?: unknown
  error?: string | null
  durationMs?: number | null
}

const statusConfig: Record<ToolCallStatus, { icon: string; label: string; color: string; bg: string }> = {
  running: { icon: '◌', label: 'Running', color: '#4fc3f7', bg: '#e0f7fa' },
  completed: { icon: '✓', label: 'Completed', color: colors.success, bg: colors.successLight },
  failed: { icon: '✗', label: 'Failed', color: colors.red, bg: colors.redLight },
  timeout: { icon: '⏱', label: 'Timed out', color: colors.orange, bg: colors.orangeLight },
  guardrail_blocked: { icon: '⊘', label: 'Blocked by guardrail', color: '#ff7043', bg: '#fbe9e7' },
}

export default function ToolCallCard({ toolName, status, input, output, error, durationMs }: Props) {
  const config = statusConfig[status]
  const [expanded, setExpanded] = useState(false)

  return (
    <div
      style={{
        background: config.bg,
        border: `1px solid ${colors.border}`,
        borderRadius: 8,
        padding: '8px 12px',
        margin: '4px 0',
        fontSize: 13,
      }}
    >
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          cursor: (input || output || error) ? 'pointer' : 'default',
        }}
      >
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 20,
            height: 20,
            borderRadius: '50%',
            background: config.color,
            color: '#fff',
            fontSize: 11,
            fontWeight: 700,
            flexShrink: 0,
          }}
        >
          {config.icon}
        </span>
        <span style={{ fontWeight: 600, color: colors.text }}>{toolName}</span>
        <span
          style={{
            fontSize: 11,
            color: config.color,
            fontWeight: 600,
          }}
        >
          {config.label}
        </span>
        {durationMs != null && (
          <span style={{ fontSize: 10, color: colors.textSecondary, marginLeft: 'auto' }}>
            {(durationMs / 1000).toFixed(1)}s
          </span>
        )}
        {(input || output || error) && (
          <span style={{ fontSize: 10, color: colors.textSecondary, marginLeft: 'auto' }}>
            {expanded ? '▲' : '▼'}
          </span>
        )}
      </div>

      {expanded && input && Object.keys(input).length > 0 && (
        <div style={{ marginTop: 6 }}>
          <div style={{ fontSize: 10, color: colors.textSecondary, marginBottom: 2 }}>Input:</div>
          <pre
            style={{
              margin: 0,
              fontSize: 11,
              background: '#fff',
              padding: '6px 8px',
              borderRadius: 4,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 120,
              overflow: 'auto',
              border: `1px solid ${colors.border}`,
            }}
          >
            {JSON.stringify(input, null, 1).slice(0, 500)}
          </pre>
        </div>
      )}

      {expanded && output != null && (
        <div style={{ marginTop: 6 }}>
          <div style={{ fontSize: 10, color: colors.textSecondary, marginBottom: 2 }}>Output:</div>
          <pre
            style={{
              margin: 0,
              fontSize: 11,
              background: '#fff',
              padding: '6px 8px',
              borderRadius: 4,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 120,
              overflow: 'auto',
              border: `1px solid ${colors.border}`,
            }}
          >
            {typeof output === 'string' ? output : JSON.stringify(output, null, 1).slice(0, 500)}
          </pre>
        </div>
      )}

      {expanded && error && (
        <div style={{ marginTop: 6 }}>
          <div style={{ fontSize: 10, color: colors.red, marginBottom: 2 }}>Error:</div>
          <pre
            style={{
              margin: 0,
              fontSize: 11,
              background: '#fff',
              padding: '6px 8px',
              borderRadius: 4,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 120,
              overflow: 'auto',
              border: `1px solid ${colors.red}`,
              color: colors.red,
            }}
          >
            {error}
          </pre>
        </div>
      )}
    </div>
  )
}
