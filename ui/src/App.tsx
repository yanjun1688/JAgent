import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from '@phosphor-icons/react'
import { Header } from './components/Header'
import { StreamLoop } from './components/StreamLoop'
import { CommandBar } from './components/CommandBar'
import { ApprovalGate } from './components/ApprovalGate'
import { IntakePanel } from './components/IntakePanel'
import { Topology } from './components/Topology'
import { Section } from './components/Section'
import { Led } from './components/Led'
import {
  api,
  ApiError,
  type ApprovalView,
  type ChatReply,
  type Decision,
  type RunRow,
  type RunView,
} from './lib/api'
import type { ChatMessage } from './lib/chat'
import { runStatusLabel, runStatusTone, type Tone } from './lib/kernel'
import { applyTheme, readTheme, type Theme } from './lib/theme'
import { cx } from './lib/cx'

const GREETING =
  '你好，我是 JAgent。告诉我你想完成什么，我会拆成步骤去做。遇到不可逆的操作，我会先停下来，把具体命令和参数给你看，等你点头。'

const IDLE_CHIPS = ['跑回归测试', '看一下当前目录里有什么', '帮我评估这个方案的风险']
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  return '出了点问题，请再试一次。'
}

function fallbackMessage(reply: ChatReply): string {
  if (reply.status === 'needs_input') return '还需要你补充一点信息。'
  if (reply.status === 'waiting_human') return '这一步需要你确认。'
  return '处理好了。'
}

function resultMeta(reply: ChatReply): { text: string; tone: Tone } | null {
  if (!reply.result) return null
  const total = reply.result.steps.length
  const passed = reply.result.steps.filter((step) => step.status === 'passed').length
  const verdict =
    reply.result.verdict === 'completed'
      ? '已完成'
      : reply.result.verdict === 'failed'
        ? '失败'
        : reply.result.verdict === 'cancelled'
          ? '已取消'
          : reply.result.verdict
  const steps = total > 0 ? ` · ${passed}/${total} 步通过` : ''
  return {
    text: `${verdict}${steps}`,
    tone: reply.result.verdict === 'failed' ? 'bad' : 'ok',
  }
}

export default function App() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  const [online, setOnline] = useState<boolean | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    { id: 1, role: 'agent', text: GREETING, at: Date.now(), meta: '从一句话开始' },
  ])
  const [pending, setPending] = useState<ChatReply | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [noticeTone, setNoticeTone] = useState<Tone>('ok')
  const [lastInput, setLastInput] = useState('')
  const [runId, setRunId] = useState<string | null>(null)
  const [run, setRun] = useState<RunView | null>(null)
  const [runs, setRuns] = useState<RunRow[]>([])
  const [gate, setGate] = useState<ApprovalView | null>(null)
  const [panelOpen, setPanelOpen] = useState<boolean>(
    () => typeof window !== 'undefined' && window.innerWidth >= 1024,
  )
  const nextId = useRef(2)

  const pushMessage = useCallback(
    (role: ChatMessage['role'], text: string, extra?: Pick<ChatMessage, 'meta' | 'tone'>) => {
      const clean = text.trim()
      if (!clean) return
      setMessages((current) => [
        ...current,
        { id: nextId.current++, role, text: clean, at: Date.now(), ...extra },
      ])
    },
    [],
  )

  const notify = useCallback((text: string | null, tone: Tone = 'ok') => {
    setNotice(text)
    setNoticeTone(tone)
  }, [])

  const refreshRuns = useCallback(async () => {
    try {
      const page = await api.listRuns(50)
      setRuns(page.runs)
    } catch {
      // The history list is secondary; a miss must not block the conversation.
    }
  }, [])

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // The badge is a fact: probe now, then recover automatically when the
  // local service is started after the page was opened.
  useEffect(() => {
    let cancelled = false
    const probe = () => {
      api
        .health()
        .then(() => {
          if (!cancelled) setOnline(true)
        })
        .catch(() => {
          if (!cancelled) setOnline(false)
        })
    }
    probe()
    const timer = window.setInterval(probe, 15_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    void refreshRuns()
  }, [refreshRuns])

  // Keep the selected run current while it is alive. Terminal runs get one
  // extra read so a late approval/step change is still reflected.
  useEffect(() => {
    if (!runId) {
      setRun(null)
      setGate(null)
      return
    }

    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const view = await api.getRun(runId)
        if (cancelled) return
        setRun(view)
        setGate(view.approval)
        if (!TERMINAL_STATUSES.has(view.status)) {
          timer = window.setTimeout(tick, 1000)
        }
      } catch (error) {
        if (cancelled) return
        if (error instanceof ApiError && error.status === 404) {
          setRun(null)
          setGate(null)
          return
        }
        timer = window.setTimeout(tick, 2000)
      }
    }

    void tick()

    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [runId])

  const applyReply = useCallback(
    (reply: ChatReply, sentText: string | null) => {
      if (sentText !== null) setLastInput(sentText)

      const meta = resultMeta(reply)
      const tone: Tone =
        meta?.tone ?? (reply.status === 'waiting_human' || reply.status === 'needs_input' ? 'warn' : 'ok')
      pushMessage('agent', reply.message?.trim() || fallbackMessage(reply), {
        ...(meta ? { meta: meta.text } : {}),
        tone,
      })

      setPending(reply.status === 'needs_input' ? reply : null)
      if (reply.run_id) {
        if (reply.run_id !== runId) setRun(null)
        setRunId(reply.run_id)
      }
      if (reply.approval) setGate(reply.approval)
      else if (reply.status !== 'waiting_human') setGate(null)

      if (reply.status === 'needs_input') {
        notify('选一个选项，或直接补充一句。', 'warn')
      } else if (reply.status === 'answered') {
        notify(null, 'ok')
      } else {
        notify(null, 'warn')
      }

      if (reply.run_id) void refreshRuns()
    },
    [notify, pushMessage, refreshRuns, runId],
  )

  const send = useCallback(
    async (text: string, answers?: Record<string, string>, choiceLabel?: string) => {
      const trimmed = text.trim()
      if (!trimmed || busy) return

      if (choiceLabel) pushMessage('you', choiceLabel, { meta: '你的选择' })
      else pushMessage('you', trimmed)

      setPending(null)
      setNotice(null)
      setBusy(true)
      try {
        const reply = await api.chat(trimmed, answers)
        applyReply(reply, trimmed)
      } catch (error) {
        const detail = describeError(error)
        pushMessage('agent', detail, { tone: 'bad' })
        notify(detail, 'bad')
      } finally {
        setBusy(false)
      }
    },
    [applyReply, busy, notify, pushMessage],
  )

  const choose = useCallback(
    (value: string) => {
      if (busy) return
      const slot = pending?.slot?.trim()
      const previous = lastInput.trim()
      // A choice answers the *previous* utterance; that is exactly what the
      // session expects (`{text, answers: {slot: value}}`).
      if (!slot || !previous) {
        void send(value)
        return
      }
      void send(previous, { [slot]: value }, value)
    },
    [busy, lastInput, pending, send],
  )

  const decide = useCallback(
    async (decision: Decision, comment: string) => {
      if (!runId || !gate || busy) return
      pushMessage('you', decision === 'approve' ? '批准执行' : '拒绝执行', {
        meta: comment ? `备注：${comment}` : '你的决定',
      })
      setBusy(true)
      try {
        const reply = await api.decide(runId, decision, comment)
        setGate(null)
        applyReply(reply, null)
        const view = await api.getRun(runId).catch(() => null)
        if (view) {
          setRun(view)
          setGate(view.approval)
        }
      } catch (error) {
        const detail = describeError(error)
        pushMessage('agent', detail, { tone: 'bad' })
        notify(detail, 'bad')
      } finally {
        setBusy(false)
      }
    },
    [applyReply, busy, gate, notify, pushMessage, runId],
  )

  const selectRun = useCallback(
    async (id: string) => {
      if (id === runId) return
      setRunId(id)
      setRun(null)
      setGate(null)
      setPending(null)
      notify('已切换到这条任务；左侧对话不会被清空。', 'ok')
      try {
        const view = await api.getRun(id)
        setRun(view)
        setGate(view.approval)
      } catch (error) {
        notify(describeError(error), 'bad')
      }
    },
    [notify, runId],
  )

  const headerStatus = busy ? 'running' : (run?.status ?? (pending ? 'needs_input' : null))
  const live = Boolean(runId) && (!run || !TERMINAL_STATUSES.has(run.status))

  return (
    <div className="crt flex h-full flex-col bg-ink-800">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:bg-ink-600 focus:px-3 focus:py-2 focus:text-sm"
      >
        跳到对话
      </a>

      <Header
        status={headerStatus}
        theme={theme}
        online={online}
        panelOpen={panelOpen}
        onToggleTheme={() => setTheme((current) => (current === 'dark' ? 'light' : 'dark'))}
        onTogglePanel={() => setPanelOpen((current) => !current)}
      />

      <div className="flex min-h-0 flex-1">
        <main id="main" className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex min-h-0 flex-1 flex-col px-4 py-4 sm:px-6">
            <IntakePanel
              messages={messages}
              pending={pending}
              busy={busy}
              onChoice={choose}
            />
          </div>

          {gate ? (
            <ApprovalGate
              key={`${gate.step_id}:${gate.what}`}
              approval={gate}
              busy={busy}
              onDecide={decide}
            />
          ) : null}

          <CommandBar
            onSubmit={(text) => void send(text)}
            busy={busy}
            blocked={Boolean(gate)}
            notice={notice}
            noticeTone={noticeTone}
            chips={IDLE_CHIPS}
          />
        </main>

        {panelOpen ? (
          <button
            type="button"
            aria-label="收起过程面板"
            onClick={() => setPanelOpen(false)}
            className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          />
        ) : null}

        <aside
          aria-label="过程"
          className={cx(
            'z-40 min-h-0 flex-col border-ink-500 bg-ink-800',
            panelOpen
              ? 'fixed inset-y-0 right-0 flex w-[min(92vw,380px)] border-l shadow-2xl lg:static lg:w-[380px] lg:shadow-none'
              : 'hidden',
          )}
        >
          <div className="flex shrink-0 items-center justify-between border-b border-ink-500 px-4 py-3">
            <div className="flex min-w-0 items-center gap-2">
              <Led tone={runStatusTone(run?.status)} size="xs" breathe={live && Boolean(runId)} />
              <span className="text-sm text-text-primary">过程</span>
              <span className="truncate font-mono text-[10px] uppercase tracking-[0.14em] text-text-tertiary">
                状态与事件
              </span>
            </div>
            <button
              type="button"
              onClick={() => setPanelOpen(false)}
              aria-label="收起过程"
              className="border border-ink-500 p-1 text-text-tertiary transition-colors duration-300 ease-base hover:border-text-tertiary hover:text-text-primary"
            >
              <X size={13} aria-hidden />
            </button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 scroll-thin">
            <div className="flex flex-col gap-5">
              <Section title="当前任务" code="run">
                <div className="border border-ink-500 bg-ink-700 px-3 py-3">
                  <div className="flex items-center gap-2">
                    <Led
                      tone={runStatusTone(run?.status)}
                      size="sm"
                      breathe={run?.status === 'running' || run?.status === 'waiting_human'}
                    />
                    <span className="text-sm text-text-primary">{runStatusLabel(run?.status)}</span>
                    {run ? (
                      <span className="ml-auto font-mono text-[10px] tabular-nums text-text-tertiary">
                        {run.steps.length} 步
                      </span>
                    ) : null}
                  </div>

                  {run?.summary ? (
                    <p className="mt-2 text-xs leading-relaxed text-text-secondary">{run.summary}</p>
                  ) : (
                    <p className="mt-2 text-xs leading-relaxed text-text-tertiary">
                      {run
                        ? '这条任务还没有可展示的总结。'
                        : '还没有任务。在左侧说一句话就能开始。'}
                    </p>
                  )}

                  {gate ? (
                    <p className="mt-2 border-t border-ink-600 pt-2 text-xs text-led-warn">
                      有一条操作在等你确认，批准前不会执行。
                    </p>
                  ) : null}

                  {runId ? (
                    <p className="mt-2 font-mono text-[10px] text-text-tertiary">编号 {runId}</p>
                  ) : null}
                </div>
              </Section>

              <Section title="步骤" code="steps">
                <Topology steps={run?.steps ?? []} />
              </Section>

              <StreamLoop runId={runId} live={live} />

              <Section
                title="最近任务"
                code="history"
                right={
                  <span className="font-mono text-[10px] tabular-nums text-text-tertiary">
                    {runs.length}
                  </span>
                }
              >
                <div className="border border-ink-500 bg-ink-700">
                  {runs.length === 0 ? (
                    <p className="px-3 py-4 text-center text-xs text-text-tertiary">还没有历史任务。</p>
                  ) : (
                    <ul>
                      {runs.map((row) => (
                        <li key={row.run_id} className="border-b border-ink-600 last:border-0">
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void selectRun(row.run_id)}
                            className={cx(
                              'flex w-full items-center gap-2 px-3 py-2 text-left',
                              'transition-colors duration-300 ease-base',
                              row.run_id === runId ? 'bg-ink-600' : 'hover:bg-ink-600/60',
                              busy && 'cursor-not-allowed opacity-60',
                            )}
                          >
                            <Led tone={runStatusTone(row.status)} size="xs" />
                            <span className="min-w-0 flex-1 truncate text-xs text-text-primary">
                              {row.summary || `任务 ${row.run_id.slice(-6)}`}
                            </span>
                            <span className="shrink-0 font-mono text-[10px] text-text-tertiary">
                              {runStatusLabel(row.status)}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </Section>
            </div>
          </div>
        </aside>
      </div>
    </div>
  )
}
