import { motion } from 'motion/react'
import { cn } from '../../design-system/utils/cn'
import { User, Sparkles } from 'lucide-react'

export interface MessageBubbleProps {
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  isStreaming?: boolean
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function MessageBubble({
  role,
  content,
  timestamp,
  isStreaming = false,
}: MessageBubbleProps) {
  const isUser = role === 'user'
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: 'spring', stiffness: 300, damping: 28 }}
      className={cn('flex w-full gap-3', isUser ? 'flex-row-reverse' : 'flex-row')}
    >
      <span
        className={cn(
          'mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
          isUser
            ? 'bg-accent-primary/20 text-accent-primary'
            : 'bg-accent-secondary/20 text-accent-secondary',
        )}
      >
        {isUser ? <User size={15} /> : <Sparkles size={15} />}
      </span>
      <div className={cn('flex max-w-[80%] flex-col gap-1', isUser ? 'items-end' : 'items-start')}>
        <div
          className={cn(
            'whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
            isUser
              ? 'gradient-nebula text-white'
              : 'glass-base text-text-primary',
            isUser ? 'rounded-tr-md' : 'rounded-tl-md',
          )}
        >
          {content}
          {isStreaming && (
            <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-text-primary align-middle" />
          )}
        </div>
        <span className="px-1 text-[10px] text-text-muted">{formatTime(timestamp)}</span>
      </div>
    </motion.div>
  )
}