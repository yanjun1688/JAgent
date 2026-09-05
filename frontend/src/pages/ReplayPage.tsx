import { useEffect, useState } from 'react'
import { motion } from 'motion/react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, FlaskConical, Loader2, X } from 'lucide-react'
import {
  getReplayDiff,
  getReplayRunMeta,
  getReplayState,
  getReplayTimeline,
} from '../api/replay-client'
import { cn } from '../design-system/utils/cn'
import { ReplayRunPicker } from '../components/replay/ReplayRunPicker'
import { EventTimelineList } from '../components/replay/EventTimelineList'
import { StateSnapshotPanel } from '../components/replay/StateSnapshotPanel'
import { StateDiffPanel } from '../components/replay/StateDiffPanel'
import { errorMessage, isNotFoundError, statusLabel, statusTone, toneClasses } from '../components/replay/statusColors'

type Mode = 'state' | 'diff'

export default function ReplayPage() {
  const params = useParams<{ runId?: string }>()
  const navigate = useNavigate()
  const runId = params.runId ?? null

  const [mode, setMode] = useState<Mode>('state')
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null)
  const [fromSeq, setFromSeq] = useState<number | null>(null)
  const [toSeq, setToSeq] = useState<number | null>(null)

  const metaQuery = useQuery({
    queryKey: ['replay', 'meta', runId],
    queryFn: () => getReplayRunMeta(runId as string),
    enabled: !!runId,
    retry: false,
  })
  const timelineQuery = useQuery({
    queryKey: ['replay', 'timeline', runId],
    queryFn: () => getReplayTimeline(runId as string),
    enabled: !!runId,
    retry: false,
  })
  const stateQuery = useQuery({
    queryKey: ['replay', 'state', runId, selectedSeq],
    queryFn: () => getReplayState(runId as string, selectedSeq ?? undefined),
    enabled: !!runId && mode === 'state' && selectedSeq != null,
    retry: false,
  })
  const diffReady = mode === 'diff' && fromSeq != null && toSeq != null
  const diffFrom = diffReady ? Math.min(fromSeq as number, toSeq as number) : 0
  const diffTo = diffReady ? Math.max(fromSeq as number, toSeq as number) : 0
  const diffQuery = useQuery({
    queryKey: ['replay', 'diff', runId, diffFrom, diffTo],
    queryFn: () => getReplayDiff(runId as string, diffFrom, diffTo),
    enabled: diffReady && !!runId,
    retry: false,
  })

  // Reset selection when the run changes; default to the latest point.
  useEffect(() => {
    setSelectedSeq(null)
    setFromSeq(null)
    setToSeq(null)
    setMode('state')
  }, [runId])

  useEffect(() => {
    if (mode === 'state' && selectedSeq == null && metaQuery.data?.latest_seq) {
      setSelectedSeq(metaQuery.data.latest_seq)
    }
  }, [mode, selectedSeq, metaQuery.data?.latest_seq])

  const handleSelectSeq = (seq: number) => {
    if (mode === 'state') {
      setSelectedSeq(seq)
      return
    }
    // Compare mode: first click = A (start fresh), second click = B.
    if (fromSeq == null || (fromSeq != null && toSeq != null)) {
      setFromSeq(seq)
      setToSeq(null)
    } else {
      setToSeq(seq)
    }
  }

  const openRun = (id: string) => navigate(`/replay/${encodeURIComponent(id)}`)

  const meta = metaQuery.data
  const timeline = timelineQuery.data?.timeline ?? []
  const notFound = isNotFoundError(metaQuery.error) || isNotFoundError(timelineQuery.error)

  return (
    <div className="min-h-0 flex-1 px-4 py-4">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="mx-auto flex h-full max-w-[1600px] gap-3"
      >
        {/* Left: run picker */}
        <div className="hidden min-h-0 w-72 shrink-0 flex-col overflow-hidden rounded-2xl glass-base lg:flex">
          <div className="flex items-center gap-2 border-b border-border-soft px-4 py-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-primary/20 text-accent-primary">
              <FlaskConical size={16} />
            </span>
            <div>
              <h1 className="font-display text-base font-bold text-text-primary">时间旅行调试器</h1>
              <p className="text-[10px] text-text-tertiary">只读 · 从事件流重建任意时刻状态</p>
            </div>
          </div>
          <ReplayRunPicker selectedRunId={runId} onSelect={openRun} />
        </div>

        {/* Right: inspector */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
          {!runId ? (
            <div className="flex h-full items-center justify-center rounded-2xl glass-base">
              <div className="flex flex-col items-center gap-3 text-text-muted">
                <FlaskConical size={28} />
                <p className="text-sm">从左侧选择一个 run，或粘贴 run_id 开始调试</p>
              </div>
            </div>
          ) : (
            <>
              {/* Control header */}
              <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-2xl glass-base px-4 py-3">
                <span className="font-mono text-xs text-text-secondary">{runId.slice(0, 20)}</span>
                {meta && (
                  <>
                    <span
                      className={cn(
                        'rounded-full px-2.5 py-0.5 text-xs font-medium',
                        toneClasses(statusTone(meta.status)).badge,
                      )}
                    >
                      {statusLabel(meta.status)}
                    </span>
                    <span className="text-[11px] text-text-muted">{meta.event_count} 事件</span>
                  </>
                )}

                {meta?.langfuse_trace_url ? (
                  <a
                    href={meta.langfuse_trace_url}
                    target="_blank"
                    rel="noreferrer"
                    className="ml-auto flex items-center gap-1 rounded-lg bg-accent-primary/20 px-2.5 py-1 text-xs text-accent-primary hover:bg-accent-primary/30"
                  >
                    <ExternalLink size={12} /> Langfuse
                  </a>
                ) : (
                  <span className="ml-auto text-[10px] text-text-tertiary" title="Langfuse 未配置，链接字段已预留">
                    Langfuse: 未配置
                  </span>
                )}

                {/* Mode chips */}
                <div className="flex items-center gap-1 rounded-lg bg-surface-1 p-0.5">
                  {(['state', 'diff'] as Mode[]).map((m) => (
                    <button
                      key={m}
                      onClick={() => setMode(m)}
                      className={cn(
                        'rounded-md px-3 py-1 text-xs font-medium transition-colors',
                        mode === m ? 'bg-accent-primary/20 text-accent-primary' : 'text-text-muted hover:text-text-primary',
                      )}
                    >
                      {m === 'state' ? '单时刻状态' : '对比两时刻'}
                    </button>
                  ))}
                </div>

                <button
                  onClick={() => navigate('/replay')}
                  className="flex h-7 w-7 items-center justify-center rounded-lg text-text-muted hover:bg-surface-1 hover:text-text-primary"
                  title="关闭"
                >
                  <X size={15} />
                </button>
              </div>

              {/* Timeline + detail */}
              <div className="flex min-h-0 flex-1 gap-3">
                <div className="flex w-80 shrink-0 flex-col overflow-hidden rounded-2xl glass-base">
                  <div className="flex shrink-0 items-center justify-between border-b border-border-soft px-3 py-2">
                    <span className="text-xs font-semibold text-text-secondary">事件时间线</span>
                    <span className="font-mono text-[10px] text-text-muted">
                      {mode === 'diff'
                        ? fromSeq == null
                          ? '点击选起点 A'
                          : toSeq == null
                            ? '点击选终点 B'
                            : `A #${diffFrom} → B #${diffTo}`
                        : selectedSeq != null
                          ? `时刻 #${selectedSeq}`
                          : ''}
                    </span>
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto p-2">
                    {timelineQuery.isLoading && (
                      <div className="flex items-center justify-center py-8 text-text-muted">
                        <Loader2 size={18} className="animate-spin" />
                      </div>
                    )}
                    {notFound && (
                      <p className="px-2 py-6 text-center text-xs text-status-error">
                        找不到该 run（可能不存在或属于其他租户）
                      </p>
                    )}
                    {timelineQuery.isError && !notFound && (
                      <p className="px-2 py-6 text-center text-xs text-status-error">
                        {errorMessage(timelineQuery.error)}
                      </p>
                    )}
                    {!timelineQuery.isLoading && timeline.length === 0 && !timelineQuery.isError && (
                      <p className="px-2 py-6 text-center text-xs text-text-muted">该 run 没有事件记录</p>
                    )}
                    <EventTimelineList
                      events={timeline}
                      selectedSeq={selectedSeq}
                      compareMode={mode === 'diff'}
                      fromSeq={fromSeq}
                      toSeq={toSeq}
                      onSelectSeq={handleSelectSeq}
                    />
                  </div>
                </div>

                <div className="min-h-0 min-w-0 flex-1 overflow-y-auto rounded-2xl glass-elevated px-5 py-4">
                  {mode === 'state' ? (
                    <StateSnapshotPanel
                      state={stateQuery.data ?? null}
                      isLoading={stateQuery.isLoading}
                      error={stateQuery.isError ? errorMessage(stateQuery.error) : null}
                      selectedSeq={selectedSeq}
                    />
                  ) : (
                    <StateDiffPanel
                      diff={diffQuery.data ?? null}
                      isLoading={diffQuery.isLoading}
                      error={diffQuery.isError ? errorMessage(diffQuery.error) : null}
                      hasSelection={fromSeq != null && toSeq != null}
                    />
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </motion.div>
    </div>
  )
}
