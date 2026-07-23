import { useMutation, useQueryClient } from '@tanstack/react-query'
import { pauseRun, resumeRun, confirmAction } from '../api/client'

const RUNS_QUERY_KEY = ['runs'] as const

export function useRunControl() {
  const queryClient = useQueryClient()

  const pauseMutation = useMutation({
    mutationFn: (runId: string) => pauseRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: RUNS_QUERY_KEY })
    },
  })

  const resumeMutation = useMutation({
    mutationFn: (runId: string) => resumeRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: RUNS_QUERY_KEY })
    },
  })

  const confirmMutation = useMutation({
    mutationFn: ({
      runId,
      confirmationId,
      confirmed,
      operatorId,
    }: {
      runId: string
      confirmationId: string
      confirmed: boolean
      operatorId: string
    }) => confirmAction(runId, confirmationId, confirmed, operatorId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: RUNS_QUERY_KEY })
    },
  })

  return {
    pause: pauseMutation.mutateAsync,
    pauseAsync: pauseMutation.mutateAsync,
    pausePending: pauseMutation.isPending,
    resume: resumeMutation.mutateAsync,
    resumeAsync: resumeMutation.mutateAsync,
    resumePending: resumeMutation.isPending,
    confirm: confirmMutation.mutateAsync,
    confirmAsync: confirmMutation.mutateAsync,
    confirmPending: confirmMutation.isPending,
  }
}