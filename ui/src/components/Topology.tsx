import type { StepView } from '../lib/api'
import { stepStatusLabel, stepStatusTone } from '../lib/kernel'
import { cx } from '../lib/cx'
import { Led } from './Led'

interface TopologyProps {
  steps: StepView[]
}

function stepName(id: string): string {
  const words = id.replace(/[_-]+/g, ' ').trim()
  return words || id
}

/**
 * The plan is a list of steps, not a graph: the runtime's own `run.steps` is
 * the source of truth. Status words are translated, never re-derived.
 */
export function Topology({ steps }: TopologyProps) {
  if (steps.length === 0) {
    return (
      <div className="flex min-h-24 flex-col items-center justify-center gap-1 border border-dashed border-ink-500 bg-ink-800/40 px-4 py-5 text-center">
        <p className="text-sm text-text-secondary">还没有步骤</p>
        <p className="text-xs leading-relaxed text-text-tertiary">
          开始一个任务后，这里会按顺序显示每一步的状态。
        </p>
      </div>
    )
  }

  const passed = steps.filter((step) => step.status === 'passed').length
  const ratio = Math.round((passed / steps.length) * 100)

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
          steps
        </span>
        <span className="font-mono text-xs tabular-nums text-text-secondary">
          {passed}/{steps.length} 通过
        </span>
      </div>

      <div className="h-1 w-full bg-ink-600" role="progressbar" aria-valuenow={ratio} aria-valuemin={0} aria-valuemax={100}>
        <div
          className="h-full bg-led-ok transition-[width] duration-500 ease-base"
          style={{ width: `${ratio}%` }}
        />
      </div>

      <ol className="flex flex-col">
        {steps.map((step, index) => {
          const tone = stepStatusTone(step.status)
          const last = index === steps.length - 1
          return (
            <li key={step.id} className="relative flex gap-3 pb-3 last:pb-0">
              {!last ? (
                <span
                  aria-hidden
                  className="absolute left-[3px] top-4 h-[calc(100%-0.5rem)] w-px bg-ink-500"
                />
              ) : null}
              <span className="relative z-10 mt-1.5 bg-ink-800">
                <Led tone={tone} size="sm" breathe={step.status === 'running'} />
              </span>
              <div className="flex min-w-0 flex-1 items-baseline justify-between gap-2">
                <span
                  className={cx(
                    'min-w-0 truncate text-sm',
                    tone === 'off' ? 'text-text-tertiary' : 'text-text-primary',
                  )}
                  title={step.id}
                >
                  {stepName(step.id)}
                </span>
                <span
                  className={cx(
                    'shrink-0 font-mono text-[10px] uppercase tracking-[0.08em]',
                    tone === 'ok' && 'text-led-ok',
                    tone === 'warn' && 'text-led-warn',
                    tone === 'bad' && 'text-led-bad',
                    tone === 'off' && 'text-text-tertiary',
                  )}
                >
                  {stepStatusLabel(step.status)}
                </span>
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
