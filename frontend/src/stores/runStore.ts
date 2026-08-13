import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import type { WsEvent, RunStatus } from '../api/types'

interface RunState {
  activeRunId: string | null
  runStatus: RunStatus | null
  events: WsEvent[]
  isWebSocketConnected: boolean

  setActiveRun: (runId: string | null) => void
  setRunStatus: (status: RunStatus | null) => void
  addEvent: (event: WsEvent) => void
  setEvents: (events: WsEvent[]) => void
  setWebSocketConnected: (connected: boolean) => void
  clearRun: () => void
}

export const useRunStore = create<RunState>()(
  devtools(
    (set) => ({
      activeRunId: null,
      runStatus: null,
      events: [],
      isWebSocketConnected: false,

      setActiveRun: (runId) =>
        set({
          activeRunId: runId,
          events: [],
          runStatus: null,
        }),
      setRunStatus: (status) => set({ runStatus: status }),
      addEvent: (event) =>
        set((state) => {
          // P0-07: WS 事件入口受信过滤 — 任何 run_id 与当前订阅不一致的事件
          // 一律丢弃并记录诊断日志，防止跨会话/跨 Run 事件进入渲染数据集。
          if (state.activeRunId && event.run_id && event.run_id !== state.activeRunId) {
            console.warn(
              `[runStore] Dropping event run_id=${event.run_id} seq=${event.seq} ` +
                `type=${event.event_type} (active run=${state.activeRunId})`,
            )
            return state
          }
          return {
            events: [...state.events, event].sort((a, b) => a.seq - b.seq),
          }
        }),
      setEvents: (events) => set({ events }),
      setWebSocketConnected: (connected) => set({ isWebSocketConnected: connected }),
      clearRun: () =>
        set({
          activeRunId: null,
          runStatus: null,
          events: [],
        }),
    }),
    { name: 'RunStore' },
  ),
)