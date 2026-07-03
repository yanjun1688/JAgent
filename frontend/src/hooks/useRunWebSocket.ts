import { useEffect, useRef, useState } from 'react'

export interface WsEvent {
  run_id: string
  seq: number
  event_type: string
  payload: Record<string, unknown>
  idempotency_key: string | null
  created_at: number
  tool_call_id: string | null
  tool_name: string | null
  input: Record<string, unknown> | null
  error: string | null
  duration_ms: number | null
  confirmation_id: string | null
}

const MAX_BACKOFF_MS = 30_000
const BASE_BACKOFF_MS = 1_000
const PING_INTERVAL_MS = 25_000

export function useRunWebSocket(
  runId: string | null,
  onEvent?: (event: WsEvent) => void,
) {
  const [events, setEvents] = useState<WsEvent[]>([])
  const [runStatus, setRunStatus] = useState<string>('')
  const [isConnected, setIsConnected] = useState(false)

  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  const wsRef = useRef<WebSocket | null>(null)
  const lastSeqRef = useRef(0)
  const runStatusRef = useRef('')
  const closedRef = useRef(false)
  const reconnectAttemptsRef = useRef(0)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!runId) {
      setEvents([])
      setRunStatus('')
      return
    }

    let disposed = false
    lastSeqRef.current = 0
    runStatusRef.current = ''
    reconnectAttemptsRef.current = 0

    function stopPing() {
      if (pingTimerRef.current) {
        clearInterval(pingTimerRef.current)
        pingTimerRef.current = null
      }
    }

    function startPing() {
      stopPing()
      pingTimerRef.current = setInterval(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send('ping')
        }
      }, PING_INTERVAL_MS)
    }

    function computeBackoff(): number {
      const base = Math.min(BASE_BACKOFF_MS * Math.pow(2, reconnectAttemptsRef.current), MAX_BACKOFF_MS)
      const jitter = Math.random() * 1_000
      return base + jitter
    }

    function scheduleReconnect(id: string) {
      if (disposed || runStatusRef.current === 'completed' || runStatusRef.current === 'failed') return
      const delay = computeBackoff()
      reconnectTimerRef.current = setTimeout(() => {
        if (!disposed) {
          reconnectAttemptsRef.current++
          connect(id)
        }
      }, delay)
    }

    function teardownSocket() {
      stopPing()
      const ws = wsRef.current
      if (ws) {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close(1000, 'teardown')
        }
        wsRef.current = null
      }
    }

    function connect(id: string) {
      if (disposed) return
      teardownSocket()

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/runs/${id}/events`)
      wsRef.current = ws
      setIsConnected(true)

      ws.onopen = () => {
        if (disposed) return
        setIsConnected(true)
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

          setEvents((prev) => [...prev, event])
          onEventRef.current?.(event)

          if (raw.event_type === 'RunCompleted') {
            runStatusRef.current = 'completed'
            setRunStatus('completed')
          } else if (raw.event_type === 'RunFailed') {
            runStatusRef.current = 'failed'
            setRunStatus('failed')
          } else if (raw.event_type === 'RunPaused') {
            runStatusRef.current = 'paused'
            setRunStatus('paused')
          } else if (raw.event_type === 'RunResumed') {
            runStatusRef.current = 'running'
            setRunStatus('running')
          }
        } catch { /* ignore non-json / malformed */ }
      }

      ws.onclose = (event) => {
        stopPing()
        wsRef.current = null
        setIsConnected(false)

        if (disposed) return
        if (event.code === 1000) return
        if (runStatusRef.current === 'completed' || runStatusRef.current === 'failed') return
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
      setIsConnected(false)
    }
  }, [runId])

  return { events, runStatus, isConnected }
}
