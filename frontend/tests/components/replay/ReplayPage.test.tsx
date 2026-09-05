import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../../src/api/replay-client', () => ({
  getReplayRunMeta: vi.fn(),
  getReplayTimeline: vi.fn(),
  getReplayState: vi.fn(),
  getReplayDiff: vi.fn(),
  ReplayApiError: class ReplayApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

vi.mock('../../../src/api/client', () => ({
  listRuns: vi.fn(async () => ({ runs: [], total: 0 })),
  listWorkspaces: vi.fn(async () => ({ workspaces: [] })),
}))

import {
  getReplayRunMeta,
  getReplayTimeline,
  getReplayState,
  getReplayDiff,
} from '../../../src/api/replay-client'
import ReplayPage from '../../../src/pages/ReplayPage'

const meta = {
  run_id: 'run-1',
  status: 'failed',
  intent: 'do thing',
  latest_seq: 11,
  event_count: 11,
  created_at: 0,
  langfuse_trace_url: null,
}

const timeline = {
  run_id: 'run-1',
  latest_seq: 11,
  total: 4,
  next_cursor: 0,
  has_more: false,
  timeline: [
    { seq: 1, event_type: 'RunStarted', created_at: 0, payload: {}, is_terminal: false },
    { seq: 6, event_type: 'DagStepCompleted', created_at: 0, payload: {}, is_terminal: false, step_id: 's1' },
    { seq: 8, event_type: 'GuardrailTriggered', created_at: 0, payload: {}, is_terminal: false, tool_name: 'write' },
    { seq: 11, event_type: 'RunFailed', created_at: 0, payload: {}, is_terminal: true },
  ],
}

const stateAt11 = {
  run_id: 'run-1',
  at_seq: 11,
  latest_seq: 11,
  is_latest: true,
  status: 'failed',
  intent: 'do thing',
  last_error: 'Plan failed: s2 failed',
  plan: {
    plan_id: 'p1',
    intent: 'do thing',
    status: 'failed',
    steps: [{ step_id: 's2', status: 'failed', tool_name: 'write', error: 'blocked' }],
  },
  tool_results: [],
  guardrail_blocks: [
    { guardrail_id: 'no_write_outside_workspace', reason: 'path escapes root', event_seq: 8 },
  ],
  pending_confirmations: [],
  thought_count: 0,
  orphaned: false,
}

const diff6to11 = {
  run_id: 'run-1',
  from_seq: 6,
  to_seq: 11,
  status_change: { from_status: 'running', to_status: 'failed' },
  steps_changed: [{ step_id: 's2', from_status: 'pending', to_status: 'failed' }],
  tool_results_added: [],
  guardrails_triggered: [
    { guardrail_id: 'no_write_outside_workspace', reason: 'path escapes root', event_seq: 8 },
  ],
  error_change: { from_error: null, to_error: 'Plan failed: s2 failed' },
  events_in_range: [],
}

function renderPage(initial = '/replay/run-1') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/replay" element={<ReplayPage />} />
          <Route path="/replay/:runId" element={<ReplayPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getReplayRunMeta).mockResolvedValue(meta as never)
  vi.mocked(getReplayTimeline).mockResolvedValue(timeline as never)
  vi.mocked(getReplayState).mockResolvedValue(stateAt11 as never)
  vi.mocked(getReplayDiff).mockResolvedValue(diff6to11 as never)
})

describe('ReplayPage', () => {
  it('shows the empty-state prompt on /replay with no run', () => {
    renderPage('/replay')
    expect(screen.getByText(/粘贴 run_id 开始调试|选择一个 run/)).toBeInTheDocument()
  })

  it('loads a run and reconstructs the latest state', async () => {
    renderPage()
    await waitFor(() => expect(getReplayState).toHaveBeenCalled())
    expect((await screen.findAllByText('已失败')).length).toBeGreaterThan(0)
    expect(screen.getByText('no_write_outside_workspace')).toBeInTheDocument()
  })

  it('switches to compare mode and computes a diff after selecting A and B', async () => {
    const user = userEvent.setup()
    renderPage()
    await waitFor(() => expect(getReplayState).toHaveBeenCalled())

    await user.click(screen.getByRole('button', { name: '对比两时刻' }))
    // First click = A (seq 6), second = B (seq 11)
    await user.click(screen.getByText('#6'))
    await user.click(screen.getByText('#11'))

    await waitFor(() => expect(getReplayDiff).toHaveBeenCalledWith('run-1', 6, 11))
    expect(await screen.findByTestId('diff-status-change')).toBeInTheDocument()
  })

  it('reports a not-found run clearly instead of a white screen', async () => {
    vi.mocked(getReplayRunMeta).mockRejectedValueOnce({ status: 404, message: 'Run not found' })
    vi.mocked(getReplayTimeline).mockRejectedValueOnce({ status: 404, message: 'Run not found' })
    renderPage()
    expect(await screen.findByText(/找不到该 run/)).toBeInTheDocument()
  })
})
