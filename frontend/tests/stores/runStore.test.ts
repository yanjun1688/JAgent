import { describe, it, expect, beforeEach } from 'vitest'
import { useRunStore } from '../../src/stores/runStore'
import type { WsEvent } from '../../src/api/types'

function mkEvent(seq: number, eventType = 'ToolCalled'): WsEvent {
  return {
    run_id: 'r1',
    seq,
    event_type: eventType,
    payload: {},
    idempotency_key: null,
    created_at: seq,
    tool_call_id: `t${seq}`,
    tool_name: 'foo',
    input: null,
    error: null,
    duration_ms: null,
    confirmation_id: null,
  }
}

describe('runStore', () => {
  beforeEach(() => {
    useRunStore.setState({
      activeRunId: null,
      runStatus: null,
      events: [],
      isWebSocketConnected: false,
    })
  })

  it('clears events when activating a new run', () => {
    useRunStore.getState().addEvent(mkEvent(1))
    expect(useRunStore.getState().events).toHaveLength(1)
    useRunStore.getState().setActiveRun('r-new')
    expect(useRunStore.getState().events).toEqual([])
    expect(useRunStore.getState().activeRunId).toBe('r-new')
  })

  it('keeps events ordered by seq on insertion regardless of order', () => {
    useRunStore.getState().addEvent(mkEvent(3))
    useRunStore.getState().addEvent(mkEvent(1))
    useRunStore.getState().addEvent(mkEvent(2))
    const seqs = useRunStore.getState().events.map((e) => e.seq)
    expect(seqs).toEqual([1, 2, 3])
  })

  it('tracks run status and connection flag', () => {
    useRunStore.getState().setRunStatus('completed')
    expect(useRunStore.getState().runStatus).toBe('completed')
    useRunStore.getState().setWebSocketConnected(true)
    expect(useRunStore.getState().isWebSocketConnected).toBe(true)
  })

  it('clears run via clearRun', () => {
    useRunStore.getState().setActiveRun('r1')
    useRunStore.getState().addEvent(mkEvent(1))
    useRunStore.getState().clearRun()
    expect(useRunStore.getState()).toMatchObject({
      activeRunId: null,
      runStatus: null,
      events: [],
    })
  })
})