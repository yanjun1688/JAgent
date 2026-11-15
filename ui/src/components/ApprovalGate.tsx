import { useState } from 'react'
import { ShieldCheck, WarningCircle } from '@phosphor-icons/react'
import type { ApprovalView, Decision } from '../lib/api'
import { cx } from '../lib/cx'
import { Led } from './Led'

interface ApprovalGateProps {
  approval: ApprovalView
  busy: boolean
  onDecide: (decision: Decision, comment: string) => void
}

/**
 * The hard product requirement: the human sees the exact tool and arguments
 * (`approval.what`) before approving. No summary, no hash, no blind click.
 */
export function ApprovalGate({ approval, busy, onDecide }: ApprovalGateProps) {
  const [comment, setComment] = useState('')
  const readable = approval.what.trim() || approval.step_id || '（运行时没有提供可读说明）'

  return (
    <section
      aria-label="等待你确认"
      className="shrink-0 border-t-2 border-led-warn/60 bg-ink-700"
    >
      <div className="mx-auto flex w-full max-w-[1000px] flex-col gap-3 px-4 py-4 sm:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <Led tone="warn" size="sm" breathe />
          <h2 className="text-sm font-semibold text-text-primary">需要你确认</h2>
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-led-warn">
            approval gate
          </span>
          <span className="ml-auto font-mono text-[10px] text-text-tertiary">
            步骤 {approval.step_id || '—'}
          </span>
        </div>

        <p className="flex items-start gap-2 text-xs leading-relaxed text-text-secondary">
          <WarningCircle size={14} className="mt-0.5 shrink-0 text-led-warn" aria-hidden />
          下面这条操作会真的执行，而且不可撤销。请先核对工具与参数，再决定是否批准。
        </p>

        <div>
          <p className="mb-1 font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
            将要执行
          </p>
          <pre
            className={cx(
              'max-h-44 overflow-auto whitespace-pre-wrap break-words border border-ink-500 bg-ink-800 px-3 py-2',
              'font-mono text-xs leading-relaxed text-text-primary scroll-thin [overflow-wrap:anywhere]',
            )}
          >
            {readable}
          </pre>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="approval-comment">
            备注
          </label>
          <input
            id="approval-comment"
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="备注（可选，会记进这次决定）"
            className={cx(
              'min-w-[180px] flex-1 border border-ink-500 bg-ink-800 px-2 py-1.5 text-sm text-text-primary',
              'placeholder:text-text-tertiary focus:border-text-secondary focus:outline-none',
            )}
          />

          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide('approve', comment.trim())}
            className={cx(
              'flex items-center gap-2 px-4 py-2 text-sm font-semibold',
              'transition-transform duration-300 ease-base active:scale-[0.98]',
              busy
                ? 'cursor-not-allowed border border-ink-500 text-text-tertiary'
                : 'bg-led-ok text-ink-900 hover:opacity-90',
            )}
          >
            <ShieldCheck size={16} weight="fill" aria-hidden />
            批准执行
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide('reject', comment.trim())}
            className={cx(
              'border px-4 py-2 text-sm font-semibold',
              'transition-colors duration-300 ease-base',
              busy
                ? 'cursor-not-allowed border-ink-500 text-text-tertiary'
                : 'border-led-bad/60 text-led-bad hover:bg-led-bad/10',
            )}
          >
            拒绝
          </button>
        </div>
      </div>
    </section>
  )
}
