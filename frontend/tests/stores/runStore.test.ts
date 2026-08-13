import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { useRunStore } from '../../src/stores/runStore'
import type { WsEvent } from '../../src/api/types'

function mkEvent(seq: number, eventType = 'ToolCalled', runId = 'r1'): WsEvent {
  return {
    run_id: runId,
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

  // ── P0-07: WS 事件入口受信过滤 ─────────────────────────────────
  describe('event isolation (P0-07)', () => {
    afterEach(() => {
      vi.restoreAllMocks()
    })

    it('adds events matching the active run', () => {
      useRunStore.getState().setActiveRun('run-a')
      useRunStore.getState().addEvent(mkEvent(1, 'ToolCalled', 'run-a'))
      expect(useRunStore.getState().events).toHaveLength(1)
      expect(useRunStore.getState().events[0].run_id).toBe('run-a')
    })

    it('drops events whose run_id does not match the active run', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      useRunStore.getState().setActiveRun('run-a')
      useRunStore.getState().addEvent(mkEvent(5, 'RunFailed', 'run-b'))
      expect(useRunStore.getState().events).toHaveLength(0)
      expect(warn).toHaveBeenCalled()
    })

    it('keeps sorting by seq after dropping foreign events', () => {
      useRunStore.getState().setActiveRun('run-a')
      useRunStore.getState().addEvent(mkEvent(2, 'AgentThought', 'run-a'))
      useRunStore.getState().addEvent(mkEvent(1, 'ToolCalled', 'run-b')) // dropped
      useRunStore.getState().addEvent(mkEvent(1, 'RunStarted', 'run-a'))
      const seqs = useRunStore.getState().events.map((e) => e.seq)
      expect(seqs).toEqual([1, 2])
    })
  })
})