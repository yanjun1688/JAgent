import { create } from 'zustand'
import { devtools, persist } from 'zustand/middleware'

export type Theme = 'nebula' | 'light'

const THEME_KEY = 'harness-theme'

interface UIState {
  sidebarOpen: boolean
  realtimePanelOpen: boolean
  expandedEventSeqs: Set<number>
  theme: Theme

  toggleSidebar: () => void
  toggleRealtimePanel: () => void
  toggleEventExpansion: (seq: number) => void
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
}

function readInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'nebula'
  try {
    const v = window.localStorage.getItem(THEME_KEY)
    return v === 'light' ? 'light' : 'nebula'
  } catch {
    return 'nebula'
  }
}

export const useUIStore = create<UIState>()(
  devtools(
    persist(
      (set) => ({
        sidebarOpen: true,
        realtimePanelOpen: true,
        expandedEventSeqs: new Set<number>(),
        theme: readInitialTheme(),

        toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
        toggleRealtimePanel: () =>
          set((state) => ({ realtimePanelOpen: !state.realtimePanelOpen })),
        toggleEventExpansion: (seq) =>
          set((state) => {
            const next = new Set(state.expandedEventSeqs)
            if (next.has(seq)) {
              next.delete(seq)
            } else {
              next.add(seq)
            }
            return { expandedEventSeqs: next }
          }),
        setTheme: (theme) => set({ theme }),
        toggleTheme: () =>
          set((state) => ({ theme: state.theme === 'nebula' ? 'light' : 'nebula' })),
      }),
      {
        name: THEME_KEY,
        // 仅持久化主题
        partialize: (s) => ({ theme: s.theme }),
      },
    ),
    { name: 'UIStore' },
  ),
)