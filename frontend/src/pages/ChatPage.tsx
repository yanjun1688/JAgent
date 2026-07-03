import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { Sparkles } from 'lucide-react'
import {
  createConversation,
  getConversation,
  persistCurrentConversationId,
  restoreCurrentConversationId,
  sendMessage,
  type ConversationMessageItem,
} from '../api/conversation-client'
import { getRunTimeline } from '../api/analysis-client'
import { useConversations } from '../hooks/useConversation'
import { useRunControl } from '../hooks/useRunControl'
import { useRunWebSocket, type WsEvent } from '../hooks/useRunWebSocket'
import { useConversationStore } from '../stores/conversationStore'
import { useUIStore } from '../stores/uiStore'
import { cn } from '../design-system/utils/cn'
import { ConversationSidebar } from '../components/conversation/ConversationSidebar'
import { MessageBubble } from '../components/chat/MessageBubble'
import { ThinkingPanel } from '../components/chat/ThinkingPanel'
import { ToolCallCard, type ToolCallStatus } from '../components/chat/ToolCallCard'
import { ConfirmationCard } from '../components/chat/ConfirmationCard'
import { InputBar } from '../components/chat/InputBar'
import { RealtimePanel } from '../components/realtime/RealtimePanel'
import { AuroraGradient } from '../components/effects/AuroraGradient'

interface DisplayMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: number
  run_id: string
  status: string
}

interface QueuedMessage {
  id: number
  text: string
}

let queueIdCounter = 0

export default function ChatPage() {
  // 对话列表
  const {
    conversations,
    isLoading: listLoading,
    error: listError,
    create: createAsync,
    remove: removeAsync,
  } = useConversations()

  const { sidebarOpen, realtimePanelOpen, toggleSidebar, toggleRealtimePanel } = useUIStore()
  const { searchQuery, setSearchQuery, setActiveConversation } = useConversationStore()

  // 本地对话状态
  const [activeConvId, setActiveConvId] = useState<string | null>(
    restoreCurrentConversationId() || null,
  )
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

  const { events: wsEvents, runStatus: wsRunStatus, isConnected } =
    useRunWebSocket(activeRunId)
  const { pauseAsync, resumeAsync, confirmAsync } = useRunControl()

  // 合并历史 timeline 与 WS 实时事件，按 seq 排序去重
  const allEvents = useMemo(() => {
    const tSeqs = new Set(timelineEvents.map((e) => e.seq))
    const fresh = wsEvents.filter((e) => !tSeqs.has(e.seq))
    return [...timelineEvents, ...fresh].sort((a, b) => a.seq - b.seq)
  }, [timelineEvents, wsEvents])

  useEffect(() => {
    if (wsRunStatus) setActiveRunStatus(wsRunStatus)
  }, [wsRunStatus])

  // 待确认事项
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
    return requested.filter((e) => e.confirmation_id && !received.has(e.confirmation_id))
  }, [allEvents])

  const showConfirmationCard = useMemo(() => {
    if (pendingConfirmations.length === 0) return false
    for (let i = allEvents.length - 1; i >= 0; i--) {
      if (allEvents[i].event_type === 'RunResumed') return false
      if (
        allEvents[i].event_type === 'RunPaused' &&
        allEvents[i].payload.reason === 'waiting_confirmation'
      )
        return true
    }
    return false
  }, [allEvents, pendingConfirmations])

  // 工具调用卡片
  const toolCallEvents = useMemo(() => {
    const called = new Map<string, WsEvent>()
    const completed = new Map<string, WsEvent>()
    const failed = new Map<string, WsEvent>()
    const timeout = new Map<string, WsEvent>()
    const blocked = new Map<string, WsEvent>()

    for (const e of allEvents) {
      const tcid = e.tool_call_id || `seq-${e.seq}`
      if (e.event_type === 'ToolCalled') called.set(tcid, e)
      else if (e.event_type === 'ToolCompleted') completed.set(tcid, e)
      else if (e.event_type === 'ToolFailed') failed.set(tcid, e)
      else if (e.event_type === 'ToolTimeout') timeout.set(tcid, e)
      else if (e.event_type === 'GuardrailTriggered') blocked.set(tcid, e)
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

      if (blocked.has(tcid)) {
        status = 'guardrail_blocked'
        error = blocked.get(tcid)!.error
      } else if (completed.has(tcid)) {
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

  const loadConversation = useCallback(async (convId: string) => {
    setActiveConvId(convId)
    setActiveConversation(convId)
    persistCurrentConversationId(convId)
    setLoading(true)
    setError(null)
    try {
      const detail = await getConversation(convId)
      setConversationTitle(detail.conversation.title || '新对话')
      const msgs: DisplayMessage[] = detail.messages.map((m: ConversationMessageItem) => ({
        id: `${m.run_id}-${m.seq}`,
        role: m.role,
        content: m.content,
        created_at: m.created_at,
        run_id: m.run_id,
        status: m.status,
      }))
      setMessages(msgs)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [setActiveConversation])

  // 初始化加载持久化的对话
  useEffect(() => {
    if (initializedRef.current) return
    if (activeConvId) {
      initializedRef.current = true
      void loadConversation(activeConvId)
    }
  }, [activeConvId, loadConversation])

  // 自动滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, allEvents.length, toolCallEvents.length])

  // Run 终态：写入 assistant 消息，并处理消息队列
  useEffect(() => {
    if (!activeRunId || !activeConvId) return
    if (activeRunStatus !== 'completed' && activeRunStatus !== 'failed') return

    setIsExecuting(false)

    const summary =
      activeRunStatus === 'completed'
        ? String(
            allEvents.find((e) => e.event_type === 'RunCompleted')?.payload.result_summary || '',
          )
        : String(
            allEvents.find((e) => e.event_type === 'RunFailed')?.payload.final_error || '',
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

    if (queue.length > 0) {
      const next = queue[0]
      setQueue((prev) => prev.slice(1))
      void executeQueuedMessage(next)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunStatus, activeRunId, activeConvId])

  async function executeQueuedMessage(qm: QueuedMessage): Promise<void> {
    if (!activeConvId) return
    try {
      await submitMessage(activeConvId, qm.text)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setIsExecuting(false)
    }
  }

  async function submitMessage(convId: string, text: string): Promise<void> {
    setTimelineEvents([])
    setLoading(true)
    setIsExecuting(true)
    setThoughtOpen(true)
    setError(null)

    try {
      const { run_id } = await sendMessage(convId, text)
      setActiveRunId(run_id)
      setActiveRunStatus('running')

      setMessages((prev) => [
        ...prev,
        {
          id: `${run_id}-user`,
          role: 'user',
          content: text,
          created_at: Date.now() / 1000,
          run_id,
          status: 'running',
        },
      ])

      const timeline = await getRunTimeline(run_id, 200, 0)
      setTimelineEvents(timeline.timeline as WsEvent[])
      const last = timeline.timeline[timeline.timeline.length - 1]
      if (last) {
        if (last.event_type === 'RunCompleted') setActiveRunStatus('completed')
        else if (last.event_type === 'RunFailed') setActiveRunStatus('failed')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setIsExecuting(false)
    } finally {
      setLoading(false)
    }
  }

  async function handleSubmit(): Promise<void> {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setError(null)

    if (!activeConvId) {
      try {
        const conv = await createAsync(undefined)
        const convId = conv.conversation_id
        setActiveConvId(convId)
        setActiveConversation(convId)
        persistCurrentConversationId(convId)
        setConversationTitle('新对话')
        setMessages([])
        await submitMessage(convId, text)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
      return
    }

    if (isExecuting) {
      queueIdCounter += 1
      setQueue((prev) => [...prev, { id: queueIdCounter, text }])
      return
    }

    await submitMessage(activeConvId, text)
  }

  async function handleConfirmResume(confirmationId: string, confirmed: boolean): Promise<void> {
    if (!activeRunId) return
    setLoading(true)
    try {
      await confirmAsync({
        runId: activeRunId,
        confirmationId,
        confirmed,
        operatorId: '',
      })
      await resumeAsync(activeRunId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  async function handlePause(): Promise<void> {
    if (!activeRunId) return
    try {
      await pauseAsync(activeRunId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }
  async function handleResume(): Promise<void> {
    if (!activeRunId) return
    try {
      await resumeAsync(activeRunId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  function handleNewConversation(): void {
    setActiveConvId(null)
    setActiveConversation(null)
    persistCurrentConversationId(null)
    setMessages([])
    setConversationTitle('新对话')
    setActiveRunId(null)
    setActiveRunStatus('')
    setTimelineEvents([])
    setError(null)
    setThoughtOpen(true)
    setQueue([])
    setIsExecuting(false)
  }

  async function handleDeleteConversation(id: string): Promise<void> {
    try {
      await removeAsync(id)
      if (id === activeConvId) handleNewConversation()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const welcome = !activeConvId && messages.length === 0 && !activeRunId

  // 推导可暂停/恢复
  const canPause = isExecuting && activeRunStatus === 'running' && !!activeRunId
  const canResume = activeRunStatus === 'paused' && !!activeRunId

  return (
    <div className="relative flex min-h-0 flex-1 gap-3 p-3">
      <AuroraGradient className="rounded-2xl" />
      {/* 左栏：对话列表 */}
      <AnimatePresence initial={false}>
        {sidebarOpen && (
          <motion.div
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 288, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="hidden min-h-0 shrink-0 overflow-hidden md:block"
          >
            <ConversationSidebar
              conversations={conversations}
              activeConversationId={activeConvId}
              searchQuery={searchQuery}
              isLoading={listLoading}
              error={listError ? (listError instanceof Error ? listError.message : String(listError)) : null}
              onSearchChange={setSearchQuery}
              onSelect={(id) => void loadConversation(id)}
              onDelete={(id) => void handleDeleteConversation(id)}
              onNew={handleNewConversation}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* 中栏：聊天区域 */}
      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl glass-base">
        {/* 折叠按钮 */}
        <button
          onClick={toggleSidebar}
          className="absolute left-0 top-3 z-10 rounded-r-lg border border-border-soft bg-background-secondary/80 px-1.5 py-2 text-xs text-text-secondary hover:text-text-primary"
          title={sidebarOpen ? '收起侧栏' : '展开侧栏'}
        >
          {sidebarOpen ? '◀' : '▶'}
        </button>

        {/* 对话标题栏 */}
        <div className="flex shrink-0 items-center gap-2 border-b border-border-soft px-5 py-3 pl-10">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent-secondary/20 text-accent-secondary">
            <Sparkles size={15} />
          </span>
          <span className="truncate text-sm font-semibold text-text-primary">
            {conversationTitle}
          </span>
          {activeRunStatus && (
            <span
              className={cn(
                'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium',
                activeRunStatus === 'running'
                  ? 'bg-status-info/20 text-status-info'
                  : activeRunStatus === 'completed'
                    ? 'bg-status-success/20 text-status-success'
                    : activeRunStatus === 'failed'
                      ? 'bg-status-error/20 text-status-error'
                      : 'bg-status-warning/20 text-status-warning',
              )}
            >
              {activeRunStatus}
            </span>
          )}
          <span className="ml-auto text-xs text-text-muted">
            {allEvents.length} 事件
            {activeRunStatus === 'running' && isConnected ? ' · 实时' : ''}
          </span>
        </div>

        {/* 消息流 */}
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {welcome ? (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <motion.div
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                className="gradient-aurora flex h-20 w-20 items-center justify-center rounded-full"
              >
                <Sparkles size={32} className="text-accent-secondary" />
              </motion.div>
              <h2 className="mt-6 font-display text-2xl font-bold text-text-primary">
                开始一次对话
              </h2>
              <p className="mt-2 max-w-sm text-sm text-text-tertiary">
                输入任务，观察 Agent 思考、调用工具并实时反馈结果。
              </p>
            </div>
          ) : (
            <>
              {messages.map((msg) =>
                msg.role === 'assistant' ? (
                  <MessageBubble
                    key={msg.id}
                    role="assistant"
                    content={msg.content}
                    timestamp={msg.created_at}
                  />
                ) : (
                  <MessageBubble
                    key={msg.id}
                    role="user"
                    content={msg.content}
                    timestamp={msg.created_at}
                  />
                ),
              )}

              {activeRunId && (
                <ThinkingPanel
                  events={allEvents}
                  open={thoughtOpen}
                  loading={activeRunStatus === 'running'}
                  onToggle={() => setThoughtOpen((v) => !v)}
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
                    input={pc.input || null}
                    loading={loading}
                    onApprove={(id) => void handleConfirmResume(id, true)}
                    onDeny={(id) => void handleConfirmResume(id, false)}
                  />
                ))}

              {error && (
                <div className="rounded-lg border border-status-error/30 bg-status-error/10 px-3 py-2 text-xs text-status-error">
                  <p className="mb-1 font-semibold">错误</p>
                  <p className="break-all">{error}</p>
                </div>
              )}
            </>
          )}
          <div ref={bottomRef} />
        </div>

        {/* 输入栏 */}
        <InputBar
          value={input}
          onChange={setInput}
          onSubmit={() => void handleSubmit()}
          isExecuting={isExecuting}
          runStatus={activeRunStatus || null}
          isSending={loading && isExecuting}
          canPause={canPause}
          canResume={canResume}
          onPause={() => void handlePause()}
          onResume={() => void handleResume()}
          queuedCount={queue.length}
          onCancelQueue={() => setQueue([])}
        />
      </div>

      {/* 右栏：实时面板 */}
      <AnimatePresence initial={false}>
        {realtimePanelOpen && (
          <motion.div
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 360, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="hidden min-h-0 shrink-0 overflow-hidden lg:block"
          >
            <RealtimePanel
              events={allEvents}
              runId={activeRunId}
              runStatus={activeRunStatus}
              isConnected={isConnected}
              onClear={() => setTimelineEvents([])}
            />
            <button
              onClick={toggleRealtimePanel}
              className="absolute right-0 top-3 text-xs text-text-muted hover:text-text-primary"
              style={{ display: 'none' }}
            >
              ▶
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* 展开实时面板按钮 */}
      {!realtimePanelOpen && (
        <button
          onClick={toggleRealtimePanel}
          className="absolute right-3 top-3 z-10 rounded-lg border border-border-soft bg-background-secondary/80 px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary"
        >
          实时面板 ▶
        </button>
      )}
    </div>
  )
}