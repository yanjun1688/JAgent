import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Boxes, Trash2 } from 'lucide-react'
import { createWorkspace, deleteWorkspace, listWorkspaces } from '../api/client'

export default function WorkspacePage() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({ queryKey: ['workspaces'], queryFn: listWorkspaces })
  const [name, setName] = useState('')
  const [root, setRoot] = useState('data/workspaces/new/work')
  const create = useMutation({
    mutationFn: () => createWorkspace({ name, description: '', scope: { target: { type: 'directory', filesystem_root: root, port: 22 } } }),
    onSuccess: () => { setName(''); void queryClient.invalidateQueries({ queryKey: ['workspaces'] }) },
  })
  const remove = useMutation({
    mutationFn: deleteWorkspace,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['workspaces'] }),
  })
  const mutationError = create.error ?? remove.error

  return (
    <div className="min-h-full px-4 py-6">
      <div className="mx-auto max-w-5xl space-y-5">
        <div className="flex items-center gap-3">
          <Boxes className="text-accent-primary" />
          <div><h1 className="font-display text-xl font-bold text-text-primary">Workspaces</h1><p className="text-sm text-text-tertiary">每个 Workspace 定义一个受信执行边界。</p></div>
        </div>
        <form className="glass-base flex flex-wrap gap-3 rounded-2xl p-4" onSubmit={(event) => { event.preventDefault(); if (name.trim()) create.mutate() }}>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Workspace name" className="min-w-48 flex-1 rounded-lg border border-border-soft bg-surface-1 px-3 py-2 text-sm text-text-primary" />
          <input value={root} onChange={(event) => setRoot(event.target.value)} placeholder="Filesystem root" className="min-w-64 flex-1 rounded-lg border border-border-soft bg-surface-1 px-3 py-2 text-sm text-text-primary" />
          <button type="submit" disabled={create.isPending} className="rounded-lg bg-accent-primary px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">创建</button>
        </form>
        {isLoading && <p className="text-sm text-text-tertiary">加载中...</p>}
        {error && <p className="text-sm text-red-400">{String(error)}</p>}
        {mutationError && <p className="text-sm text-red-400">{mutationError instanceof Error ? mutationError.message : String(mutationError)}</p>}
        <div className="grid gap-3 md:grid-cols-2">
          {data?.workspaces.map((workspace) => (
            <article key={workspace.workspace_id} className="glass-base rounded-2xl p-4">
              <div className="flex items-start justify-between gap-3"><div><h2 className="font-semibold text-text-primary">{workspace.name}</h2><p className="mt-1 text-xs text-text-tertiary">{workspace.scope.target.type} · {workspace.scope.target.filesystem_root ?? workspace.scope.target.remote_root}</p></div>
                {workspace.workspace_id === 'default' ? (
                  <span className="text-xs text-text-tertiary" title="default workspace 不可删除">系统</span>
                ) : (
                  <button aria-label={`删除 ${workspace.name}`} onClick={() => { if (window.confirm(`删除 ${workspace.name}?`)) remove.mutate(workspace.workspace_id) }} className="rounded-lg p-2 text-text-tertiary hover:bg-red-500/10 hover:text-red-400"><Trash2 size={16} /></button>
                )}
              </div>
              <div className="mt-4 flex gap-4 text-xs text-text-secondary"><span>{workspace.run_count} runs</span><span>{workspace.status}</span><span>{workspace.scope.allowed_tools?.length ?? 0} tools</span></div>
            </article>
          ))}
        </div>
      </div>
    </div>
  )
}
