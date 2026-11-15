import { useEffect, useRef, useState } from 'react'
import { Crosshair } from '@phosphor-icons/react'
import { api, ApiError, type RunRow, type RunView, type ServerEvent, type StepView } from '../lib/api'
import { eventDetail, eventLabel, eventTone, LANES, laneOf, runStatusLabel, runStatusTone, type Lane, type Tone } from '../lib/kernel'
import { cx } from '../lib/cx'
import { Led } from './Led'
import { Section } from './Section'
import { Topology } from './Topology'

interface DataFlowPanelProps {
  runId: string | null
  /** Terminal runs get one last fetch, then the timer stops. */
  live: boolean
  run: RunView | null
  runs: RunRow[]
  busy: boolean
  onSelectRun: (id: string) => void
  onVerify: () => void
}

const POLL_MS = 1000
const KEEP = 80

const LANE_TONE: Record<string, Tone> = {
  turn: 'flow',
  model: 'human',
  plan: 'warn',
  step: 'ok',
  gate: 'human',
  run: 'flow',
  mem: 'ok',
}

function timeOf(at: number): string {
  const date = new Date(at * 1000)
  const pad = (value: number) => value.toString().padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

interface LaneState {
  count: number
  lastSeq: number
  lastTone: Tone
}

const EMPTY_LANES: Record<string, LaneState> = Object.fromEntries(
  LANES.map((lane) => [lane.id, { count: 0, lastSeq: 0, lastTone: 'off' as Tone }]),
)

/**
 * Incremental event poll driving the lane bus. The timer is chained (not
 * `setInterval`) so two slow reads can never overlap; `next_seq` is the only
 * cursor this panel keeps.
 */
function useEventStream(runId: string | null, live: boolean) {
  const [events, setEvents] = useState<ServerEvent[]>([])
  const [lanes, setLanes] = useState(EMPTY_LANES)
  const [error, setError] = useState<string | null>(null)
  const cursorRef = useRef(0)
  const runRef = useRef<string | null>(null)

  useEffect(() => {
    if (runRef.current !== runId) {
      runRef.current = runId
      cursorRef.current = 0
      setEvents([])
      setLanes(EMPTY_LANES)
      setError(null)
    }
    if (!runId) return

    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const page = await api.pollEvents(runId, cursorRef.current)
        if (cancelled) return
        cursorRef.current = page.next_seq
        setError(null)
        if (page.events.length > 0) {
          setEvents((current) => [...current, ...page.events].slice(-KEEP))
          setLanes((current) => {
            const next = { ...current }
            for (const event of page.events) {
              const lane = next[laneOf(event.type)] ?? { count: 0, lastSeq: 0, lastTone: 'off' as Tone }
              next[laneOf(event.type)] = {
                count: lane.count + 1,
                lastSeq: event.seq,
                lastTone: eventTone(event.type),
              }
            }
            return next
          })
        }
      } catch (caught) {
        if (cancelled) return
        setError(caught instanceof ApiError ? caught.detail : '读取数据流失败，正在重试。')
      }
      if (!cancelled && live) {
        timer = window.setTimeout(tick, POLL_MS)
      }
    }

    void tick()

    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [runId, live])

  return { events, lanes, error }
}

function LaneRow({ lane, state }: { lane: Lane; state: LaneState }) {
  const tone: Tone = state.lastSeq === 0 ? 'off' : (LANE_TONE[lane.id] ?? 'flow')
  const flashing = state.lastSeq > 0
  return (
    <li
      key={state.lastSeq}
      className={cx(
        'flex items-center gap-2 border border-night-600 bg-night-900/70 px-2 py-1.5',
        flashing && 'animate-lane-flash',
      )}
    >
      <Led tone={state.lastTone === 'off' ? tone : state.lastTone} size="xs" flash={state.lastSeq || undefined} />
      <span className="w-12 shrink-0 font-display text-[10px] font-medium tracking-[0.18em] text-text-secondary">
        {lane.label}
      </span>
      <span className="text-[11px] text-text-tertiary">{lane.sub}</span>
      <span className="ml-auto font-mono text-[10px] tabular-nums text-text-tertiary">
        ×{state.count}
      </span>
    </li>
  )
}

/**
 * Left column: the data-flow bus. Events pulse through seven lanes; the plan
 * itself is the step topology below; history and the audit view hang off it.
 */
export function DataFlowPanel({ runId, live, run, runs, busy, onSelectRun, onVerify }: DataFlowPanelProps) {
  const { events, lanes, error } = useEventStream(runId, live)
  const recent = [...events].reverse().slice(0, 8)

  return (
    <aside
      aria-label="数据流"
      className="hidden min-h-0 w-[300px] shrink-0 flex-col overflow-y-auto border-r border-night-500 bg-night-800/60 px-4 py-4 scroll-thin xl:flex 2xl:w-[340px]"
    >
      {/* 包一层 gap 容器：Section 不能作为滚动容器的直接 flex 子项被压缩，
          否则内容会互相重叠（旧右栏同款结构先例）。 */}
      <div className="flex flex-col gap-5">
      <Section
        title="数据流"
        code="flow bus"
        right={
          runId ? (
            <span className="font-mono text-[10px] tabular-nums text-text-tertiary">
              {events.length} 条
            </span>
          ) : null
        }
      >
        <ul className="flex flex-col gap-1">
          {LANES.map((lane) => (
            <LaneRow key={lane.id} lane={lane} state={lanes[lane.id]} />
          ))}
        </ul>

        <div className="mt-3 border border-night-600 bg-night-900/70">
          <div className="flex items-center justify-between border-b border-night-600 px-2 py-1.5">
            <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
              tape
            </span>
            <span className="font-mono text-[10px] text-text-tertiary">
              {runId ? '1s 增量' : '待机'}
            </span>
          </div>
          {!runId ? (
            <p className="px-3 py-5 text-center text-xs leading-relaxed text-text-tertiary">
              说一句话开始任务，
              <br />
              事件会在这里流过。
            </p>
          ) : recent.length === 0 ? (
            <p className="px-3 py-5 text-center text-xs text-text-tertiary">
              {error ?? '任务刚开始，还没有事件'}
            </p>
          ) : (
            <ul className="flex flex-col">
              {recent.map((event) => (
                <li
                  key={event.seq}
                  className="animate-neon-in flex items-start gap-2 border-b border-night-600/60 px-2 py-1.5 last:border-0"
                >
                  <span className="mt-1.5 shrink-0">
                    <Led tone={eventTone(event.type)} size="xs" flash={event.seq} />
                  </span>
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span className="text-xs text-text-primary">{eventLabel(event.type)}</span>
                    {eventDetail(event.type, event.payload) ? (
                      <span className="min-w-0 break-words font-mono text-[10px] leading-relaxed text-text-tertiary [overflow-wrap:anywhere]">
                        {eventDetail(event.type, event.payload)}
                      </span>
                    ) : null}
                  </div>
                  <span className="shrink-0 font-mono text-[9px] tabular-nums text-text-tertiary">
                    {timeOf(event.at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Section>

      <Section
        title="当前任务"
        code="run"
        right={
          runId ? (
            <button
              type="button"
              onClick={onVerify}
              title="验证对账：声称 vs 证据（D-032）"
              className="flex items-center gap-1 border border-neon-violet/50 px-1.5 py-0.5 font-mono text-[10px] text-led-human transition-colors duration-300 ease-base hover:bg-neon-violet/10"
            >
              <Crosshair size={11} aria-hidden />
              验证
            </button>
          ) : null
        }
      >
        <div className="border border-night-500 bg-night-700 px-3 py-3">
          <div className="flex items-center gap-2">
            <Led tone={runStatusTone(run?.status)} size="sm" breathe={live && Boolean(runId)} />
            <span className="text-sm text-text-primary">{runStatusLabel(run?.status)}</span>
            {run ? (
              <span className="ml-auto font-mono text-[10px] tabular-nums text-text-tertiary">
                {run.steps.length} 步
              </span>
            ) : null}
          </div>

          {run?.summary ? (
            <p className="mt-2 text-xs leading-relaxed text-text-secondary">{run.summary}</p>
          ) : (
            <p className="mt-2 text-xs leading-relaxed text-text-tertiary">
              {run ? '这条任务还没有可展示的总结。' : '还没有任务。在中间说一句话就能开始。'}
            </p>
          )}

          {runId ? <p className="mt-2 font-mono text-[10px] text-text-tertiary">编号 {runId}</p> : null}
        </div>

        <Topology steps={run?.steps ?? []} />
      </Section>

      <Section
        title="最近任务"
        code="history"
        right={
          <span className="font-mono text-[10px] tabular-nums text-text-tertiary">{runs.length}</span>
        }
      >
        <div className="border border-night-500 bg-night-700">
          {runs.length === 0 ? (
            <p className="px-3 py-4 text-center text-xs text-text-tertiary">还没有历史任务。</p>
          ) : (
            <ul>
              {runs.map((row) => (
                <li key={row.run_id} className="border-b border-night-600 last:border-0">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onSelectRun(row.run_id)}
                    className={cx(
                      'flex w-full items-center gap-2 px-3 py-2 text-left',
                      'transition-colors duration-300 ease-base',
                      row.run_id === runId ? 'bg-night-600' : 'hover:bg-night-600/60',
                      busy && 'cursor-not-allowed opacity-60',
                    )}
                  >
                    <Led tone={runStatusTone(row.status)} size="xs" />
                    <span className="min-w-0 flex-1 truncate text-xs text-text-primary">
                      {row.summary || `任务 ${row.run_id.slice(-6)}`}
                    </span>
                    <span className="shrink-0 font-mono text-[10px] text-text-tertiary">
                      {runStatusLabel(row.status)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Section>
      </div>
    </aside>
  )
}

export type { StepView }
