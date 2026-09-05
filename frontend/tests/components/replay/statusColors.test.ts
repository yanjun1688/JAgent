import { describe, it, expect } from 'vitest'
import {
  statusTone,
  statusLabel,
  eventTypeColor,
  isNotFoundError,
  errorMessage,
} from '../../../src/components/replay/statusColors'

describe('replay statusColors helpers', () => {
  it('maps run/step statuses to semantic tones', () => {
    expect(statusTone('running')).toBe('info')
    expect(statusTone('completed')).toBe('success')
    expect(statusTone('failed')).toBe('error')
    expect(statusTone('guardrail_blocked')).toBe('warning')
    expect(statusTone('timeout')).toBe('warning')
    expect(statusTone('skipped')).toBe('warning')
    expect(statusTone('pending')).toBe('muted')
    expect(statusTone(undefined)).toBe('muted')
  })

  it('localizes known status labels and falls back to the raw value', () => {
    expect(statusLabel('failed')).toBe('已失败')
    expect(statusLabel('guardrail_blocked')).toBe('护栏拦截')
    expect(statusLabel('weird-new-status')).toBe('weird-new-status')
    expect(statusLabel(null)).toBe('—')
  })

  it('colours event types by prefix', () => {
    expect(eventTypeColor('ToolCalled')).toBe('text-accent-primary')
    expect(eventTypeColor('RunFailed')).toBe('text-accent-secondary')
    expect(eventTypeColor('GuardrailTriggered')).toBe('text-status-error')
    expect(eventTypeColor('DagStepStarted')).toBe('text-accent-quaternary')
    expect(eventTypeColor('SomethingElse')).toBe('text-text-tertiary')
  })

  it('detects 404 errors for empty-state handling', () => {
    expect(isNotFoundError({ status: 404 })).toBe(true)
    expect(isNotFoundError({ status: 400 })).toBe(false)
    expect(isNotFoundError(new Error('x'))).toBe(false)
    expect(isNotFoundError(null)).toBe(false)
  })

  it('extracts a message from unknown error shapes', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom')
    expect(errorMessage('plain')).toBe('plain')
    expect(errorMessage(null)).toBe('')
  })
})
