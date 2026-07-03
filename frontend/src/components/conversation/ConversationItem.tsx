import { motion } from 'motion/react'
import { MessageSquare, Trash2 } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import type { Conversation } from '../../api/conversation-client'

export interface ConversationItemProps {
  conversation: Conversation
  isActive: boolean
  onSelect: (id: string) => void
  onDelete?: (id: string) => void
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  const now = new Date()
  const diff = now.getTime() - d.getTime()
  const day = 24 * 60 * 60 * 1000
  if (diff < day) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  if (diff < 7 * day) {
    return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
  }
  return d.toLocaleDateString('zh-CN')
}

export function ConversationItem({
  conversation,
  isActive,
  onSelect,
  onDelete,
}: ConversationItemProps) {
  return (
    <motion.button
      layout
      onClick={() => onSelect(conversation.conversation_id)}
      whileHover={{ scale: 1.01 }}
      whileTap={{ scale: 0.98 }}
      className={cn(
        'group flex w-full items-start gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors',
        isActive
          ? 'border-accent-primary/40 bg-accent-primary/10'
          : 'border-transparent hover:border-border-soft hover:bg-surface-1',
      )}
    >
      <span
        className={cn(
          'mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
          isActive ? 'bg-accent-primary/20 text-accent-primary' : 'bg-surface-1 text-text-tertiary',
        )}
      >
        <MessageSquare size={15} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <p
            className={cn(
              'truncate text-sm font-medium',
              isActive ? 'text-text-primary' : 'text-text-secondary',
            )}
          >
            {conversation.title || '新对话'}
          </p>
          <span className="shrink-0 text-[10px] text-text-muted">
            {formatTime(conversation.updated_at)}
          </span>
        </div>
        <p className="mt-0.5 text-xs text-text-tertiary">
          {conversation.message_count} 条消息
        </p>
      </div>
      {onDelete && (
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => {
            e.stopPropagation()
            onDelete(conversation.conversation_id)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.stopPropagation()
              onDelete(conversation.conversation_id)
            }
          }}
          className="hidden shrink-0 cursor-pointer rounded p-1 text-text-muted hover:bg-status-error/20 hover:text-status-error group-hover:block"
          aria-label="删除对话"
        >
          <Trash2 size={14} />
        </span>
      )}
    </motion.button>
  )
}