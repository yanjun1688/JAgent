import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { PaperPlaneRight } from '@phosphor-icons/react'
import { cx } from '../lib/cx'
import type { Tone } from '../lib/kernel'

interface CommandBarProps {
  onSubmit: (text: string) => void
  busy: boolean
  /** True while an approval gate is open: the next move belongs to the gate. */
  blocked?: boolean
  notice: string | null
  noticeTone: Tone
  chips: string[]
  placeholder?: string
}

const NOTICE_COLOR: Record<Tone, string> = {
  ok: 'text-text-secondary',
  warn: 'text-led-warn',
  bad: 'text-led-bad',
  off: 'text-text-tertiary',
  flow: 'text-led-flow',
  human: 'text-led-human',
  live: 'text-led-live',
}

export function CommandBar({
  onSubmit,
  busy,
  blocked = false,
  notice,
  noticeTone,
  chips,
  placeholder = '说一句就行：要做什么、改哪个文件、或者先问一句',
}: CommandBarProps) {
  const [value, setValue] = useState('')
  const inputId = useId()
  const hintId = `${inputId}-hint`
  const area = useRef<HTMLTextAreaElement>(null)
  const canSend = value.trim().length > 0 && !busy && !blocked
  const empty = value.length === 0

  useEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = '0px'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  function submit(event?: FormEvent) {
    event?.preventDefault()
    if (!canSend) return
    onSubmit(value)
    setValue('')
    area.current?.focus()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Escape') {
      setValue('')
      return
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <footer className="shrink-0 border-t border-night-500 bg-night-800/80">
      <div className="mx-auto flex w-full max-w-[1000px] flex-col gap-2 px-4 py-3 sm:px-6">
        <p
          aria-live="polite"
          className={cx('min-h-5 text-sm transition-colors duration-300 ease-base', NOTICE_COLOR[noticeTone])}
        >
          {blocked
            ? '先处理上面的确认：批准或拒绝之后，我们再继续。'
            : (notice ?? '面板只负责把过程显示出来，不替你决定。')}
        </p>

        <form
          onSubmit={submit}
          className={cx(
            'flex items-end gap-3 border border-night-500 bg-night-700 px-3 py-3',
            'transition-colors duration-300 ease-base focus-within:border-neon-cyan',
            'focus-within:shadow-[0_0_18px_rgb(var(--neon-cyan)/0.15)]',
          )}
        >
          <span aria-hidden className="neon-magenta-glow pb-1 font-mono text-lg leading-none text-neon-magenta">
            &gt;
          </span>

          <label htmlFor={inputId} className="sr-only">
            用自然语言描述你要做的事
          </label>
          <div className="relative min-w-0 flex-1">
            <textarea
              id={inputId}
              ref={area}
              rows={1}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              onKeyDown={onKeyDown}
              aria-describedby={hintId}
              autoComplete="off"
              autoFocus
              className={cx(
                'block w-full resize-none bg-transparent py-1 text-lg leading-7 text-text-primary',
                'focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0',
              )}
            />
            {empty ? (
              <span
                aria-hidden
                className="pointer-events-none absolute inset-x-0 top-1 flex items-center gap-1 text-lg leading-7 text-text-tertiary"
              >
                <span className="animate-caret text-neon-cyan neon-cyan-glow">▊</span>
                <span className="truncate">{placeholder}</span>
              </span>
            ) : null}
          </div>

          <button
            type="submit"
            disabled={!canSend}
            className={cx(
              'flex shrink-0 items-center gap-1.5 border px-4 py-2.5 text-base font-semibold leading-none',
              'transition-all duration-300 ease-base active:scale-[0.98]',
              canSend
                ? 'border-neon-magenta/80 bg-neon-magenta/15 text-neon-magenta shadow-[0_0_16px_rgb(var(--neon-magenta)/0.3)] hover:bg-neon-magenta/25'
                : 'cursor-not-allowed border-night-500 text-text-tertiary',
            )}
          >
            <PaperPlaneRight size={15} aria-hidden />
            发送
          </button>
        </form>

        <div className="mt-1 flex flex-wrap items-center gap-2">
          <span className="text-xs text-text-tertiary">试试</span>
          {chips.map((chip) => (
            <button
              key={chip}
              type="button"
              onClick={() => {
                setValue(chip)
                area.current?.focus()
              }}
              className={cx(
                'max-w-[300px] truncate border border-night-500 px-2 py-1 text-xs text-text-secondary',
                'transition-colors duration-300 ease-base hover:border-neon-cyan/60 hover:text-text-primary',
              )}
            >
              {chip}
            </button>
          ))}
          <span id={hintId} className="ml-auto hidden text-xs text-text-tertiary sm:inline">
            回车发送 · shift 回车换行
          </span>
        </div>
      </div>
    </footer>
  )
}
