import { useEffect, useRef } from 'react'
import type { ChatReply } from '../lib/api'
import type { ChatMessage } from '../lib/chat'
import { cx } from '../lib/cx'
import { Led } from './Led'

interface ConversationPanelProps {
  messages: ChatMessage[]
  /** The open `needs_input` turn, if the runtime is waiting for an answer. */
  pending: ChatReply | null
  busy: boolean
  onChoice: (value: string) => void
}

function timeOf(at: number): string {
  const date = new Date(at)
  const pad = (value: number) => value.toString().padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * The conversation, not an intake form: the runtime's `message` is the voice,
 * and `choices` become neon buttons that answer the *previous* utterance.
 */
export function ConversationPanel({ messages, pending, busy, onChoice }: ConversationPanelProps) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [messages.length, pending])

  const choices = pending?.choices ?? []

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 scroll-thin sm:px-6">
        <ol aria-live="polite" className="mx-auto flex w-full max-w-[760px] flex-col gap-5">
          {messages.map((message) => {
            const mine = message.role === 'you'
            return (
              <li
                key={message.id}
                className={cx('animate-row-in flex flex-col gap-1', mine ? 'items-end' : 'items-start')}
              >
                <div className="flex items-center gap-2">
                  {mine ? null : (
                    <Led
                      tone={message.tone ?? 'ok'}
                      size="xs"
                      breathe={message.tone === 'warn' || message.tone === 'human'}
                    />
                  )}
                  <span className="font-display text-[10px] uppercase tracking-[0.2em] text-text-tertiary">
                    {mine ? 'YOU' : 'JAGENT'}
                  </span>
                  <span className="font-mono text-[10px] tabular-nums text-text-tertiary">
                    {timeOf(message.at)}
                  </span>
                </div>
                <div
                  className={cx(
                    'max-w-[85%] whitespace-pre-wrap break-words px-3 py-2 text-[15px] leading-7',
                    '[overflow-wrap:anywhere]',
                    mine
                      ? 'border border-neon-cyan/50 bg-neon-cyan/10 text-text-primary'
                      : 'panel-edge border border-night-500 bg-night-700 text-text-primary',
                  )}
                >
                  {message.text}
                </div>
                {message.badge ? (
                  <span
                    className={cx(
                      'inline-flex items-center gap-1 border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.14em]',
                      message.badgeTone === 'warn'
                        ? 'border-led-warn/50 text-led-warn'
                        : message.badgeTone === 'flow'
                          ? 'border-neon-cyan/40 text-neon-cyan'
                          : 'border-led-ok/40 text-led-ok',
                    )}
                  >
                    {message.badge}
                  </span>
                ) : null}
                {message.meta ? (
                  <span
                    className={cx(
                      'font-mono text-[10px]',
                      message.tone === 'bad'
                        ? 'text-led-bad'
                        : message.tone === 'warn'
                          ? 'text-led-warn'
                          : message.tone === 'human'
                            ? 'text-led-human'
                            : 'text-text-tertiary',
                    )}
                  >
                    {message.meta}
                  </span>
                ) : null}
              </li>
            )
          })}

          {busy ? (
            <li className="flex items-center gap-2">
              <Led tone="flow" size="xs" breathe />
              <span className="font-display text-[10px] uppercase tracking-[0.2em] text-text-tertiary">
                JAGENT 处理中
              </span>
              <span className="animate-caret font-mono text-sm text-neon-cyan">▊</span>
            </li>
          ) : null}

          {choices.length > 0 ? (
            <li className="animate-neon-in flex flex-col gap-2 border border-led-warn/50 bg-night-800 px-3 py-3 shadow-[0_0_18px_rgb(var(--led-warn)/0.08)]">
              <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-led-warn">
                请选择一项 · {pending?.slot}
              </p>
              <div className="flex flex-wrap gap-2">
                {choices.map((choice) => (
                  <button
                    key={choice.value}
                    type="button"
                    disabled={busy}
                    onClick={() => onChoice(choice.value)}
                    className={cx(
                      'flex items-center gap-1.5 border px-3 py-1.5 text-sm',
                      'transition-all duration-300 ease-base',
                      busy
                        ? 'cursor-not-allowed border-night-500 text-text-tertiary'
                        : 'border-neon-amber/60 text-neon-amber hover:bg-neon-amber/10 hover:shadow-[0_0_14px_rgb(var(--neon-amber)/0.25)]',
                    )}
                  >
                    {choice.label}
                  </button>
                ))}
              </div>
            </li>
          ) : null}
          <div ref={endRef} />
        </ol>
      </div>
    </div>
  )
}
