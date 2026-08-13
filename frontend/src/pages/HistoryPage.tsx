import { useState } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { useQuery } from '@tanstack/react-query'
import { History as HistoryIcon, Loader2 } from 'lucide-react'
import { listRuns, listWorkspaces } from '../api/client'
import { RunTimeline } from '../components/history/RunTimeline'
import { RunDetailPanel } from '../components/history/RunDetailPanel'

export default function HistoryPage() {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<string | null>(null)
  const [workspaceId, setWorkspaceId] = useState<string | undefined>()
  const { data: workspaceData } = useQuery({ queryKey: ['workspaces'], queryFn: listWorkspaces })

  const {
    data,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['runs', workspaceId],
    queryFn: () => listRuns(workspaceId),
    refetchInterval: 5000,
  })

  const runs = data?.runs ?? []
  const apiError = error ? (error instanceof Error ? error.message : String(error)) : null

  return (
    <div className="min-h-0 flex-1 px-4 py-4">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="mx-auto flex h-full max-w-7xl gap-3"
      >
        {/* 左栏：时间线 */}
        <div className="hidden min-h-0 w-80 shrink-0 flex-col overflow-hidden rounded-2xl glass-base md:flex">
          <div className="mb-1 flex items-center gap-2 px-4 pt-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-primary/20 text-accent-primary">
              <HistoryIcon size={16} />
            </span>
            <div>
              <h1 className="font-display text-base font-bold text-text-primary">Run 历史</h1>
              <p className="text-[10px] text-text-tertiary">每 5 秒自动刷新 · 共 {runs.length} 条</p>
              <select value={workspaceId ?? ''} onChange={(event) => setWorkspaceId(event.target.value || undefined)} className="mt-2 w-full rounded-lg border border-border-soft bg-surface-1 px-2 py-1 text-xs text-text-secondary">
                <option value="">全部 Workspace</option>
                {(workspaceData?.workspaces ?? []).map((workspace) => <option key={workspace.workspace_id} value={workspace.workspace_id}>{workspace.name}</option>)}
              </select>
            </div>
          </div>
          <RunTimeline
            className="min-h-0 flex-1"
            runs={runs}
            selectedRunId={selectedRunId}
            searchQuery={searchQuery}
            statusFilter={statusFilter}
            isLoading={isLoading && runs.length === 0}
            error={apiError}
            onSearchChange={setSearchQuery}
            onFilterChange={setStatusFilter}
            onSelect={setSelectedRunId}
          />
        </div>

        {/* 右栏：详情面板 */}
        <div className="flex min-h-0 flex-1 min-w-0">
          {selectedRunId ? (
            <AnimatePresence mode="wait">
              <motion.div
                key={selectedRunId}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="h-full w-full"
              >
                <RunDetailPanel
                  runId={selectedRunId}
                  onClose={() => setSelectedRunId(null)}
                />
              </motion.div>
            </AnimatePresence>
          ) : (
            <div className="flex h-full w-full items-center justify-center rounded-2xl glass-base">
              <div className="flex flex-col items-center gap-3 text-text-muted">
                {isLoading ? (
                  <Loader2 size={28} className="animate-spin text-accent-primary" />
                ) : (
                  <HistoryIcon size={28} />
                )}
                <p className="text-sm">
                  {isLoading ? '加载中…' : '选择左侧的 Run 查看详情'}
                </p>
              </div>
            </div>
          )}
        </div>
      </motion.div>
    </div>
  )
}
