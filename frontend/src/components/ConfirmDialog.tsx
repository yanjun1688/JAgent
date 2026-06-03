import React, { useState } from 'react'

interface Props {
  toolName: string
  onConfirm: (operatorId: string) => void
  onDeny: (operatorId: string) => void
  onClose: () => void
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  background: 'rgba(0,0,0,0.4)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1000,
}

const dialogStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 8,
  padding: 24,
  maxWidth: 420,
  width: '90%',
  boxShadow: '0 4px 24px rgba(0,0,0,0.2)',
}

export default function ConfirmDialog({ toolName, onConfirm, onDeny, onClose }: Props) {
  const [operatorId, setOperatorId] = useState('')
  const [error, setError] = useState('')

  function handleAction(action: (id: string) => void) {
    if (!operatorId.trim()) {
      setError('Operator ID is required')
      return
    }
    action(operatorId.trim())
  }

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={dialogStyle} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ margin: '0 0 8px' }}>Confirm Tool Execution</h3>
        <p style={{ color: '#666', marginBottom: 16 }}>
          The agent wants to execute a potentially dangerous operation:
        </p>
        <div
          style={{
            background: '#fff3e0',
            border: '1px solid #ffe0b2',
            borderRadius: 4,
            padding: 12,
            marginBottom: 16,
            fontFamily: 'monospace',
          }}
        >
          Tool: <strong>{toolName}</strong>
        </div>
        <div style={{ marginBottom: 16 }}>
          <label style={{ display: 'block', marginBottom: 4, fontSize: 13, color: '#666' }}>
            Operator ID
          </label>
          <input
            type="text"
            value={operatorId}
            onChange={(e) => { setOperatorId(e.target.value); setError('') }}
            placeholder="Enter your operator ID"
            style={{
              width: '100%',
              padding: '8px 12px',
              border: '1px solid #ccc',
              borderRadius: 4,
              fontSize: 14,
              boxSizing: 'border-box',
            }}
          />
          {error && <p style={{ color: '#ef5350', fontSize: 13, margin: '4px 0 0' }}>{error}</p>}
        </div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={() => handleAction(onDeny)}
            style={{
              padding: '8px 20px',
              background: '#ef5350',
              color: '#fff',
              border: 'none',
              borderRadius: 4,
              cursor: 'pointer',
            }}
          >
            Deny
          </button>
          <button
            onClick={() => handleAction(onConfirm)}
            style={{
              padding: '8px 20px',
              background: '#66bb6a',
              color: '#fff',
              border: 'none',
              borderRadius: 4,
              cursor: 'pointer',
            }}
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  )
}
