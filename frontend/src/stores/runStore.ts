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
        set((state) => ({
          events: [...state.events, event].sort((a, b) => a.seq - b.seq),
        })),
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