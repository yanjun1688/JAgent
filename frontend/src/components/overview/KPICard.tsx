import { motion } from 'motion/react'
import { type LucideIcon } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'
import { GlassCard } from '../ui/GlassCard'

export interface KPICardProps {
  label: string
  value: number | string
  icon: LucideIcon
  accent?: 'primary' | 'secondary' | 'tertiary' | 'success' | 'warning' | 'error' | 'info'
  hint?: string
  isLoading?: boolean
}

const accentMap = {
  primary: 'text-accent-primary bg-accent-primary/15',
  secondary: 'text-accent-secondary bg-accent-secondary/15',
  tertiary: 'text-accent-tertiary bg-accent-tertiary/15',
  success: 'text-status-success bg-status-success/15',
  warning: 'text-status-warning bg-status-warning/15',
  error: 'text-status-error bg-status-error/15',
  info: 'text-status-info bg-status-info/15',
} as const

export function KPICard({ label, value, icon: Icon, accent = 'primary', hint, isLoading }: KPICardProps) {
  return (
    <GlassCard interactive className="px-4 py-3">
      <div className="flex items-start gap-3">
        <span
          className={cn(
            'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl',
            accentMap[accent],
          )}
        >
          <Icon size={18} />
        </span>
        <div className="min-w-0">
          <p className="truncate text-xs text-text-tertiary">{label}</p>
          {isLoading ? (
            <div className="mt-1 h-6 w-16 animate-pulse rounded bg-surface-3" />
          ) : (
            <motion.p
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              className="font-display text-2xl font-bold text-text-primary"
            >
              {value}
            </motion.p>
          )}
          {hint && <p className="mt-0.5 text-[10px] text-text-muted">{hint}</p>}
        </div>
      </div>
    </GlassCard>
  )
}