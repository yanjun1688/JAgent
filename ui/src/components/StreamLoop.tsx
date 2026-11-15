import { useEffect, useRef, useState } from 'react'
import { api, ApiError, type ServerEvent } from '../lib/api'
import { eventDetail, eventLabel, eventTone } from '../lib/kernel'
import { Section } from './Section'
import { Led } from './Led'
import { cx } from '../lib/cx'

interface StreamLoopProps {
  runId: string | null
  /** Terminal runs get one last fetch, then the timer stops. */
  live: boolean
}

const POLL_MS = 1000
const KEEP = 60

function timeOf(at: number): string {
  const date = new Date(at * 1000)
  const pad = (value: number) => value.toString().padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/**
 * Incremental event poll. The timer is chained (not `setInterval`) so two slow
 * reads can never overlap; `next_seq` is the only cursor the panel keeps.
 */
export function StreamLoop({ runId, live }: StreamLoopProps) {
  const [events, setEvents] = useState<ServerEvent[]>([])
  const [count, setCount] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const cursorRef = useRef(0)
  const runRef = useRef<string | null>(null)

  useEffect(() => {
    // A terminal run flips `live`; only a real run change resets the tape.
    if (runRef.current !== runId) {
      runRef.current = runId
      cursorRef.current = 0
      setEvents([])
      setCount(0)
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
          setCount((current) => current + page.events.length)
          setEvents((current) => [...current, ...page.events].slice(-KEEP))
        }
      } catch (caught) {
        if (cancelled) return
        setError(caught instanceof ApiError ? caught.detail : '读取过程失败，正在重试。')
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

  const recent = events.slice(-6).reverse()
  const latest = events[events.length - 1]

  return (
    <Section
      title="过程"
      code="events · 1s"
      right={
        <span className="flex items-center gap-2 font-mono text-xs text-text-tertiary">
          <Led tone={latest ? eventTone(latest.type) : 'off'} size="xs" flash={latest?.seq} />
          {count} 条
        </span>
      }
    >
      <div className="flex min-h-0 flex-col border border-ink-500 bg-ink-700">
        <div className="flex shrink-0 items-center justify-between border-b border-ink-600 px-3 py-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
            最近发生的
          </span>
          <span className="font-mono text-[10px] text-text-tertiary">
            {runId ? '每秒自动刷新' : '待机'}
          </span>
        </div>

        {!runId ? (
          <div className="flex flex-col items-center justify-center gap-1 px-4 py-8 text-center">
            <p className="text-sm text-text-secondary">还没有正在看的任务</p>
            <p className="text-xs leading-relaxed text-text-tertiary">
              说一句话开始一个任务，它的过程会实时出现在这里。
            </p>
          </div>
        ) : recent.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-1 px-4 py-8 text-center">
            <p className="text-sm text-text-secondary">{error ?? '任务刚开始，还没有过程记录'}</p>
            <p className="text-xs leading-relaxed text-text-tertiary">
              {error ? '连上服务后会自动继续。' : '第一步开始后，这里会出现第一条记录。'}
            </p>
          </div>
        ) : (
          <ul className="flex flex-col">
            {recent.map((event) => (
              <li
                key={event.seq}
                className="flex items-start gap-2 border-b border-ink-600/60 px-3 py-2 last:border-0"
              >
                <span className="mt-1.5 shrink-0">
                  <Led tone={eventTone(event.type)} size="xs" flash={event.seq} />
                </span>
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="text-xs text-text-primary">{eventLabel(event.type)}</span>
                  {eventDetail(event.type, event.payload) ? (
                    <span
                      className={cx(
                        'min-w-0 break-words font-mono text-[10px] leading-relaxed text-text-tertiary',
                        '[overflow-wrap:anywhere]',
                      )}
                    >
                      {eventDetail(event.type, event.payload)}
                    </span>
                  ) : null}
                </div>
                <span className="shrink-0 font-mono text-[10px] tabular-nums text-text-tertiary">
                  {timeOf(event.at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  )
}
