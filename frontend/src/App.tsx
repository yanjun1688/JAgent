import { lazy, Suspense } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Header } from './components/Header'
import { LoadingScreen } from './components/LoadingScreen'
import { ParticleBackground } from './components/effects/ParticleBackground'
import { useTheme } from './hooks/useTheme'

const ChatPage = lazy(() => import('./pages/ChatPage'))
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const HistoryPage = lazy(() => import('./pages/HistoryPage'))
const WorkspacePage = lazy(() => import('./pages/WorkspacePage'))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 5,
      gcTime: 1000 * 60 * 30,
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
})

function App() {
  useTheme()
  return (
    <QueryClientProvider client={queryClient}>
      <div className="relative flex h-screen flex-col bg-background-primary">
        <div className="pointer-events-none absolute inset-0 z-0">
          <ParticleBackground className="h-full w-full opacity-60" />
        </div>
        <Header />
        <main className="relative z-10 min-h-0 flex-1">
          <Suspense fallback={<LoadingScreen />}>
            <Routes>
              {/* 新路由 */}
              <Route path="/" element={<ChatPage />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/workspaces" element={<WorkspacePage />} />

              {/* 旧路由重定向 → 3 页面 */}
              <Route path="/analysis" element={<Navigate to="/overview" replace />} />
              <Route
                path="/analysis/tools"
                element={<Navigate to="/overview" replace />}
              />
              <Route
                path="/analysis/guardrails"
                element={<Navigate to="/overview" replace />}
              />
              <Route
                path="/analysis/runs/:runId"
                element={<Navigate to="/history" replace />}
              />
              <Route path="/runs/:runId" element={<Navigate to="/history" replace />} />
              <Route path="/ops" element={<Navigate to="/overview" replace />} />
              <Route path="/ops/chat" element={<Navigate to="/" replace />} />
              <Route
                path="/ops/runs/:runId"
                element={<Navigate to="/history" replace />}
              />
              <Route path="/ops/system" element={<Navigate to="/overview" replace />} />

              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </QueryClientProvider>
  )
}

export default App
