import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { WsEvent } from '../api/types'

// Mutable fixture the mocked hook returns; set per test.
const wsMock = vi.hoisted(() => ({
  events: [] as WsEvent[],
  runStatus: 'running' as string | null,
  isConnected: false,
}))

vi.mock('../hooks/useRunWebSocket', () => ({
  useRunWebSocket: () => ({
    events: wsMock.events,
    runStatus: wsMock.runStatus,
    isConnected: wsMock.isConnected,
  }),
}))

vi.mock('../api/analysis-client', () => ({
  getRunTimeline: vi.fn(() => Promise.resolve({ timeline: [] })),
}))

vi.mock('../api/client', () => ({
  createRun: vi.fn(),
  confirmAction: vi.fn(),
  pauseRun: vi.fn(),
  resumeRun: vi.fn(),
}))

import ChatDrawer from './ChatDrawer'

describe('ChatDrawer failed-message boundary (P0-07)', () => {
  beforeAll(() => {
    wsMock.runStatus = 'running'
  })

  afterAll(() => {
    wsMock.events = []
  })

  it('renders user_facing_message, never final_error, on RunFailed', async () => {
    wsMock.events = [
      {
        run_id: 'run-1',
        seq: 3,
        event_type: 'RunFailed',
        payload: {
          final_error: 'Steps not achieved: s1',
          user_facing_message: '任务未能完成，请检查任务要求或稍后重试。',
        },
        idempotency_key: null,
        created_at: 1000,
        tool_call_id: null,
        tool_name: null,
        input: null,
        error: null,
        duration_ms: null,
        confirmation_id: null,
      },
    ]

    render(<ChatDrawer initialRunId="run-1" />)

    expect(
      await screen.findByText('任务未能完成，请检查任务要求或稍后重试。'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Steps not achieved/)).not.toBeInTheDocument()
    expect(screen.queryByText(/s1/)).not.toBeInTheDocument()
  })

  it('falls back to a generic message when user_facing_message is missing', async () => {
    wsMock.events = [
      {
        run_id: 'run-2',
        seq: 4,
        event_type: 'RunFailed',
        payload: { final_error: 'DAG execution: 0/1 step(s) completed' },
        idempotency_key: null,
        created_at: 1000,
        tool_call_id: null,
        tool_name: null,
        input: null,
        error: null,
        duration_ms: null,
        confirmation_id: null,
      },
    ]

    render(<ChatDrawer initialRunId="run-2" />)

    expect(
      await screen.findByText('任务未能完成，请检查任务要求或稍后重试。'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/DAG execution/)).not.toBeInTheDocument()
  })
})
