import { colors } from '../api/analysis-styles'

interface Props {
  role: 'user' | 'assistant'
  content: string
  timestamp?: number
}

export default function MessageBubble({ role, content, timestamp }: Props) {
  const isUser = role === 'user'

  return (
    <div
      style={{
        display: 'flex',
        justifyContent: isUser ? 'flex-end' : 'flex-start',
        marginBottom: 4,
      }}
    >
      {!isUser && (
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: '50%',
            background: '#7c4dff',
            color: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 13,
            marginRight: 8,
            flexShrink: 0,
            alignSelf: 'flex-end',
          }}
        >
          J
        </div>
      )}

      <div style={{ maxWidth: '85%' }}>
        <div
          style={{
            padding: '8px 14px',
            borderRadius: 14,
            borderBottomRightRadius: isUser ? 4 : 14,
            borderBottomLeftRadius: isUser ? 14 : 4,
            background: isUser ? '#1a73e8' : '#fff',
            border: isUser ? 'none' : `1px solid ${colors.border}`,
            color: isUser ? '#fff' : colors.text,
            fontSize: 13,
            lineHeight: 1.5,
            wordBreak: 'break-word',
            whiteSpace: 'pre-wrap',
          }}
        >
          {content}
        </div>

        {timestamp && (
          <div
            style={{
              fontSize: 10,
              color: colors.textSecondary,
              marginTop: 2,
              paddingLeft: isUser ? 4 : 4,
              textAlign: isUser ? 'right' : 'left',
            }}
          >
            {new Date(timestamp * 1000).toLocaleTimeString()}
          </div>
        )}
      </div>

      {isUser && (
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: '50%',
            background: colors.blue,
            color: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 13,
            marginLeft: 8,
            flexShrink: 0,
            alignSelf: 'flex-end',
          }}
        >
          U
        </div>
      )}
    </div>
  )
}
