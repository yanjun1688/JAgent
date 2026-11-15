import { useEffect, useState } from 'react'
import { CheckCircle, Warning, X } from '@phosphor-icons/react'
import { api, ApiError, type VerifyView } from '../lib/api'
import { stepStatusLabel, stepStatusTone } from '../lib/kernel'
import { cx } from '../lib/cx'
import { Led } from './Led'

interface VerifyDrawerProps {
  runId: string
  onClose: () => void
}

const VERDICT_LABEL: Record<string, string> = {
  completed: '声称已完成',
  failed: '声称失败',
  cancelled: '声称已取消',
  none: '没有终态',
}

/**
 * D-032 audit view: claims and evidence side by side. The runtime computes
 * `consistent` / `problems`; the panel only renders them.
 */
export function VerifyDrawer({ runId, onClose }: VerifyDrawerProps) {
  const [view, setView] = useState<VerifyView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .verifyRun(runId)
      .then((data) => {
        if (!cancelled) setView(data)
      })
      .catch((caught) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.detail : '读取验证视图失败。')
      })
    return () => {
      cancelled = true
    }
  }, [runId])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/60" role="dialog" aria-label="验证对账">
      <button type="button" aria-label="关闭验证对账" className="flex-1 cursor-default" onClick={onClose} />
      <div className="animate-neon-in panel-edge flex h-full w-[min(92vw,640px)] flex-col border-l border-neon-violet/50 bg-night-800 shadow-2xl">
        <div className="flex shrink-0 items-center gap-2 border-b border-night-500 px-4 py-3">
          <Led tone={view ? (view.consistent ? 'ok' : 'bad') : 'off'} size="sm" />
          <h2 className="font-display text-sm font-medium uppercase tracking-[0.16em] text-text-primary">
            验证对账
          </h2>
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-text-tertiary">
            claimed vs evidence · D-032
          </span>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="ml-auto border border-night-500 p-1 text-text-tertiary transition-colors duration-300 ease-base hover:border-text-tertiary hover:text-text-primary"
          >
            <X size={13} aria-hidden />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 scroll-thin">
          {error ? (
            <p className="text-sm text-led-bad">{error}</p>
          ) : !view ? (
            <p className="text-sm text-text-tertiary">正在读取对账视图……</p>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="border border-night-500 bg-night-700 px-3 py-3">
                <p className="flex items-center gap-2 text-sm text-text-primary">
                  {view.consistent ? (
                    <CheckCircle size={15} className="text-led-ok" aria-hidden />
                  ) : (
                    <Warning size={15} className="text-led-bad" aria-hidden />
                  )}
                  <span className={view.consistent ? 'text-led-ok' : 'text-led-bad'}>
                    {view.consistent ? '声称与证据一致' : '对账发现问题'}
                  </span>
                  <span className="ml-auto font-mono text-[10px] text-text-tertiary">
                    {VERDICT_LABEL[view.claimed.verdict] ?? view.claimed.verdict}
                  </span>
                </p>
                {view.claimed.fail_reason ? (
                  <p className="mt-1 font-mono text-xs text-text-secondary">
                    原因：{view.claimed.fail_reason}
                  </p>
                ) : null}
                {view.problems.length > 0 ? (
                  <ul className="mt-2 flex flex-col gap-1 border-t border-night-600 pt-2">
                    {view.problems.map((problem) => (
                      <li key={problem} className="text-xs leading-relaxed text-led-bad">
                        · {problem}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              <div>
                <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
                  步骤证据（{view.steps.length}）
                </p>
                <div className="flex flex-col gap-2">
                  {view.steps.map((step) => {
                    const tone = stepStatusTone(step.status)
                    return (
                      <div key={step.step_id} className="border border-night-500 bg-night-700 px-3 py-2">
                        <div className="flex items-center gap-2">
                          <Led tone={tone} size="xs" />
                          <span className="min-w-0 flex-1 truncate text-sm text-text-primary">
                            {step.step_id}
                          </span>
                          <span className="font-mono text-[10px] text-text-tertiary">
                            {step.tool} · 尝试×{step.attempts}
                          </span>
                          <span className="font-mono text-[10px] uppercase text-text-secondary">
                            {stepStatusLabel(step.status)}
                          </span>
                        </div>
                        <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] leading-relaxed text-text-tertiary scroll-thin [overflow-wrap:anywhere]">
                          {JSON.stringify(step.args)}
                        </pre>
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px]">
                          <span
                            className={cx(
                              'font-mono',
                              step.verified === 'pass' ? 'text-led-ok' : 'text-led-warn',
                            )}
                          >
                            断言 {step.verified === 'pass' ? '通过' : step.verified || '未执行'}
                          </span>
                          <span className="font-mono text-text-tertiary">
                            检查 {step.checks.length} 条
                          </span>
                          {step.invalidated ? (
                            <span className="font-mono text-led-warn">曾被失效</span>
                          ) : null}
                          {step.error_code ? (
                            <span className="font-mono text-led-bad">{step.error_code}</span>
                          ) : null}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>

              <div>
                <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
                  模型调用（{view.model_calls.length}）
                </p>
                <ul className="flex flex-col gap-1 font-mono text-[10px] text-text-tertiary">
                  {view.model_calls.slice(-12).map((call, index) => (
                    <li key={index}>
                      {String(call.purpose ?? '?')} · {String(call.model ?? '?')} ·{' '}
                      {String(call.duration_ms ?? 0)}ms · in {String(call.tokens_in ?? 0)} / out{' '}
                      {String(call.tokens_out ?? 0)}
                    </li>
                  ))}
                  {view.model_calls.length === 0 ? <li>本次会话还没有模型调用。</li> : null}
                </ul>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
