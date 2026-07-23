import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from '../../src/stores/uiStore'

describe('uiStore', () => {
  beforeEach(() => {
    useUIStore.setState({
      sidebarOpen: true,
      realtimePanelOpen: true,
      expandedEventSeqs: new Set(),
      theme: 'nebula',
    })
  })

  it('toggles sidebar open state', () => {
    useUIStore.getState().toggleSidebar()
    expect(useUIStore.getState().sidebarOpen).toBe(false)
    useUIStore.getState().toggleSidebar()
    expect(useUIStore.getState().sidebarOpen).toBe(true)
  })

  it('toggles realtime panel', () => {
    useUIStore.getState().toggleRealtimePanel()
    expect(useUIStore.getState().realtimePanelOpen).toBe(false)
  })

  it('toggles event expansion set membership', () => {
    useUIStore.getState().toggleEventExpansion(42)
    expect(useUIStore.getState().expandedEventSeqs.has(42)).toBe(true)
    useUIStore.getState().toggleEventExpansion(42)
    expect(useUIStore.getState().expandedEventSeqs.has(42)).toBe(false)
  })

  it('preserves other expanded seqs when toggling one', () => {
    useUIStore.getState().toggleEventExpansion(1)
    useUIStore.getState().toggleEventExpansion(2)
    useUIStore.getState().toggleEventExpansion(1)
    expect(useUIStore.getState().expandedEventSeqs.has(1)).toBe(false)
    expect(useUIStore.getState().expandedEventSeqs.has(2)).toBe(true)
  })

  it('sets theme', () => {
    useUIStore.getState().setTheme('light')
    expect(useUIStore.getState().theme).toBe('light')
  })
})