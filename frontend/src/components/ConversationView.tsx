import { useCallback, useState } from 'react'
import { colors } from '../api/analysis-styles'
import { restoreCurrentConversationId } from '../api/conversation-client'
import ConversationSidebar from './ConversationSidebar'
import ConversationDrawer from './ConversationDrawer'

interface Props {
  style?: React.CSSProperties
  initialConversationId?: string
  onActiveRunChange?: (runId: string | null) => void
}

export default function ConversationView({ style, initialConversationId, onActiveRunChange }: Props) {
  const [activeConvId, setActiveConvId] = useState<string | null>(
    initialConversationId || restoreCurrentConversationId() || null,
  )
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0)

  const handleSelect = useCallback((convId: string) => {
    setActiveConvId(convId)
  }, [])

  const handleNew = useCallback(() => {
    setActiveConvId(null)
    setSidebarRefreshKey((k) => k + 1)
  }, [])

  const handleConversationChange = useCallback(
    (convId: string | null) => {
      if (convId) {
        setActiveConvId(convId)
        setSidebarRefreshKey((k) => k + 1)
      }
    },
    [],
  )

  return (
    <div
      style={{
        display: 'flex',
        height: '100%',
        ...style,
      }}
    >
      {sidebarOpen && (
        <ConversationSidebar
          activeConversationId={activeConvId}
          onSelect={handleSelect}
          onNew={handleNew}
          refreshKey={sidebarRefreshKey}
        />
      )}

      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          position: 'relative',
        }}
      >
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          style={{
            position: 'absolute',
            left: 0,
            top: 12,
            zIndex: 10,
            border: `1px solid ${colors.border}`,
            background: '#fff',
            borderRadius: '0 6px 6px 0',
            padding: '4px 6px',
            cursor: 'pointer',
            fontSize: 12,
            color: colors.textSecondary,
          }}
          title={sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'}
        >
          {sidebarOpen ? '\u25C0' : '\u25B6'}
        </button>

        <ConversationDrawer
          key={activeConvId || 'new'}
          initialConversationId={activeConvId || undefined}
          onConversationChange={handleConversationChange}
          onActiveRunChange={onActiveRunChange}
          onNewConversation={handleNew}
          style={{ flex: 1 }}
        />
      </div>
    </div>
  )
}
