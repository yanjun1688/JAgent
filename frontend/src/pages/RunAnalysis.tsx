import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getRunAnalysis, getRunTimeline, getRunToolTraces } from '../api/analysis-client'
import type { RunAnalysisSummary, ParsedEventDetail, ToolTraceItem } from '../api/analysis-types'
import {
  card, sectionTitle, colors, fmt, formatDuration, formatTime,
  statusBadge, eventTypeBadge,
} from '../api/analysis-styles'
import TraceTree from '../components/TraceTree'
import ChatDrawer from '../components/ChatDrawer'

export default function RunAnalysis() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [summary, setSummary] = useState<RunAnalysisSummary | null>(null)
  const [timeline, setTimeline] = useState<ParsedEventDetail[]>([])
  const [traces, setTraces] = useState<ToolTraceItem[]>([])
  const [cursor, setCursor] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingSummary, setLoadingSummary] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const loadingRef = useRef(false)
  const [expandedSeqs, setExpandedSeqs] = useState<Set<number>>(new Set())
  const [drawerOpen, setDrawerOpen] = useState(false)

  useEffect(() => {
    if (!runId) return
    setLoadingSummary(true)
    Promise.all([
      getRunAnalysis(runId).then((s) => { setSummary(s); return s }),
      getRunTimeline(runId, 50, 0).then((t) => {
        setTimeline(t.timeline)
        setCursor(t.next_cursor)
        setHasMore(t.has_more)
      }),
      getRunToolTraces(runId).then((t) => setTraces(t.tool_traces)),
    ]).catch(() => navigate('/analysis')).finally(() => setLoadingSummary(false))
  }, [runId, navigate])

  const loadMore = useCallback(async () => {
    if (!runId || !hasMore || loadingRef.current) return
    loadingRef.current = true
    setLoadingMore(true)
    try {
      const t = await getRunTimeline(runId, 50, cursor)
      setTimeline((prev) => [...prev, ...t.timeline])
      setCursor(t.next_cursor)
      setHasMore(t.has_more)
    } finally {
      loadingRef.current = false
      setLoadingMore(false)
    }
  }, [runId, cursor, hasMore])

  const toggleExpand = (seq: number) => {
    setExpandedSeqs((prev) => {
      const next = new Set(prev)
      if (next.has(seq)) next.delete(seq)
      else next.add(seq)
      return next
    })
  }

  const renderPayload = (e: ParsedEventDetail) => {
    if (e.event_type === 'RunStarted') return `Intent: ${e.payload.intent}`
    if (e.event_type === 'AgentThought') {
      return (e.payload.thought as string)?.slice(0, 200) || JSON.stringify(e.payload).slice(0, 200)
    }
    if (e.tool_name) {
      const parts = [e.tool_name]
      if (e.error) parts.push(`✗ ${e.error}`)
      else if (e.duration_ms) parts.push(`(${formatDuration(e.duration_ms)})`)
      return parts.join(' ')
    }
    return ''
  }

  if (loadingSummary) {
    return <p style={{ color: colors.textSecondary }}>Loading run analysis...</p>
  }
  if (!summary) {
    return <p>Run not found</p>
  }

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <button
            onClick={() => navigate('/analysis')}
            style={{ background: 'none', border: 'none', color: colors.blue, cursor: 'pointer', fontSize: 14, padding: 0 }}
          >
            ← Dashboard
          </button>
          <span style={{ margin: '0 8px', color: colors.border }}>|</span>
          <button
            onClick={() => navigate(`/runs/${runId}`)}
            style={{ background: 'none', border: 'none', color: colors.blue, cursor: 'pointer', fontSize: 14, padding: 0 }}
          >
            Classic View
          </button>
        </div>

        <button
          onClick={() => setDrawerOpen(true)}
          style={{
            padding: '8px 18px',
            background: '#6c5ce7',
            color: '#fff',
            border: 'none',
            borderRadius: 8,
            cursor: 'pointer',
            fontSize: 13,
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          <span>💬</span>
          <span>Agent Chat</span>
        </button>
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 24 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>Run {summary.run_id}</h1>
          <p style={{ color: colors.textSecondary, margin: '4px 0', fontSize: 14 }}>{summary.intent}</p>
        </div>
        <div style={{ ...statusBadge(summary.status), fontSize: 13, padding: '4px 14px' }}>
          {summary.status}
        </div>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 24 }}>
        <div style={{ ...card, padding: '14px 18px' }}>
          <div style={{ fontSize: 11, color: colors.textSecondary, textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Events</div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>{fmt(summary.event_count)}</div>
        </div>
        <div style={{ ...card, padding: '14px 18px' }}>
          <div style={{ fontSize: 11, color: colors.textSecondary, textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Tool Calls</div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>{fmt(summary.tool_trace_count)}</div>
        </div>
        <div style={{ ...card, padding: '14px 18px' }}>
          <div style={{ fontSize: 11, color: colors.textSecondary, textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Tokens</div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>{fmt(summary.total_tokens)}</div>
        </div>
        <div style={{ ...card, padding: '14px 18px' }}>
          <div style={{ fontSize: 11, color: colors.textSecondary, textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Duration</div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>{formatDuration(summary.total_duration_ms)}</div>
        </div>
        <div style={{ ...card, padding: '14px 18px' }}>
          <div style={{ fontSize: 11, color: colors.textSecondary, textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Guardrails</div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2, color: summary.guardrail_event_count > 0 ? colors.red : colors.text }}>
            {fmt(summary.guardrail_event_count)}
          </div>
        </div>
        <div style={{ ...card, padding: '14px 18px' }}>
          <div style={{ fontSize: 11, color: colors.textSecondary, textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Feedback</div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 2 }}>{fmt(summary.feedback_count)}</div>
        </div>
      </div>

      <div style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', gap: 16, alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Timeline</h2>
          <span style={{ fontSize: 12, color: colors.textSecondary }}>
            {summary.event_count} total · showing {timeline.length}
          </span>
        </div>

        <div style={{ fontFamily: 'monospace', fontSize: 13, lineHeight: 1.6 }}>
          {timeline.map((e) => (
            <div key={`${e.seq}-${e.event_type}`}>
              <div
                onClick={() => toggleExpand(e.seq)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 12px',
                  borderLeft: `3px solid ${eventTypeBadge(e.event_type).background || '#ddd'}`,
                  background: expandedSeqs.has(e.seq) ? '#f8f9fa' : '#fafafa',
                  cursor: 'pointer',
                }}
              >
                <span style={{ color: '#999', minWidth: 36, fontSize: 12 }}>#{e.seq}</span>
                <span
                  style={{
                    display: 'inline-block',
                    padding: '1px 6px',
                    borderRadius: 4,
                    fontSize: 11,
                    background: eventTypeBadge(e.event_type).background || '#eee',
                    color: '#fff',
                    fontWeight: 'bold',
                    minWidth: 100,
                    textAlign: 'center',
                  }}
                >
                  {e.event_type}
                </span>
                <span style={{ color: '#999', fontSize: 12, minWidth: 65 }}>{formatTime(e.created_at).split(',')[0]}</span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#333' }}>
                  {renderPayload(e)}
                </span>
                <span style={{ color: '#ccc', fontSize: 11 }}>{expandedSeqs.has(e.seq) ? '▲' : '▼'}</span>
              </div>
              {expandedSeqs.has(e.seq) && (
                <div style={{ padding: '10px 16px', background: '#f8f9fa', borderLeft: '3px solid #ddd', fontSize: 12 }}>
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 300, overflow: 'auto' }}>
                    {JSON.stringify(e.payload, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ))}
        </div>

        {hasMore && (
          <div style={{ textAlign: 'center', marginTop: 12 }}>
            <button
              onClick={loadMore}
              disabled={loadingMore}
              style={{
                padding: '6px 20px',
                background: colors.bg,
                border: `1px solid ${colors.border}`,
                borderRadius: 6,
                cursor: loadingMore ? 'default' : 'pointer',
                fontSize: 12,
                color: colors.textSecondary,
              }}
            >
              {loadingMore ? 'Loading...' : `Load more (${summary.event_count - timeline.length})`}
            </button>
          </div>
        )}
      </div>

      <div style={{ marginBottom: 24 }}>
        <TraceTree traces={traces} />
      </div>

      {drawerOpen && runId && (
        <div style={{ position: 'fixed', top: 0, right: 0, bottom: 0, width: 480, zIndex: 1000, boxShadow: '-4px 0 24px rgba(0,0,0,0.12)' }}>
          <div style={{ position: 'absolute', top: 12, right: 12, zIndex: 10 }}>
            <button onClick={() => setDrawerOpen(false)} style={{ background: '#fff', border: `1px solid ${colors.border}`, borderRadius: 6, cursor: 'pointer', fontSize: 16, padding: '4px 8px', color: '#999' }}>✕</button>
          </div>
          <ChatDrawer initialRunId={runId} />
        </div>
      )}
    </div>
  )
}
