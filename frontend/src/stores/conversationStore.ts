import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import type { Conversation } from '../api/conversation-client'

interface ConversationState {
  activeConversationId: string | null
  conversations: Conversation[]
  searchQuery: string

  setActiveConversation: (id: string | null) => void
  setConversations: (conversations: Conversation[]) => void
  setSearchQuery: (query: string) => void
  addConversation: (conversation: Conversation) => void
  removeConversation: (id: string) => void
  updateConversation: (id: string, updates: Partial<Conversation>) => void
}

export const useConversationStore = create<ConversationState>()(
  devtools(
    (set) => ({
      activeConversationId: null,
      conversations: [],
      searchQuery: '',

      setActiveConversation: (id) => set({ activeConversationId: id }),
      setConversations: (conversations) => set({ conversations }),
      setSearchQuery: (query) => set({ searchQuery: query }),
      addConversation: (conversation) =>
        set((state) => ({
          conversations: [conversation, ...state.conversations],
        })),
      removeConversation: (id) =>
        set((state) => ({
          conversations: state.conversations.filter((c) => c.conversation_id !== id),
          activeConversationId:
            state.activeConversationId === id ? null : state.activeConversationId,
        })),
      updateConversation: (id, updates) =>
        set((state) => ({
          conversations: state.conversations.map((c) =>
            c.conversation_id === id ? { ...c, ...updates } : c,
          ),
        })),
    }),
    { name: 'ConversationStore' },
  ),
)