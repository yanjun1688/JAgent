import { useEffect, useState } from 'react'
import { colors } from '../api/analysis-styles'
import { listConversations, deleteConversation } from '../api/conversation-client'
import type { Conversation } from '../api/conversation-client'

interface Props {
  activeConversationId: string | null
  onSelect: (convId: string) => void
  onNew: () => void
  refreshKey?: number
}

export default function ConversationSidebar({
  activeConversationId,
  onSelect,
  onNew,
  refreshKey,
}: Props) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const result = await listConversations()
      setConversations(result.conversations)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [refreshKey])

  async function handleDelete(convId: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await deleteConversation(convId)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== convId))
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
    }
  }

  const filtered = search.trim()
    ? conversations.filter(
        (c) =>
          c.title.toLowerCase().includes(search.toLowerCase()) ||
          c.conversation_id.toLowerCase().includes(search.toLowerCase()),
      )
    : conversations

  return (
    <div
      style={{
        width: 280,
        height: '100%',
        background: '#fafafa',
        borderRight: `1px solid ${colors.border}`,
        display: 'flex',
        flexDirection: 'column',
        flexShrink: 0,
      }}
    >
      <div
        style={{
          padding: '12px 14px',
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search conversations..."
          style={{
            flex: 1,
            padding: '6px 10px',
            border: `1px solid ${colors.border}`,
            borderRadius: 8,
            fontSize: 12,
            outline: 'none',
            background: '#fff',
          }}
        />
        <button
          onClick={onNew}
          style={{
            width: 32,
            height: 32,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            border: `1px solid ${colors.border}`,
            borderRadius: 8,
            background: colors.primary,
            color: '#fff',
            cursor: 'pointer',
            fontSize: 18,
            fontWeight: 700,
            flexShrink: 0,
          }}
        >
          +
        </button>
      </div>

      <div style={{ flex: 1, overflow: 'auto' }}>
        {loading && filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', color: colors.textSecondary, fontSize: 12 }}>
            Loading...
          </div>
        )}

        {!loading && filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', color: colors.textSecondary, fontSize: 12 }}>
            {search ? 'No matching conversations' : 'No conversations yet'}
          </div>
        )}

        {error && (
          <div
            style={{
              margin: 8,
              padding: 8,
              background: colors.redLight,
              borderRadius: 6,
              fontSize: 11,
              color: colors.red,
            }}
          >
            {error}
          </div>
        )}

        {filtered.map((conv) => {
          const isActive = conv.conversation_id === activeConversationId
          return (
            <div
              key={conv.conversation_id}
              onClick={() => onSelect(conv.conversation_id)}
              style={{
                padding: '10px 14px',
                cursor: 'pointer',
                borderBottom: `1px solid ${colors.border}`,
                background: isActive ? colors.blueLight : 'transparent',
                transition: 'background 0.1s',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: isActive ? colors.blue : colors.text,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {conv.title || 'New conversation'}
                  </div>
                  <div style={{ fontSize: 11, color: colors.textSecondary, marginTop: 2 }}>
                    {conv.message_count} message{conv.message_count !== 1 ? 's' : ''}
                    {' · '}
                    {new Date(conv.updated_at * 1000).toLocaleDateString()}
                  </div>
                </div>
                <button
                  onClick={(e) => handleDelete(conv.conversation_id, e)}
                  style={{
                    border: 'none',
                    background: 'transparent',
                    color: colors.textSecondary,
                    cursor: 'pointer',
                    fontSize: 14,
                    padding: '0 2px',
                    flexShrink: 0,
                    opacity: 0.5,
                  }}
                  title="Delete conversation"
                >
                  x
                </button>
              </div>
              {conv.status === 'archived' && (
                <span
                  style={{
                    display: 'inline-block',
                    marginTop: 4,
                    padding: '1px 6px',
                    borderRadius: 4,
                    fontSize: 10,
                    background: colors.grayLight,
                    color: colors.textSecondary,
                  }}
                >
                  archived
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
