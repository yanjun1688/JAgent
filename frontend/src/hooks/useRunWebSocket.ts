import { useEffect, useRef } from 'react'
import { useRunStore } from '../stores/runStore'
import type { WsEvent, RunStatus } from '../api/types'

// Re-export shared types so existing imports keep working
export type { WsEvent, RunStatus }

const MAX_BACKOFF_MS = 30_000
const BASE_BACKOFF_MS = 1_000
const PING_INTERVAL_MS = 25_000

function mapEventTypeToStatus(eventType: string): RunStatus | null {
  switch (eventType) {
    case 'RunCompleted':
      return 'completed'
    case 'RunFailed':
      return 'failed'
    case 'RunPaused':
      return 'paused'
    case 'RunResumed':
      return 'running'
    case 'RunStarted':
      return 'running'
    default:
      return null
  }
}

export interface UseRunWebSocketResult {
  events: WsEvent[]
  runStatus: RunStatus | null
  isConnected: boolean
}

/**
 * 订阅指定 Run 的实时事件流。
 *
 * 该 Hook 是受信边界的"事件入口"：所有 WebSocket 消息在此被解析后
 * 通过 runStore.addEvent 写入客户端事件流，组件只读 store 即可。
 * 自动按 seq 排序、断线指数退避重连、最终态停止重连。
 */
export function useRunWebSocket(runId: string | null): UseRunWebSocketResult {
  const addEvent = useRunStore((s) => s.addEvent)
  const setRunStatus = useRunStore((s) => s.setRunStatus)
  const setWebSocketConnected = useRunStore((s) => s.setWebSocketConnected)
  const setActiveRun = useRunStore((s) => s.setActiveRun)

  const storeEvents = useRunStore((s) => s.events)
  const storeRunStatus = useRunStore((s) => s.runStatus)
  const storeConnected = useRunStore((s) => s.isWebSocketConnected)

  const wsRef = useRef<WebSocket | null>(null)
  const lastSeqRef = useRef(0)
  const runStatusRef = useRef<RunStatus | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const prevRunIdRef = useRef<string | null>(null)

  useEffect(() => {
    if (!runId) {
      setActiveRun(null)
      prevRunIdRef.current = null
      return
    }

    const runIdChanged = prevRunIdRef.current !== runId
    prevRunIdRef.current = runId

    if (runIdChanged) {
      setActiveRun(runId)
      lastSeqRef.current = 0
      runStatusRef.current = null
      reconnectAttemptsRef.current = 0
    }

    let disposed = false

    function stopPing(): void {
      if (pingTimerRef.current) {
        clearInterval(pingTimerRef.current)
        pingTimerRef.current = null
      }
    }

    function startPing(): void {
      stopPing()
      pingTimerRef.current = setInterval(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send('ping')
        }
      }, PING_INTERVAL_MS)
    }

    function computeBackoff(): number {
      const base = Math.min(
        BASE_BACKOFF_MS * Math.pow(2, reconnectAttemptsRef.current),
        MAX_BACKOFF_MS,
      )
      const jitter = Math.random() * 1_000
      return base + jitter
    }

    function scheduleReconnect(id: string): void {
      if (
        disposed ||
        runStatusRef.current === 'completed' ||
        runStatusRef.current === 'failed'
      ) {
        return
      }
      const delay = computeBackoff()
      reconnectTimerRef.current = setTimeout(() => {
        if (!disposed) {
          reconnectAttemptsRef.current++
          connect(id)
        }
      }, delay)
    }

    function teardownSocket(): void {
      stopPing()
      const ws = wsRef.current
        if (ws) {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        if (
          ws.readyState === WebSocket.OPEN ||
          ws.readyState === WebSocket.CONNECTING
        ) {
          ws.close(1000, 'teardown')
        }
        wsRef.current = null
      }
    }

    function connect(id: string): void {
      if (disposed) return
      teardownSocket()

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/runs/${id}/events`)
      wsRef.current = ws

      ws.onopen = () => {
        if (disposed) return
        setWebSocketConnected(true)
        reconnectAttemptsRef.current = 0
        startPing()
      }

      ws.onmessage = (msg) => {
        if (disposed) return
        try {
          const raw = JSON.parse(msg.data)
          if (!raw.event_type) return
          if (raw.seq <= lastSeqRef.current) return
          lastSeqRef.current = raw.seq

          const event: WsEvent = {
            run_id: raw.run_id || id,
            seq: raw.seq,
            event_type: raw.event_type,
            payload: raw.payload || {},
            created_at: raw.created_at,
            idempotency_key: raw.idempotency_key || null,
            tool_call_id: raw.payload?.tool_call_id || null,
            tool_name: raw.payload?.tool_name || null,
            input: raw.payload?.input || null,
            error: raw.payload?.error || null,
            duration_ms: raw.payload?.duration_ms || null,
            confirmation_id: raw.payload?.confirmation_id || null,
          }

          addEvent(event)

          const nextStatus = mapEventTypeToStatus(raw.event_type)
          if (nextStatus) {
            runStatusRef.current = nextStatus
            setRunStatus(nextStatus)
          }
        } catch {
          /* ignore non-json / malformed */
        }
      }

      ws.onclose = (event) => {
        stopPing()
        wsRef.current = null
        setWebSocketConnected(false)
        if (disposed) return
        if (event.code === 1000) return
        if (
          runStatusRef.current === 'completed' ||
          runStatusRef.current === 'failed'
        ) {
          return
        }
        scheduleReconnect(id)
      }

      ws.onerror = () => {
        // onclose fires afterwards and handles reconnect
      }
    }

    connect(runId)

    return () => {
      disposed = true
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      teardownSocket()
      setWebSocketConnected(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  return {
    events: storeEvents,
    runStatus: storeRunStatus,
    isConnected: storeConnected,
  }
}