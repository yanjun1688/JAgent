import { useMemo } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { ChevronDown, Brain, Loader2 } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import type { WsEvent } from '../../api/types'

export interface ThinkingPanelProps {
  events: WsEvent[]
  open: boolean
  loading: boolean
  onToggle: () => void
}

function summarize(events: WsEvent[]): string[] {
  const steps: string[] = []
  for (const e of events) {
    switch (e.event_type) {
      case 'ThinkStarted':
        steps.push('开始推理…')
        break
      case 'ThinkCompleted':
        steps.push('本轮推理完成')
        break
      case 'ActStarted':
        steps.push('准备调用工具')
        break
      case 'ObserveStarted':
        steps.push('观察工具结果')
        break
      case 'ObserveCompleted':
        steps.push('生成观察摘要')
        break
      case 'ContextCompacted':
        steps.push('上下文已压缩')
        break
    }
  }
  return steps
}

export function ThinkingPanel({ events, open, loading, onToggle }: ThinkingPanelProps) {
  const steps = useMemo(() => summarize(events), [events])

  return (
    <div className="overflow-hidden rounded-xl border border-border-soft bg-surface-2">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
      >
        <span
          className={cn(
            'flex h-6 w-6 items-center justify-center rounded-md',
            loading ? 'bg-status-info/20 text-status-info' : 'bg-accent-secondary/20 text-accent-secondary',
          )}
        >
          {loading ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Brain size={14} />
          )}
        </span>
        <span className="text-sm font-medium text-text-secondary">
          {loading ? 'Agent 正在思考…' : '思考过程'}
        </span>
        <span className="ml-auto text-xs text-text-muted">{steps.length} 步</span>
        <ChevronDown
          size={16}
          className={cn('text-text-muted transition-transform', open && 'rotate-180')}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <div className="max-h-48 space-y-1 overflow-y-auto px-4 pb-3">
              {steps.length === 0 ? (
                <p className="text-xs text-text-muted">等待事件…</p>
              ) : (
                steps.map((step, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs text-text-tertiary">
                    <span className="h-1 w-1 shrink-0 rounded-full bg-accent-primary" />
                    {step}
                  </div>
                ))
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}