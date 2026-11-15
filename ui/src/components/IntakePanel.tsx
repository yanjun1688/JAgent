import { useEffect, useRef } from 'react'
import { ArrowRight } from '@phosphor-icons/react'
import type { ChatReply } from '../lib/api'
import type { ChatMessage } from '../lib/chat'
import { cx } from '../lib/cx'
import { Led } from './Led'
import { Section } from './Section'

interface IntakePanelProps {
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
 * and `choices` become buttons that answer the *previous* utterance.
 */
export function IntakePanel({ messages, pending, busy, onChoice }: IntakePanelProps) {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [messages.length, pending])

  const choices = pending?.choices ?? []

  return (
    <Section
      title="对话"
      code="chat"
      className="min-h-0 flex-1"
      right={
        <span className="font-mono text-xs tabular-nums text-text-tertiary">
          {messages.length} 条
        </span>
      }
    >
      <div className="flex min-h-0 flex-1 flex-col border border-ink-500 bg-ink-700">
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 scroll-thin sm:px-6">
          <ol aria-live="polite" className="mx-auto flex w-full max-w-[760px] flex-col gap-5">
            {messages.map((message) => {
              const mine = message.role === 'you'
              return (
                <li
                  key={message.id}
                  className={cx('flex flex-col gap-1', mine ? 'items-end' : 'items-start')}
                >
                  <div className="flex items-center gap-2">
                    {mine ? null : (
                      <Led
                        tone={message.tone ?? 'ok'}
                        size="xs"
                        breathe={message.tone === 'warn'}
                      />
                    )}
                    <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
                      {mine ? '你' : 'JAgent'}
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
                        ? 'bg-ink-600 font-sans text-text-primary'
                        : 'border border-ink-500 bg-ink-800 font-sans text-text-primary',
                    )}
                  >
                    {message.text}
                  </div>
                  {message.meta ? (
                    <span
                      className={cx(
                        'font-mono text-[10px]',
                        message.tone === 'bad'
                          ? 'text-led-bad'
                          : message.tone === 'warn'
                            ? 'text-led-warn'
                            : 'text-text-tertiary',
                      )}
                    >
                      {message.meta}
                    </span>
                  ) : null}
                </li>
              )
            })}

            {choices.length > 0 ? (
              <li className="flex flex-col gap-2 border border-led-warn/40 bg-ink-800 px-3 py-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-led-warn">
                  请选择一项
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
                        'transition-colors duration-300 ease-base',
                        busy
                          ? 'cursor-not-allowed border-ink-500 text-text-tertiary'
                          : 'border-text-secondary text-text-primary hover:bg-text-primary hover:text-ink-800',
                      )}
                    >
                      {choice.label}
                      <ArrowRight size={13} aria-hidden />
                    </button>
                  ))}
                </div>
                <p className="text-xs leading-relaxed text-text-tertiary">
                  也可以直接在下面补一句，不必点选项。
                </p>
              </li>
            ) : null}

            {pending && choices.length === 0 ? (
              <li className="border border-ink-500 bg-ink-800 px-3 py-2 text-xs leading-relaxed text-text-secondary">
                还差一点信息：直接在下面回答就行。
              </li>
            ) : null}
          </ol>
          <div ref={endRef} />
        </div>
      </div>
    </Section>
  )
}
