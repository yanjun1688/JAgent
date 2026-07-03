import { colors } from '../api/analysis-styles'

interface Props {
  confirmationId: string
  toolName: string
  toolCallId?: string
  riskLevel?: string
  input?: Record<string, unknown>
  onApprove: (confirmationId: string) => void
  onDeny: (confirmationId: string) => void
  loading?: boolean
}

export default function ConfirmationCard({
  confirmationId,
  toolName,
  riskLevel,
  input,
  onApprove,
  onDeny,
  loading,
}: Props) {
  const riskColor = riskLevel === 'high' ? '#d32f2f' : '#f57c00'

  return (
    <div
      style={{
        background: '#fff3e0',
        border: '1px solid #ffe0b2',
        borderRadius: 10,
        padding: 14,
        margin: '4px 0',
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: '#e65100', marginBottom: 6 }}>
        Confirmation Required
      </div>
      <div style={{ fontSize: 13, marginBottom: 6 }}>
        <strong>{toolName}</strong>
        {riskLevel && (
          <span
            style={{
              marginLeft: 6,
              fontSize: 11,
              color: riskColor,
              fontWeight: 600,
            }}
          >
            (risk: {riskLevel})
          </span>
        )}
      </div>
      {input && Object.keys(input).length > 0 && (
        <pre
          style={{
            margin: '0 0 8px',
            fontSize: 11,
            background: '#f5f5f5',
            padding: 8,
            borderRadius: 4,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
            maxHeight: 120,
            overflow: 'auto',
          }}
        >
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
        <button
          onClick={() => onDeny(confirmationId)}
          disabled={loading}
          style={{
            padding: '5px 14px',
            background: loading ? '#ef9a9a' : '#ef5350',
            color: '#fff',
            border: 'none',
            borderRadius: 6,
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          Deny & Continue
        </button>
        <button
          onClick={() => onApprove(confirmationId)}
          disabled={loading}
          style={{
            padding: '5px 14px',
            background: loading ? '#a5d6a7' : '#66bb6a',
            color: '#fff',
            border: 'none',
            borderRadius: 6,
            cursor: loading ? 'not-allowed' : 'pointer',
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          Approve & Continue
        </button>
      </div>
    </div>
  )
}
