import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { colors } from '../api/analysis-styles'
import { getRunTimeline } from '../api/analysis-client'
import { confirmAction, pauseRun, resumeRun } from '../api/client'
import {
  ConversationApiError,
  createClientRequestId,
  getConversation,
  sendMessage,
  createConversation,
  isRetryableConversationError,
  persistCurrentConversationId,
} from '../api/conversation-client'
import type { ConversationMessageItem } from '../api/conversation-client'
import { useRunWebSocket, type WsEvent } from '../hooks/useRunWebSocket'
import MessageBubble from './MessageBubble'
import ThinkingPanel from './ThinkingPanel'
import ToolCallCard, { type ToolCallStatus } from './ToolCallCard'
import ConfirmationCard from './ConfirmationCard'
import FinalAnswer from './FinalAnswer'
import PendingIndicator, { pendingPulseKeyframes } from './PendingIndicator'

interface QueuedMessage {
  id: number
  text: string
  requestId: string
  status: 'queued' | 'sending'
}

// P0-07: 网络失败时用同一 client_request_id 重试的次数（后端按 id 幂等去重）。
const MAX_SEND_RETRIES = 3

interface DisplayMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: number
  run_id: string
  status: string
}

interface Props {
  style?: React.CSSProperties
  initialConversationId?: string
  onConversationChange?: (convId: string | null) => void
  onActiveRunChange?: (runId: string | null) => void
  onNewConversation?: () => void
}

let queueIdCounter = 0

export { queueIdCounter }

export default function ConversationDrawer({
  style,
  initialConversationId,
  onConversationChange,
  onActiveRunChange,
  onNewConversation,
}: Props) {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [conversationTitle, setConversationTitle] = useState('Agent Chat')
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeRunStatus, setActiveRunStatus] = useState<string>('')
  const [timelineEvents, setTimelineEvents] = useState<WsEvent[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [thoughtOpen, setThoughtOpen] = useState(true)
  const [queue, setQueue] = useState<QueuedMessage[]>([])
  const [isExecuting, setIsExecuting] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const initializedRef = useRef(false)
  // 追踪最近一次请求加载的会话，防止异步 re-attach 串到已切换的会话
  const conversationIdRef = useRef<string | null>(null)

  const { events: wsEvents, runStatus: wsRunStatus, isConnected } = useRunWebSocket(activeRunId)

  const allEvents = useMemo(() => {
    const tSeqs = new Set(timelineEvents.map((e) => e.seq))
    const newWs = wsEvents.filter((e) => !tSeqs.has(e.seq))
    return [...timelineEvents, ...newWs].sort((a, b) => a.seq - b.seq)
  }, [timelineEvents, wsEvents])

  useEffect(() => {
    if (wsRunStatus) setActiveRunStatus(wsRunStatus)
  }, [wsRunStatus])

  useEffect(() => {
    onActiveRunChange?.(activeRunId)
  }, [activeRunId, onActiveRunChange])

  const pendingConfirmations = useMemo(() => {
    const received = new Set<string>()
    const requested: WsEvent[] = []
    for (const e of allEvents) {
      if (e.event_type === 'ConfirmationReceived' && e.confirmation_id) {
        received.add(e.confirmation_id)
      }
      if (e.event_type === 'ConfirmationRequested' && e.confirmation_id) {
        requested.push(e)
      }
    }
    return requested.filter((e) => e.confirmation_id && !received.has(e.confirmation_id!))
  }, [allEvents])

  const showConfirmationCard = useMemo(() => {
    if (pendingConfirmations.length === 0) return false
    for (let i = allEvents.length - 1; i >= 0; i--) {
      if (allEvents[i].event_type === 'RunResumed') return false
      if (allEvents[i].event_type === 'RunPaused' && allEvents[i].payload.reason === 'waiting_confirmation') return true
    }
    return false
  }, [allEvents, pendingConfirmations])

  const toolCallEvents = useMemo(() => {
    const called = new Map<string, WsEvent>()
    const completed = new Map<string, WsEvent>()
    const failed = new Map<string, WsEvent>()
    const timeout = new Map<string, WsEvent>()

    for (const e of allEvents) {
      const tcid = e.tool_call_id || `seq-${e.seq}`
      if (e.event_type === 'ToolCalled') called.set(tcid, e)
      else if (e.event_type === 'ToolCompleted') completed.set(tcid, e)
      else if (e.event_type === 'ToolFailed') failed.set(tcid, e)
      else if (e.event_type === 'ToolTimeout') timeout.set(tcid, e)
    }

    const cards: Array<{
      key: string
      toolName: string
      status: ToolCallStatus
      input: Record<string, unknown> | null
      output: unknown
      error: string | null
      durationMs: number | null
    }> = []

    for (const [tcid, callEvent] of called) {
      let status: ToolCallStatus = 'running'
      let output: unknown = undefined
      let error: string | null = null
      let durationMs: number | null = null

      if (completed.has(tcid)) {
        status = 'completed'
        output = completed.get(tcid)!.payload.output
        durationMs = completed.get(tcid)!.duration_ms
      } else if (failed.has(tcid)) {
        status = 'failed'
        error = failed.get(tcid)!.error
        durationMs = failed.get(tcid)!.duration_ms
      } else if (timeout.has(tcid)) {
        status = 'timeout'
        durationMs = timeout.get(tcid)!.duration_ms
      }

      cards.push({
        key: tcid,
        toolName: callEvent.tool_name || 'unknown',
        status,
        input: callEvent.input,
        output,
        error,
        durationMs,
      })
    }

    return cards
  }, [allEvents])

  async function handleConfirmResume(confirmationId: string, confirmed: boolean) {
    if (!activeRunId) return
    setLoading(true)
    try {
      await confirmAction(activeRunId, confirmationId, confirmed, '')
      await resumeRun(activeRunId)
    } finally {
      setLoading(false)
    }
  }

  // P0-07: 切回会话时，若该会话最后一条用户消息的 run 仍非终态（running/paused），
  // 重新挂载其实时订阅（WS 重新连接 + 回填已有 timeline），恢复执行中状态展示。
  async function resumeActiveRun(lastRunId: string, convId: string) {
    try {
      const timeline = await getRunTimeline(lastRunId, 200, 0)
      if (conversationIdRef.current !== convId) return
      const events = timeline.timeline as WsEvent[]
      const last = events[events.length - 1]
      if (last && (last.event_type === 'RunCompleted' || last.event_type === 'RunFailed')) return
      setActiveRunId(lastRunId)
      setActiveRunStatus(last?.event_type === 'RunPaused' ? 'paused' : 'running')
      setTimelineEvents(events)
      setThoughtOpen(true)
      setIsExecuting(true)
    } catch {
      // timeline 查询失败不阻断会话加载
    }
  }

  // 自愈：持久化/传入的会话 id 在后端已不存在（DB 重置 / 删除归档 / 跨租户）。
  // 清掉陈旧引用并向上冒泡重置，回到"新会话"态，避免后续发消息持续 404。
  const resetStaleConversation = useCallback(() => {
    conversationIdRef.current = null
    setConversationId(null)
    onConversationChange?.(null)
    persistCurrentConversationId(null)
    setConversationTitle('Agent Chat')
    setMessages([])
    setActiveRunId(null)
    setActiveRunStatus('')
    setTimelineEvents([])
    setIsExecuting(false)
    setQueue([])
    setError(null)
  }, [onConversationChange])

  async function loadConversation(convId: string) {
    conversationIdRef.current = convId
    setConversationId(convId)
    onConversationChange?.(convId)
    persistCurrentConversationId(convId)
    setLoading(true)
    setError(null)
    // P0-07: 切换会话必须原子重置上一会话的 run 状态，防止跨会话串线
    setActiveRunId(null)
    setActiveRunStatus('')
    setTimelineEvents([])
    setIsExecuting(false)
    setQueue([])
    setThoughtOpen(false)
    try {
      const detail = await getConversation(convId)
      setConversationTitle(detail.conversation.title)
      const msgs: DisplayMessage[] = detail.messages.map((m: ConversationMessageItem) => ({
        id: `${m.run_id}-${m.seq}`,
        role: m.role as DisplayMessage['role'],
        content: m.content,
        created_at: m.created_at,
        run_id: m.run_id,
        status: m.status,
      }))
      setMessages(msgs)
      // P0-07: 切回会话时自动恢复仍执行中的 run（若最后一条是用户消息且 run 未终态）
      const lastUser = [...msgs].reverse().find((m) => m.role === 'user')
      if (lastUser) void resumeActiveRun(lastUser.run_id, convId)
    } catch (err) {
      // 404 = 会话已不存在：陈旧引用，自愈到新会话而非报错卡死。
      if (err instanceof ConversationApiError && err.status === 404) {
        if (conversationIdRef.current === convId) resetStaleConversation()
        return
      }
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!initialConversationId || initializedRef.current) return
    initializedRef.current = true
    loadConversation(initialConversationId)
  }, [initialConversationId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, allEvents.length, toolCallEvents.length])

  useEffect(() => {
    if (!activeRunId || !conversationId) return
    if (activeRunStatus === 'completed' || activeRunStatus === 'failed') {
      setIsExecuting(false)
      if (queue.length > 0) {
        const next = queue[0]
        setQueue((prev) => prev.slice(1))
        void executeQueuedMessage(next)
      }
    }
  }, [activeRunStatus, activeRunId, conversationId, queue])

  useEffect(() => {
    if (!activeRunId || !conversationId) return
    if (activeRunStatus === 'completed' || activeRunStatus === 'failed') {
      const summary =
        activeRunStatus === 'completed'
          ? String(allEvents.find((e) => e.event_type === 'RunCompleted')?.payload.result_summary || '')
          : String(
              // P0-07: 失败消息只使用 user_facing_message，不得把内部 final_error 暴露给用户
              allEvents.find((e) => e.event_type === 'RunFailed')?.payload.user_facing_message ||
                '任务未能完成，请检查任务要求或稍后重试。',
            )

      setMessages((prev) => {
        const existing = prev.find((m) => m.run_id === activeRunId && m.role === 'assistant')
        if (existing) {
          return prev.map((m) =>
            m.run_id === activeRunId && m.role === 'assistant'
              ? { ...m, content: summary, status: activeRunStatus }
              : m,
          )
        }
        return [
          ...prev,
          {
            id: `${activeRunId}-assistant`,
            role: 'assistant',
            content: summary,
            created_at: Date.now() / 1000,
            run_id: activeRunId,
            status: activeRunStatus,
          },
        ]
      })

      setTimelineEvents([])
      setActiveRunId(null)
      setActiveRunStatus('')
      setThoughtOpen(false)
    }
  }, [activeRunStatus, activeRunId, conversationId, allEvents])

  async function executeQueuedMessage(qm: QueuedMessage) {
    if (!conversationId) return
    setQueue((prev) => prev.map((q) => (q.id === qm.id ? { ...q, status: 'sending' } : q)))
    try {
      // P0-07: 复用入队时生成的 client_request_id，重试不会产生重复 Run
      await submitMessage(conversationId, qm.text, qm.requestId)
      setQueue((prev) => prev.filter((q) => q.id !== qm.id))
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
      setQueue((prev) => prev.filter((q) => q.id !== qm.id))
      setIsExecuting(false)
    }
  }

  async function submitMessage(convId: string, text: string, requestId?: string) {
    setTimelineEvents([])
    setLoading(true)
    setIsExecuting(true)
    setThoughtOpen(true)

    // P0-07: client_request_id 在消息编组时一次性生成；网络失败重试必须复用
    // 同一个 id，后端按 (conversation, id) 幂等去重，避免重复 Run。
    const clientRequestId = requestId ?? createClientRequestId()

    try {
      let runId: string | null = null
      let lastErr: unknown = null
      for (let attempt = 0; attempt < MAX_SEND_RETRIES; attempt++) {
        if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, 300 * attempt))
        try {
          const resp = await sendMessage(convId, text, clientRequestId)
          runId = resp.run_id
          break
        } catch (err) {
          lastErr = err
          // 确定性错误（404 会话不存在 / 4xx）重试结果不变，立即停止，避免刷重复 404。
          if (!isRetryableConversationError(err)) break
        }
      }
      if (runId === null) throw lastErr ?? new Error('Failed to send message')
      const run_id = runId
      setActiveRunId(run_id)
      setActiveRunStatus('running')

      // 幂等重试（claimed=false 复用已有 run）时避免追加重复的用户气泡
      setMessages((prev) => {
        if (prev.some((m) => m.run_id === run_id && m.role === 'user')) return prev
        return [
          ...prev,
          {
            id: `${run_id}-user`,
            role: 'user',
            content: text,
            created_at: Date.now() / 1000,
            run_id,
            status: 'running',
          },
        ]
      })

      const timeline = await getRunTimeline(run_id, 200, 0)
      setTimelineEvents(timeline.timeline as WsEvent[])
      const last = timeline.timeline[timeline.timeline.length - 1]
      if (last) {
        if (last.event_type === 'RunCompleted') setActiveRunStatus('completed')
        else if (last.event_type === 'RunFailed') setActiveRunStatus('failed')
      }
    } catch (err) {
      // 404 = 当前会话在后端已不存在（DB 重置 / 被删除）。自愈到新会话并提示重发。
      if (err instanceof ConversationApiError && err.status === 404) {
        if (conversationIdRef.current === convId) resetStaleConversation()
        setError('该对话已不存在（可能后端数据已重置），已为你切换到新对话，请重新发送。')
      } else {
        const msg = err instanceof Error ? err.message : String(err)
        setError(msg)
        setIsExecuting(false)
      }
    } finally {
      setLoading(false)
    }
  }

  async function handleSubmit() {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setError(null)

    if (!conversationId) {
      try {
        const conv = await createConversation()
        setConversationId(conv.conversation_id)
        onConversationChange?.(conv.conversation_id)
        persistCurrentConversationId(conv.conversation_id)
        setConversationTitle('Agent Chat')
        setMessages([])
        await submitMessage(conv.conversation_id, text, createClientRequestId())
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        setError(msg)
      }
      return
    }

    if (isExecuting) {
      const id = ++queueIdCounter
      // 入队时即生成幂等 id，执行队列时复用（P0-07）
      setQueue((prev) => [...prev, { id, text, requestId: createClientRequestId(), status: 'queued' }])
      return
    }

    await submitMessage(conversationId, text, createClientRequestId())
  }

  function cancelQueue() {
    setQueue([])
  }

  const welcome = !conversationId && messages.length === 0 && !activeRunId

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: '#fff',
        borderRadius: 12,
        border: `1px solid ${colors.border}`,
        overflow: 'hidden',
        ...style,
      }}
    >
      <style>{pendingPulseKeyframes()}</style>

      <div
        style={{
          padding: '12px 16px',
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          background: '#fafafa',
          flexShrink: 0,
        }}
      >
        <div>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{conversationTitle}</span>
          {activeRunStatus && (
            <span
              style={{
                display: 'inline-block',
                padding: '2px 10px',
                borderRadius: 12,
                fontSize: 10,
                fontWeight: 700,
                color: '#fff',
                background:
                  activeRunStatus === 'running'
                    ? '#4fc3f7'
                    : activeRunStatus === 'completed'
                      ? '#66bb6a'
                      : activeRunStatus === 'failed'
                        ? '#ef5350'
                        : '#999',
                marginLeft: 8,
                verticalAlign: 'middle',
              }}
            >
              {activeRunStatus}
            </span>
          )}
        </div>
        <span style={{ fontSize: 11, color: colors.textSecondary }}>
          {allEvents.length} event{allEvents.length !== 1 ? 's' : ''}
          {activeRunStatus === 'running' && isConnected ? ' · live' : ''}
        </span>
      </div>

      <div
        style={{
          flex: 1,
          overflow: 'auto',
          padding: 12,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        {welcome ? (
          <div style={{ textAlign: 'center', padding: '40px 20px', color: colors.textSecondary }}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>
              {String.fromCodePoint(0x1F916)}
            </div>
            <div style={{ fontWeight: 600, fontSize: 15, color: colors.text, marginBottom: 4 }}>
              Start a conversation
            </div>
            <div style={{ fontSize: 13 }}>Type a task below and watch the agent work.</div>
          </div>
        ) : (
          <>
            {messages.map((msg) =>
              msg.role === 'assistant' ? (
                <div key={msg.id} style={{ display: 'flex', justifyContent: 'flex-start' }}>
                  <FinalAnswer content={msg.content} />
                </div>
              ) : (
                <MessageBubble
                  key={msg.id}
                  role={msg.role}
                  content={msg.content}
                  timestamp={msg.created_at}
                />
              ),
            )}

            {activeRunId && (
              <ThinkingPanel
                events={allEvents}
                open={thoughtOpen}
                onToggle={() => setThoughtOpen(!thoughtOpen)}
                loading={activeRunStatus === 'running'}
              />
            )}

            {activeRunId &&
              toolCallEvents.map((tc) => (
                <ToolCallCard
                  key={tc.key}
                  toolName={tc.toolName}
                  status={tc.status}
                  input={tc.input}
                  output={tc.output}
                  error={tc.error}
                  durationMs={tc.durationMs}
                />
              ))}

            {showConfirmationCard &&
              pendingConfirmations.map((pc) => (
                <ConfirmationCard
                  key={pc.confirmation_id}
                  confirmationId={pc.confirmation_id!}
                  toolName={String(pc.tool_name || 'unknown')}
                  riskLevel={String(pc.payload.risk_level || '')}
                  input={pc.input || undefined}
                  onApprove={(id) => handleConfirmResume(id, true)}
                  onDeny={(id) => handleConfirmResume(id, false)}
                  loading={loading}
                />
              ))}

            {error && (
              <div
                style={{
                  margin: '8px 0',
                  padding: '8px 12px',
                  background: colors.redLight,
                  borderRadius: 6,
                  border: `1px solid ${colors.red}`,
                  fontSize: 12,
                  color: colors.red,
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: 2 }}>Error</div>
                <div style={{ wordBreak: 'break-all' }}>{error}</div>
              </div>
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      <PendingIndicator count={queue.length} onCancel={cancelQueue} />

      <form
        onSubmit={(e) => {
          e.preventDefault()
          handleSubmit()
        }}
        style={{
          display: 'flex',
          gap: 8,
          padding: '10px 12px',
          borderTop: `1px solid ${colors.border}`,
          background: '#fafafa',
          flexShrink: 0,
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={isExecuting ? 'Type your next message (will be queued)...' : 'Type a task...'}
          disabled={loading}
          style={{
            flex: 1,
            padding: '8px 12px',
            border: `1px solid ${colors.border}`,
            borderRadius: 8,
            fontSize: 13,
            outline: 'none',
            background: '#fff',
          }}
        />
        {activeRunStatus === 'running' && (
          <button
            onClick={() => activeRunId && pauseRun(activeRunId)}
            disabled={loading}
            type="button"
            style={{
              padding: '8px 14px',
              background: loading ? '#ffcc80' : '#ffb74d',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            Pause
          </button>
        )}
        {activeRunStatus === 'paused' && (
          <button
            onClick={() => activeRunId && resumeRun(activeRunId)}
            disabled={loading}
            type="button"
            style={{
              padding: '8px 14px',
              background: loading ? '#a5d6a7' : '#66bb6a',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            Resume
          </button>
        )}
        <button
          type="submit"
          disabled={loading || !input.trim()}
          style={{
            padding: '8px 18px',
            background: !input.trim() ? '#ccc' : '#6c5ce7',
            color: '#fff',
            border: 'none',
            borderRadius: 8,
            cursor: !input.trim() ? 'default' : 'pointer',
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
