import { useState, useRef, useEffect } from 'react'
import { SendHorizontal, Pause, Play, ListOrdered, X } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import { motion, AnimatePresence } from 'motion/react'

export interface InputBarProps {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  isExecuting: boolean
  runStatus: string | null
  isSending: boolean
  canPause: boolean
  canResume: boolean
  onPause: () => void
  onResume: () => void
  queuedCount: number
  onCancelQueue: () => void
}

export function InputBar({
  value,
  onChange,
  onSubmit,
  isExecuting,
  runStatus,
  isSending,
  canPause,
  canResume,
  onPause,
  onResume,
  queuedCount,
  onCancelQueue,
}: InputBarProps) {
  const [focused, setFocused] = useState(false)
  const ref = useRef<HTMLTextAreaElement>(null)

  // 自动调整文本框高度
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [value])

  const disabled = isSending
  const placeholder = isExecuting
    ? '输入下一条消息将自动排队…'
    : '输入任务…  (Enter 发送, Shift+Enter 换行)'

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>): void {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!disabled && value.trim()) onSubmit()
    }
  }

  return (
    <div className="shrink-0 px-4 pb-4">
      <AnimatePresence>
        {queuedCount > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6 }}
            className="mb-2 flex items-center gap-2 rounded-lg border border-border-soft bg-surface-1 px-3 py-1.5 text-xs text-text-secondary"
          >
            <ListOrdered size={13} className="text-accent-primary" />
            <span>队列中 {queuedCount} 条待发送</span>
            <button
              onClick={onCancelQueue}
              className="ml-auto inline-flex items-center gap-1 text-text-muted hover:text-status-error"
            >
              <X size={12} /> 取消
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      <div
        className={cn(
          'glass-base flex items-end gap-2 rounded-2xl p-2 transition-shadow',
          focused && 'shadow-glow',
        )}
      >
        <textarea
          ref={ref}
          rows={1}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={placeholder}
          className="max-h-40 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-sm text-text-primary placeholder:text-text-muted focus:outline-none"
        />

        {canPause && (
          <button
            type="button"
            onClick={onPause}
            disabled={isSending}
            title="暂停 Run"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-status-warning/20 text-status-warning transition-colors hover:bg-status-warning/30 disabled:opacity-50"
          >
            <Pause size={16} />
          </button>
        )}
        {canResume && (
          <button
            type="button"
            onClick={onResume}
            disabled={isSending}
            title="恢复 Run"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-status-success/20 text-status-success transition-colors hover:bg-status-success/30 disabled:opacity-50"
          >
            <Play size={16} />
          </button>
        )}

        <button
          type="button"
          onClick={onSubmit}
          disabled={disabled || !value.trim()}
          title="发送"
          className={cn(
            'flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition-all',
            value.trim() && !disabled
              ? 'gradient-nebula text-white shadow-glow hover:scale-105'
              : 'bg-surface-1 text-text-muted',
          )}
        >
          <SendHorizontal size={16} />
        </button>
      </div>
      {runStatus && (
        <p className="mt-1.5 px-1 text-[10px] text-text-muted">
          Run 状态: {runStatus}
        </p>
      )}
    </div>
  )
}