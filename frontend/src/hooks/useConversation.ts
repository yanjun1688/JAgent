import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  createConversation,
  deleteConversation,
  listConversations,
  sendMessage,
  updateConversation,
} from '../api/conversation-client'

const QUERY_KEY = ['conversations'] as const

export function useConversations() {
  const queryClient = useQueryClient()

  const { data, isLoading, error } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: listConversations,
  })

  const createMutation = useMutation({
    mutationFn: (title?: string) => createConversation(title),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string
      payload: { title?: string; status?: string }
    }) => updateConversation(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY })
    },
  })

  return {
    conversations: data?.conversations ?? [],
    total: data?.total ?? 0,
    isLoading,
    error,
    create: createMutation.mutateAsync,
    createAsync: createMutation.mutateAsync,
    remove: deleteMutation.mutateAsync,
    removeAsync: deleteMutation.mutateAsync,
    update: updateMutation.mutateAsync,
  }
}

export function useSendMessage() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ conversationId, message }: { conversationId: string; message: string }) =>
      sendMessage(conversationId, message),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY })
    },
  })
}