import { colors } from '../api/analysis-styles'

interface Props {
  count: number
  onCancel?: () => void
}

export default function PendingIndicator({ count, onCancel }: Props) {
  if (count <= 0) return null

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '6px 12px',
        background: '#e3f2fd',
        borderTop: `1px solid ${colors.border}`,
        fontSize: 12,
        color: colors.textSecondary,
      }}
    >
      <span>
        <span
          style={{
            display: 'inline-block',
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: colors.blue,
            marginRight: 6,
            animation: 'pending-pulse 1.2s ease-in-out infinite',
          }}
        />
        {count === 1 ? '1 message pending' : `${count} messages pending`}
      </span>
      {onCancel && (
        <button
          onClick={onCancel}
          style={{
            border: 'none',
            background: 'transparent',
            color: colors.red,
            cursor: 'pointer',
            fontSize: 11,
            fontWeight: 600,
          }}
        >
          Cancel all
        </button>
      )}
    </div>
  )
}

export function pendingPulseKeyframes() {
  return `
    @keyframes pending-pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.3; transform: scale(0.6); }
    }
  `
}
