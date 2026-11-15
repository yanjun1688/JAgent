import { SidebarSimple } from '@phosphor-icons/react'
import { Led } from './Led'
import { runStatusLabel, runStatusTone } from '../lib/kernel'
import { cx } from '../lib/cx'
import type { Theme } from '../lib/theme'

interface HeaderProps {
  status: string | null
  theme: Theme
  /** null until the health probe answers. */
  online: boolean | null
  /** Whether the right-hand "过程" panel is open. */
  panelOpen: boolean
  onToggleTheme: () => void
  onTogglePanel: () => void
}

export function Header({
  status,
  theme,
  online,
  panelOpen,
  onToggleTheme,
  onTogglePanel,
}: HeaderProps) {
  const tone = runStatusTone(status)

  return (
    <header className="shrink-0 border-b border-ink-500 bg-ink-800">
      <div className="mx-auto flex w-full max-w-[1600px] items-center gap-4 px-4 py-3 sm:px-6">
        <div className="flex shrink-0 items-center gap-2">
          <Led tone="ok" size="md" breathe />
          <span className="phosphor text-base font-semibold tracking-[0.18em]">JAGENT</span>
          <span className="hidden font-mono text-xs uppercase tracking-[0.16em] text-text-tertiary sm:inline">
            // v8
          </span>
        </div>

        <div className="hidden h-6 w-px bg-ink-500 sm:block" />

        <div className="flex min-w-0 items-center gap-3">
          <span className="flex shrink-0 items-center gap-2 border border-ink-500 bg-ink-700 px-2 py-1">
            <Led tone={tone} size="xs" breathe={status === 'running' || status === 'waiting_human'} />
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
              online === false && 'border-led-bad/40 text-led-bad',
              online === null && 'border-ink-500 text-text-tertiary',
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

          <button
            type="button"
            onClick={onTogglePanel}
            aria-expanded={panelOpen}
            title={panelOpen ? '收起过程面板' : '展开过程面板'}
            className={cx(
              'flex items-center gap-1.5 border px-2 py-1 font-mono text-xs',
              'transition-colors duration-300 ease-base',
              panelOpen
                ? 'border-text-tertiary text-text-primary'
                : 'border-ink-500 text-text-secondary hover:border-text-tertiary hover:text-text-primary',
            )}
          >
            <SidebarSimple size={13} aria-hidden />
            过程
          </button>

          <button
            type="button"
            onClick={onToggleTheme}
            title="切换黑 / 白"
            aria-label={theme === 'dark' ? '切到白底' : '切到黑底'}
            className={cx(
              'border border-ink-500 px-2 py-1 font-mono text-xs text-text-secondary',
              'transition-colors duration-300 ease-base hover:border-text-tertiary hover:text-text-primary',
            )}
          >
            {theme === 'dark' ? '◐ 白' : '◑ 黑'}
          </button>
        </div>
      </div>
    </header>
  )
}
