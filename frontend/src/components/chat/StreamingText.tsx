import { motion } from 'motion/react'
import { cn } from '../../design-system/utils/cn'

export interface StreamingTextProps {
  text: string
  className?: string
  speed?: number
}

/**
 * 流式渲染文本：基于已计算好的 text 显示，附带光标动画。
 * 真正的流式分片由 WebSocket 事件驱动（每次 ChunkAppended 追加 text）。
 */
export function StreamingText({ text, className }: StreamingTextProps) {
  return (
    <span className={cn('whitespace-pre-wrap break-words', className)}>
      {text}
      <motion.span
        aria-hidden
        className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5"
        animate={{ opacity: [1, 0, 1] }}
        transition={{ duration: 1, repeat: Infinity }}
      >
        <span className="block h-full w-full bg-accent-primary" />
      </motion.span>
    </span>
  )
}