import { cn } from '../../design-system/utils/cn'

export type Status = 'running' | 'paused' | 'completed' | 'failed'

export interface StatusBadgeProps {
  status: Status
  className?: string
}

const statusColors: Record<Status, string> = {
  running: 'bg-status-info text-white',
  paused: 'bg-status-warning text-white',
  completed: 'bg-status-success text-white',
  failed: 'bg-status-error text-white',
}

const statusLabels: Record<Status, string> = {
  running: '运行中',
  paused: '已暂停',
  completed: '已完成',
  failed: '已失败',
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        statusColors[status],
        className,
      )}
    >
      {statusLabels[status]}
    </span>
  )
}