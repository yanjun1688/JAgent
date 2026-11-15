import { useEffect, useRef, useState } from 'react'
import { ChatCircleDots, Plus } from '@phosphor-icons/react'
import { Led } from './Led'
import { MetricStrip } from './MetricStrip'
import { runStatusLabel, runStatusTone } from '../lib/kernel'
import type { SessionSummary } from '../lib/api'
import { cx } from '../lib/cx'

interface HeaderProps {
  status: string | null
  /** null until the health probe answers. */
  online: boolean | null
  sessions: SessionSummary[]
  sessionId: string
  onSelectSession: (id: string) => void
  onCreateSession: () => void
}

function sessionTitle(session: SessionSummary | undefined): string {
  if (!session) return ''
  return session.title || `对话 ${session.session_id}`
}

/**
 * City Pop wordmark + global switches. The health lamp is a fact (G4), the
 * session picker is D-034, the metric strip is D-031 S2 — none of it invents
 * state the runtime does not report.
 */
export function Header({
  status,
  online,
  sessions,
  sessionId,
  onSelectSession,
  onCreateSession,
}: HeaderProps) {
  const tone = runStatusTone(status)
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const current = sessions.find((session) => session.session_id === sessionId)

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  return (
    <header className="panel-edge relative z-40 shrink-0 border-b border-night-500 bg-night-800/90">
      <div className="relative z-10 mx-auto flex w-full max-w-[1800px] items-center gap-4 px-4 py-2.5 sm:px-6">
        <div className="flex shrink-0 items-center gap-2.5">
          <Led tone="live" size="md" breathe={online !== false} />
          <span className="chrome animate-chrome-shine font-display text-lg font-bold tracking-[0.22em]">
            JAGENT
          </span>
          <span className="hidden font-mono text-[10px] uppercase tracking-[0.18em] text-neon-magenta neon-magenta-glow sm:inline">
            v8 // NIGHT DRIVE
          </span>
        </div>

        <div className="hidden h-6 w-px bg-night-500 sm:block" />

        <div className="flex min-w-0 items-center gap-3">
          <span className="flex shrink-0 items-center gap-2 border border-night-500 bg-night-700 px-2 py-1">
            <Led
              tone={tone}
              size="xs"
              breathe={status === 'running' || status === 'waiting_human' || status === 'planning'}
            />
            <span className="text-xs text-text-secondary">[{runStatusLabel(status)}]</span>
          </span>
          <span className="hidden truncate text-xs text-text-tertiary md:inline">
            不可逆的操作会先停下来，等你确认
          </span>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-2 sm:gap-3">
          <span
            className={cx(
              'hidden border px-2 py-1 font-mono text-xs sm:inline',
              online === true && 'border-led-ok/40 text-led-ok',
              online === false && 'border-led-bad/50 text-led-bad',
              online === null && 'border-night-500 text-text-tertiary',
            )}
            title={
              online === true
                ? '已连上本地服务'
                : online === false
                  ? '连不上本地服务：先启动 python -m jagent.api'
                  : '正在探测服务'
            }
          >
            {online === true ? 'LIVE' : online === false ? 'OFFLINE' : '…'}
          </span>

          <MetricStrip sessionId={sessionId} />

          <div ref={menuRef} className="relative">
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              title="切换 / 新建对话（D-034 多对话）"
              className={cx(
                'flex max-w-[220px] items-center gap-1.5 border px-2 py-1 text-xs',
                'transition-colors duration-300 ease-base',
                open
                  ? 'border-neon-cyan/70 text-text-primary'
                  : 'border-night-500 text-text-secondary hover:border-neon-cyan/60 hover:text-text-primary',
              )}
            >
              <ChatCircleDots size={13} aria-hidden />
              <span className="truncate">{sessionTitle(current) || sessionId}</span>
            </button>

            {open ? (
              <div className="animate-neon-in absolute right-0 top-full z-50 mt-1 w-[280px] border border-night-500 bg-night-800 shadow-2xl">
                <div className="max-h-[280px] overflow-y-auto scroll-thin">
                  {sessions.length === 0 ? (
                    <p className="px-3 py-4 text-center text-xs text-text-tertiary">
                      还没有对话记录；发一条消息后这里会出现当前对话。
                    </p>
                  ) : (
                    <ul>
                      {sessions.map((session) => (
                        <li key={session.session_id} className="border-b border-night-600 last:border-0">
                          <button
                            type="button"
                            onClick={() => {
                              setOpen(false)
                              onSelectSession(session.session_id)
                            }}
                            className={cx(
                              'flex w-full flex-col gap-0.5 px-3 py-2 text-left transition-colors duration-300 ease-base',
                              session.session_id === sessionId ? 'bg-night-600' : 'hover:bg-night-600/60',
                            )}
                          >
                            <span className="flex items-center gap-2">
                              <Led
                                tone={session.session_id === sessionId ? 'flow' : 'off'}
                                size="xs"
                              />
                              <span className="min-w-0 flex-1 truncate text-xs text-text-primary">
                                {sessionTitle(session)}
                              </span>
                              <span className="shrink-0 font-mono text-[10px] tabular-nums text-text-tertiary">
                                {session.turns}轮/{session.runs}run
                              </span>
                            </span>
                            <span className="pl-4 font-mono text-[10px] text-text-tertiary">
                              {session.session_id}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false)
                    onCreateSession()
                  }}
                  className="flex w-full items-center gap-2 border-t border-night-500 px-3 py-2 text-xs text-neon-cyan transition-colors duration-300 ease-base hover:bg-neon-cyan/10"
                >
                  <Plus size={13} aria-hidden />
                  新建对话
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </header>
  )
}
