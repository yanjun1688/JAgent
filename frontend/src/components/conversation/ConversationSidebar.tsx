import { useMemo } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { Plus, Search, Inbox } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import { slideUp } from '../../design-system/utils/motion'
import type { Conversation } from '../../api/conversation-client'
import { ConversationItem } from './ConversationItem'

export interface ConversationSidebarProps {
  conversations: Conversation[]
  activeConversationId: string | null
  searchQuery: string
  isLoading: boolean
  error: string | null
  onSearchChange: (query: string) => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onNew: () => void
  className?: string
}

export function ConversationSidebar({
  conversations,
  activeConversationId,
  searchQuery,
  isLoading,
  error,
  onSearchChange,
  onSelect,
  onDelete,
  onNew,
  className,
}: ConversationSidebarProps) {
  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return conversations
    return conversations.filter((c) =>
      (c.title || '新对话').toLowerCase().includes(q),
    )
  }, [conversations, searchQuery])

  return (
    <div className={cn('flex h-full w-72 shrink-0 flex-col glass-base', className)}>
      {/* 标题 + 新建按钮 */}
      <div className="flex items-center justify-between px-4 pb-3 pt-4">
        <h2 className="font-display text-sm font-semibold tracking-wide text-text-primary">
          对话
        </h2>
        <button
          onClick={onNew}
          className="flex items-center gap-1 rounded-lg bg-accent-primary/20 px-2.5 py-1.5 text-xs font-medium text-accent-primary transition-colors hover:bg-accent-primary/30"
          title="新对话"
        >
          <Plus size={14} />
          新建
        </button>
      </div>

      {/* 搜索 */}
      <div className="px-4 pb-3">
        <div className="relative">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
          />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="搜索对话..."
            className="w-full rounded-lg border border-border-soft bg-surface-1 py-2 pl-9 pr-3 text-sm text-text-primary placeholder:text-text-muted focus:border-accent-primary/50 focus:outline-none"
          />
        </div>
      </div>

      {/* 列表 */}
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-2">
        {error && (
          <p className="px-3 py-6 text-center text-xs text-status-error">{error}</p>
        )}
        {isLoading && (
          <div className="space-y-2 px-2 pt-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                className="h-12 animate-pulse rounded-xl bg-surface-1"
              />
            ))}
          </div>
        )}
        {!isLoading && filtered.length === 0 && !error && (
          <div className="flex flex-col items-center px-4 py-12 text-center">
            <Inbox size={28} className="mb-2 text-text-muted" />
            <p className="text-xs text-text-tertiary">
              {searchQuery ? '未找到匹配的对话' : '尚无对话，点击新建开始'}
            </p>
          </div>
        )}
        <AnimatePresence initial={false}>
          {filtered.map((c) => (
            <motion.div
              key={c.conversation_id}
              variants={slideUp}
              initial="hidden"
              animate="visible"
              exit="exit"
            >
              <ConversationItem
                conversation={c}
                isActive={c.conversation_id === activeConversationId}
                onSelect={onSelect}
                onDelete={onDelete}
              />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}