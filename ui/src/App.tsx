import { useCallback, useEffect, useRef, useState } from 'react'
import { StopCircle } from '@phosphor-icons/react'
import { Header } from './components/Header'
import { DataFlowPanel } from './components/DataFlowPanel'
import { ConversationPanel } from './components/ConversationPanel'
import { ApprovalGate } from './components/ApprovalGate'
import { CommandBar } from './components/CommandBar'
import { LogsPanel } from './components/LogsPanel'
import { VerifyDrawer } from './components/VerifyDrawer'
import { Led } from './components/Led'
import {
  api,
  ApiError,
  type ApprovalView,
  type ChatReply,
  type Decision,
  type RunRow,
  type RunView,
  type SessionSummary,
} from './lib/api'
import type { ChatMessage } from './lib/chat'
import { runStatusLabel, runStatusTone, type Tone } from './lib/kernel'
import { cx } from './lib/cx'

const GREETING =
  '你好，我是 JAgent。告诉我你想完成什么，我会拆成步骤去做。遇到不可逆的操作，我会先停下来，把具体命令和参数给你看，等你点头。'

const IDLE_CHIPS = ['跑回归测试', '看一下当前目录里有什么', '帮我评估这个方案的风险']
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])
const MAIN_SESSION = 's_main'

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
  const [online, setOnline] = useState<boolean | null>(null)
  const [sessionId, setSessionId] = useState(MAIN_SESSION)
  const [sessions, setSessions] = useState<SessionSummary[]>([])
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
  const [verifyOpen, setVerifyOpen] = useState(false)
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

  const refreshSessions = useCallback(async () => {
    try {
      const page = await api.listSessions()
      setSessions(page.sessions)
    } catch {
      // Same: the picker is secondary.
    }
  }, [])

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
    void refreshSessions()
  }, [refreshRuns, refreshSessions])

  // The history list keeps moving without user input.
  useEffect(() => {
    const timer = window.setInterval(() => void refreshRuns(), 4000)
    return () => window.clearInterval(timer)
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
        meta?.tone ??
        (reply.status === 'waiting_human' || reply.status === 'needs_input' ? 'warn' : 'ok')
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
      void refreshSessions()
    },
    [notify, pushMessage, refreshRuns, refreshSessions, runId],
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
        const reply = await api.chat(trimmed, answers, sessionId)
        applyReply(reply, trimmed)
      } catch (error) {
        const detail = describeError(error)
        pushMessage('agent', detail, { tone: 'bad' })
        notify(detail, 'bad')
      } finally {
        setBusy(false)
      }
    },
    [applyReply, busy, notify, pushMessage, sessionId],
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

  const cancelRun = useCallback(async () => {
    if (!runId || busy) return
    pushMessage('you', '取消任务', { meta: '你的决定' })
    setBusy(true)
    try {
      const reply = await api.cancel(runId, '用户取消')
      applyReply(reply, null)
      const view = await api.getRun(runId).catch(() => null)
      if (view) setRun(view)
    } catch (error) {
      const detail = describeError(error)
      pushMessage('agent', detail, { tone: 'bad' })
      notify(detail, 'bad')
    } finally {
      setBusy(false)
    }
  }, [applyReply, busy, notify, pushMessage, runId])

  const resetConversationView = useCallback(() => {
    setMessages([{ id: nextId.current++, role: 'agent', text: GREETING, at: Date.now(), meta: '从一句话开始' }])
    setPending(null)
    setRunId(null)
    setRun(null)
    setGate(null)
    setNotice(null)
    setLastInput('')
    setVerifyOpen(false)
  }, [])

  const selectSession = useCallback(
    (id: string) => {
      if (id === sessionId) return
      setSessionId(id)
      resetConversationView()
      notify(`已切换到 ${id}；记忆跨对话共享，对话历史各自独立。`, 'ok')
      void refreshRuns()
      void refreshSessions()
    },
    [notify, refreshRuns, refreshSessions, resetConversationView, sessionId],
  )

  const createSession = useCallback(async () => {
    try {
      const { session_id } = await api.createSession()
      setSessionId(session_id)
      resetConversationView()
      notify(`新对话 ${session_id} 已就绪，说一句话开始。`, 'ok')
      void refreshSessions()
    } catch (error) {
      notify(describeError(error), 'bad')
    }
  }, [notify, refreshSessions, resetConversationView])

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
  const canCancel = live && !busy && !gate

  return (
    <div className="crt grid-floor flex h-full flex-col bg-night-900">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:bg-night-700 focus:px-3 focus:py-2 focus:text-sm"
      >
        跳到对话
      </a>

      <Header
        status={headerStatus}
        online={online}
        sessions={sessions}
        sessionId={sessionId}
        onSelectSession={(id) => void selectSession(id)}
        onCreateSession={() => void createSession()}
      />

      <div className="relative z-10 flex min-h-0 flex-1">
        <DataFlowPanel
          runId={runId}
          live={live}
          run={run}
          runs={runs}
          busy={busy}
          onSelectRun={(id) => void selectRun(id)}
          onVerify={() => setVerifyOpen(true)}
        />

        <main id="main" className="flex min-h-0 min-w-0 flex-1 flex-col">
          {/* 任务状态条：运行中的任务可以在这里取消（D-028） */}
          <div className="flex shrink-0 items-center gap-3 border-b border-night-600 bg-night-800/50 px-4 py-1.5 sm:px-6">
            <Led
              tone={runStatusTone(run?.status)}
              size="xs"
              breathe={live && Boolean(runId)}
            />
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-text-secondary">
              {runId ? `${runStatusLabel(run?.status)} · ${runId}` : '对话待命 · 说一句话开始任务'}
            </span>
            {canCancel ? (
              <button
                type="button"
                onClick={() => void cancelRun()}
                disabled={busy}
                className={cx(
                  'ml-auto flex items-center gap-1 border px-2 py-0.5 font-mono text-[10px]',
                  'border-led-bad/50 text-led-bad transition-colors duration-300 ease-base hover:bg-led-bad/10',
                  busy && 'cursor-not-allowed opacity-60',
                )}
                title="在步边界记账式取消；已完成/已失败的任务不可取消"
              >
                <StopCircle size={11} aria-hidden />
                取消任务
              </button>
            ) : null}
          </div>

          <div className="flex min-h-0 flex-1 flex-col px-0 py-0">
            <div className="flex min-h-0 flex-1 flex-col px-4 py-4 sm:px-6">
              <ConversationPanel
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
                onDecide={(decision, comment) => void decide(decision, comment)}
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
          </div>
        </main>

        <LogsPanel />
      </div>

      {verifyOpen && runId ? (
        <VerifyDrawer runId={runId} onClose={() => setVerifyOpen(false)} />
      ) : null}
    </div>
  )
}
