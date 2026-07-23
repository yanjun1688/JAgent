import React, { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import ConversationView from '../components/ConversationView'
import OpsRealTimePanel from '../components/OpsRealTimePanel'
import { useRunWebSocket } from '../hooks/useRunWebSocket'
import { restoreCurrentConversationId } from '../api/conversation-client'

export default function OpsChatView() {
  const [searchParams] = useSearchParams()
  const [activeRunId, setActiveRunId] = useState<string | null>(searchParams.get('runId') || null)

  const handleActiveRunChange = useCallback((runId: string | null) => {
    setActiveRunId(runId)
  }, [])

  return (
    <div style={{ display: 'flex', gap: 12, height: 'calc(100vh - 60px)' }}>
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <ConversationView
          style={{ flex: 1 }}
          initialConversationId={restoreCurrentConversationId() || undefined}
          onActiveRunChange={handleActiveRunChange}
        />
      </div>
      <div style={{ width: 420, flexShrink: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <OpsRealTimeView activeRunId={activeRunId} />
      </div>
    </div>
  )
}

function OpsRealTimeView({ activeRunId }: { activeRunId: string | null }) {
  const { events, runStatus, isConnected } = useRunWebSocket(activeRunId)

  return (
    <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
      <OpsRealTimePanel
        runId={activeRunId}
        events={events}
        runStatus={runStatus || 'running'}
        isConnected={isConnected}
      />
    </div>
  )
}
