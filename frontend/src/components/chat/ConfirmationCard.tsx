import { motion } from 'motion/react'
import { ShieldAlert, Check, X, Loader2 } from 'lucide-react'
import { cn } from '../../design-system/utils/cn'

export interface ConfirmationCardProps {
  confirmationId: string
  toolName: string
  riskLevel: string
  input?: Record<string, unknown> | null
  loading: boolean
  onApprove: (confirmationId: string) => void
  onDeny: (confirmationId: string) => void
}

export function ConfirmationCard({
  confirmationId,
  toolName,
  riskLevel,
  input,
  loading,
  onApprove,
  onDeny,
}: ConfirmationCardProps) {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.97 }}
      animate={{ opacity: 1, scale: 1 }}
      className="rounded-xl border border-status-warning/30 bg-status-warning/10 p-3"
    >
      <div className="flex items-center gap-2 text-status-warning">
        <ShieldAlert size={16} />
        <span className="text-sm font-semibold">需要操作员确认</span>
      </div>
      <div className="mt-2 flex items-center gap-2 text-sm text-text-primary">
        <span className="rounded-md bg-surface-1 px-2 py-0.5 font-mono text-xs">{toolName}</span>
        {riskLevel && (
          <span
            className={cn(
              'rounded-full px-2 py-0.5 text-xs font-medium',
              String(riskLevel).toLowerCase() === 'high'
                ? 'bg-status-error/20 text-status-error'
                : 'bg-status-warning/20 text-status-warning',
            )}
          >
            风险等级: {riskLevel}
          </span>
        )}
      </div>
      {input && Object.keys(input).length > 0 && (
        <pre className="mt-2 max-h-32 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-xs text-text-secondary">
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <button
          onClick={() => onDeny(confirmationId)}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded-lg bg-status-error/20 px-3 py-1.5 text-xs font-medium text-status-error transition-colors hover:bg-status-error/30 disabled:opacity-50"
        >
          {loading ? <Loader2 size={13} className="animate-spin" /> : <X size={13} />}
          拒绝并继续
        </button>
        <button
          onClick={() => onApprove(confirmationId)}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded-lg bg-status-success/20 px-3 py-1.5 text-xs font-medium text-status-success transition-colors hover:bg-status-success/30 disabled:opacity-50"
        >
          {loading ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
          批准并继续
        </button>
      </div>
    </motion.div>
  )
}