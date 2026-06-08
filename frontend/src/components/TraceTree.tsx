import React, { useState } from 'react'
import type { ToolTraceItem } from '../api/analysis-types'
import { card, colors, fmt, formatDuration, badge } from '../api/analysis-styles'

function StatusBadge({ status }: { status: string }) {
  const bg = status === 'completed' ? colors.success
    : status === 'failed' ? colors.red
    : status === 'timeout' ? colors.red
    : status === 'guardrail_blocked' ? '#ff7043'
    : colors.gray
  return <span style={{ ...badge(bg), fontSize: 10 }}>{status}</span>
}

function TraceRow({ trace, defaultOpen }: { trace: ToolTraceItem; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen || false)

  return (
    <div style={{ border: `1px solid ${colors.border}`, borderRadius: 8, overflow: 'hidden' }}>
      <div
        onClick={() => setOpen(!open)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '10px 14px',
          cursor: 'pointer',
          background: open ? '#f8f9fa' : '#fff',
          borderBottom: open ? `1px solid ${colors.border}` : 'none',
        }}
      >
        <span style={{ color: '#ccc', fontSize: 11, transition: 'transform 0.15s', transform: open ? 'rotate(90deg)' : '' }}>
          ▶
        </span>
        <span style={{ fontWeight: 600, fontSize: 13, minWidth: 100 }}>{trace.tool_name}</span>
        <StatusBadge status={trace.status} />
        <span style={{ fontSize: 12, color: colors.textSecondary }}>
          #{trace.called_seq}{trace.completed_seq ? ` → #${trace.completed_seq}` : ''}
        </span>
        {trace.duration_ms > 0 && (
          <span style={{ fontSize: 12, color: colors.textSecondary }}>{formatDuration(trace.duration_ms)}</span>
        )}
        {trace.error && (
          <span style={{ fontSize: 12, color: colors.red, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {trace.error}
          </span>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <span style={{ ...badge('#eee', '#666'), fontSize: 10 }}>{trace.tool_call_id.slice(0, 8)}</span>
          {trace.retryable.eligible && (
            <button
              style={{
                padding: '2px 10px',
                background: colors.orange,
                color: '#fff',
                border: 'none',
                borderRadius: 4,
                cursor: 'pointer',
                fontSize: 11,
                fontWeight: 600,
              }}
              title={trace.retryable.ineligible_reason || ''}
            >
              Retry
            </button>
          )}
        </div>
      </div>

      {open && (
        <div style={{ padding: '12px 16px', background: '#fafafa', fontSize: 12 }}>
          {trace.input && Object.keys(trace.input).length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontWeight: 600, color: colors.textSecondary, marginBottom: 4, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Input</div>
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all', background: '#fff', padding: 8, borderRadius: 4, border: `1px solid ${colors.border}` }}>
                {JSON.stringify(trace.input, null, 2)}
              </pre>
            </div>
          )}
          {trace.output !== null && trace.output !== undefined && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontWeight: 600, color: colors.textSecondary, marginBottom: 4, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Output</div>
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all', background: '#fff', padding: 8, borderRadius: 4, border: `1px solid ${colors.border}` }}>
                {typeof trace.output === 'string' ? trace.output : JSON.stringify(trace.output, null, 2)}
              </pre>
            </div>
          )}
          {trace.error && (
            <div>
              <div style={{ fontWeight: 600, color: colors.red, marginBottom: 4, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Error</div>
              <div style={{ color: colors.red, background: colors.redLight, padding: '6px 10px', borderRadius: 4 }}>{trace.error}</div>
            </div>
          )}
          {trace.guardrail_id && (
            <div>
              <div style={{ fontWeight: 600, color: '#ff7043', marginBottom: 4, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Guardrail</div>
              <div style={{ color: '#ff7043', background: '#fff3e0', padding: '6px 10px', borderRadius: 4 }}>
                {trace.guardrail_id}: {trace.guardrail_reason}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface Props {
  traces: ToolTraceItem[]
}

export default function TraceTree({ traces }: Props) {
  const [allOpen, setAllOpen] = useState(false)

  if (traces.length === 0) {
    return <div style={{ textAlign: 'center', color: colors.textSecondary, fontSize: 13, padding: 40 }}>No tool traces available.</div>
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ fontWeight: 600, fontSize: 15 }}>Tool Traces ({traces.length})</div>
        <button
          onClick={() => setAllOpen(!allOpen)}
          style={{
            padding: '4px 12px',
            background: colors.bg,
            border: `1px solid ${colors.border}`,
            borderRadius: 4,
            cursor: 'pointer',
            fontSize: 12,
            color: colors.textSecondary,
          }}
        >
          {allOpen ? 'Collapse All' : 'Expand All'}
        </button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {traces.map((trace) => (
          <TraceRow key={trace.tool_call_id} trace={trace} defaultOpen={allOpen} />
        ))}
      </div>
    </div>
  )
}
