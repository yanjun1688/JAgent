import { useEffect, useRef, useState } from 'react'
import { Pause, Play, TerminalWindow } from '@phosphor-icons/react'
import { api, ApiError, type LogEntry } from '../lib/api'
import { logLevelTone } from '../lib/kernel'
import { cx } from '../lib/cx'
import { Led } from './Led'

const POLL_MS = 1000
const KEEP = 600

type LevelFilter = 'all' | 'warn' | 'error'

const LEVEL_ORDER: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
}

function timeOf(at: number): string {
  const date = new Date(at * 1000)
  const pad = (value: number) => value.toString().padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

const LEVEL_TEXT: Record<string, string> = {
  INFO: 'text-led-flow',
  WARNING: 'text-led-warn',
  ERROR: 'text-led-bad',
  CRITICAL: 'text-led-bad',
  DEBUG: 'text-text-tertiary',
}

/**
 * Right column: the backend's own log stream (D-036) — uvicorn access lines,
 * service lifecycle, model calls, exception stacks. Operational telemetry,
 * NOT the event stream: the two never merge on screen.
 */
export function LogsPanel() {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<LevelFilter>('all')
  const [paused, setPaused] = useState(false)
  const cursorRef = useRef(-1)
  const boxRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  useEffect(() => {
    if (paused) return
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const page = await api.pollLogs(cursorRef.current)
        if (cancelled) return
        cursorRef.current = page.next
        setError(null)
        if (page.logs.length > 0) {
          setLogs((current) => [...current, ...page.logs].slice(-KEEP))
        }
      } catch (caught) {
        if (cancelled) return
        setError(caught instanceof ApiError ? caught.detail : '读取日志失败，正在重试。')
      }
      timer = window.setTimeout(tick, POLL_MS)
    }

    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [paused])

  // Stick to the bottom like a real console until the user scrolls up.
  useEffect(() => {
    const box = boxRef.current
    if (box && stickRef.current) {
      box.scrollTop = box.scrollHeight
    }
  }, [logs])

  const onScroll = () => {
    const box = boxRef.current
    if (!box) return
    stickRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 32
  }

  const threshold =
    filter === 'error' ? LEVEL_ORDER.ERROR : filter === 'warn' ? LEVEL_ORDER.WARNING : 0
  const visible = logs.filter((entry) => (LEVEL_ORDER[entry.level] ?? 0) >= threshold)
  const errorCount = logs.filter((entry) => (LEVEL_ORDER[entry.level] ?? 0) >= LEVEL_ORDER.ERROR)
    .length
  const warnCount = logs.filter((entry) => (LEVEL_ORDER[entry.level] ?? 0) >= LEVEL_ORDER.WARNING)
    .length

  return (
    <aside
      aria-label="后端日志"
      className="hidden min-h-0 w-[420px] shrink-0 flex-col border-l border-night-500 bg-night-900/80 lg:flex 2xl:w-[480px]"
    >
      <div className="panel-edge flex shrink-0 items-center gap-2 border-b border-night-500 px-4 py-2.5">
        <TerminalWindow size={14} className="text-neon-cyan" aria-hidden />
        <span className="font-display text-[13px] font-medium uppercase tracking-[0.18em] text-text-primary">
          后端日志
        </span>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-text-tertiary">
          stdout · D-036
        </span>
        <span className="ml-auto flex items-center gap-2 font-mono text-[10px] tabular-nums text-text-tertiary">
          {warnCount > 0 ? <span className="text-led-warn">!{warnCount}</span> : null}
          {errorCount > 0 ? <span className="text-led-bad">✕{errorCount}</span> : null}
          <button
            type="button"
            onClick={() => setPaused((value) => !value)}
            title={paused ? '继续滚动' : '暂停拉取'}
            className={cx(
              'border p-1 transition-colors duration-300 ease-base',
              paused
                ? 'border-led-warn/60 text-led-warn'
                : 'border-night-500 text-text-tertiary hover:border-neon-cyan/60 hover:text-text-primary',
            )}
          >
            {paused ? <Play size={11} aria-hidden /> : <Pause size={11} aria-hidden />}
          </button>
        </span>
      </div>

      <div className="flex shrink-0 items-center gap-1.5 border-b border-night-600 px-4 py-1.5">
        {(
          [
            ['all', '全部'],
            ['warn', 'WARN+'],
            ['error', 'ERROR'],
          ] as Array<[LevelFilter, string]>
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setFilter(value)}
            className={cx(
              'border px-2 py-0.5 font-mono text-[10px] transition-colors duration-300 ease-base',
              filter === value
                ? 'border-neon-cyan/70 text-neon-cyan'
                : 'border-night-500 text-text-tertiary hover:text-text-secondary',
            )}
          >
            {label}
          </button>
        ))}
        <span className="ml-auto font-mono text-[10px] tabular-nums text-text-tertiary">
          {visible.length} 行
        </span>
      </div>

      <div
        ref={boxRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto px-2 py-2 scroll-thin"
      >
        {visible.length === 0 ? (
          <p className="px-3 py-8 text-center font-mono text-xs leading-relaxed text-text-tertiary">
            {error ?? '等待服务日志……'}
            <br />
            <span className="text-[10px]">
              每条 HTTP 请求、模型调用与异常都会流经这里。
            </span>
          </p>
        ) : (
          <ul className="flex flex-col font-mono text-[11px] leading-relaxed">
            {visible.map((entry) => {
              const tone = logLevelTone(entry.level)
              return (
                <li
                  key={entry.id}
                  className={cx(
                    'animate-neon-in flex items-start gap-2 rounded-none px-2 py-0.5 hover:bg-night-700/70',
                    entry.level === 'ERROR' && 'bg-led-bad/5',
                  )}
                >
                  <span className="mt-1.5 shrink-0">
                    <Led tone={tone} size="xs" flash={entry.id} />
                  </span>
                  <span className="shrink-0 tabular-nums text-text-tertiary">{timeOf(entry.at)}</span>
                  <span className={cx('shrink-0 uppercase', LEVEL_TEXT[entry.level] ?? 'text-text-tertiary')}>
                    {entry.level}
                  </span>
                  <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-text-secondary [overflow-wrap:anywhere]">
                    {entry.message}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </aside>
  )
}
