import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StateSnapshotPanel } from '../../../src/components/replay/StateSnapshotPanel'
import { StateDiffPanel } from '../../../src/components/replay/StateDiffPanel'
import type { RunStateView, StateDiff } from '../../../src/api/schema'

const failedState: RunStateView = {
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
    steps: [
      { step_id: 's1', status: 'completed', tool_name: 'read', output_summary: 'ok' },
      { step_id: 's2', status: 'failed', tool_name: 'write', error: 'blocked by guardrail' },
    ],
  },
  tool_results: [
    { tool_call_id: 'tc1', tool_name: 'read', status: 'completed', duration_ms: 12, event_seq: 5 },
  ],
  guardrail_blocks: [
    {
      guardrail_id: 'no_write_outside_workspace',
      reason: 'path escapes workspace root',
      event_seq: 8,
      tool_call_id: 'tc2',
      tool_name: 'write',
    },
  ],
  pending_confirmations: [],
  thought_count: 0,
  orphaned: false,
}

describe('StateSnapshotPanel', () => {
  it('shows an instructional empty state before a point is selected', () => {
    render(<StateSnapshotPanel state={null} isLoading={false} error={null} selectedSeq={null} />)
    expect(screen.getByText(/点击左侧时间线上的任意事件/)).toBeInTheDocument()
  })

  it('renders the failed status, steps and guardrail block', () => {
    render(<StateSnapshotPanel state={failedState} isLoading={false} error={null} selectedSeq={11} />)
    expect(screen.getAllByText('已失败').length).toBeGreaterThan(0)
    expect(screen.getByText('s2')).toBeInTheDocument()
    expect(screen.getByText('no_write_outside_workspace')).toBeInTheDocument()
    expect(screen.getByText(/Plan failed: s2 failed/)).toBeInTheDocument()
  })

  it('renders a loading spinner while fetching', () => {
    const { container } = render(
      <StateSnapshotPanel state={null} isLoading error={null} selectedSeq={6} />,
    )
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('surfaces API errors without a white screen', () => {
    render(<StateSnapshotPanel state={null} isLoading={false} error="Run not found" selectedSeq={3} />)
    expect(screen.getByText('Run not found')).toBeInTheDocument()
  })
})

const diff: StateDiff = {
  run_id: 'run-1',
  from_seq: 6,
  to_seq: 11,
  status_change: { from_status: 'running', to_status: 'failed' },
  steps_changed: [
    { step_id: 's2', from_status: 'pending', to_status: 'failed', error: 'blocked' },
  ],
  tool_results_added: [],
  guardrails_triggered: [
    { guardrail_id: 'no_write_outside_workspace', reason: 'path escapes root', event_seq: 8 },
  ],
  error_change: { from_error: null, to_error: 'Plan failed: s2 failed' },
  events_in_range: [],
}

describe('StateDiffPanel', () => {
  it('prompts for A/B selection before any point is chosen', () => {
    render(<StateDiffPanel diff={null} isLoading={false} error={null} hasSelection={false} />)
    expect(screen.getByText(/起点 A/)).toBeInTheDocument()
  })

  it('prominently shows the status transition and changed step', () => {
    render(<StateDiffPanel diff={diff} isLoading={false} error={null} hasSelection />)
    expect(screen.getByTestId('diff-status-change')).toBeInTheDocument()
    expect(screen.getAllByText('已失败').length).toBeGreaterThan(0)
    expect(screen.getAllByText('运行中').length).toBeGreaterThan(0)
    expect(screen.getByText('s2')).toBeInTheDocument()
    expect(screen.getByText('no_write_outside_workspace')).toBeInTheDocument()
  })

  it('states clearly when status did not change', () => {
    const noChange: StateDiff = { ...diff, status_change: null, steps_changed: [] }
    render(<StateDiffPanel diff={noChange} isLoading={false} error={null} hasSelection />)
    expect(screen.getByText(/运行状态未发生变化/)).toBeInTheDocument()
    expect(screen.getByText(/没有步骤状态发生变化/)).toBeInTheDocument()
  })

  it('surfaces errors', () => {
    render(<StateDiffPanel diff={null} isLoading={false} error="seq 999 is out of range" hasSelection />)
    expect(screen.getByText(/out of range/)).toBeInTheDocument()
  })
})
