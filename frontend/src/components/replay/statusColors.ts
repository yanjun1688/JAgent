// Shared display helpers for the Event Replay Inspector.
// Pure formatting/colour helpers kept component-free so they can be unit tested.

export type Tone = 'info' | 'success' | 'warning' | 'error' | 'muted'

export interface ToneClasses {
  dot: string
  text: string
  badge: string
}

const TONE_CLASSES: Record<Tone, ToneClasses> = {
  info: {
    dot: 'bg-status-info/20 text-status-info',
    text: 'text-status-info',
    badge: 'bg-status-info/15 text-status-info',
  },
  success: {
    dot: 'bg-status-success/20 text-status-success',
    text: 'text-status-success',
    badge: 'bg-status-success/15 text-status-success',
  },
  warning: {
    dot: 'bg-status-warning/20 text-status-warning',
    text: 'text-status-warning',
    badge: 'bg-status-warning/15 text-status-warning',
  },
  error: {
    dot: 'bg-status-error/20 text-status-error',
    text: 'text-status-error',
    badge: 'bg-status-error/15 text-status-error',
  },
  muted: {
    dot: 'bg-surface-2 text-text-muted',
    text: 'text-text-muted',
    badge: 'bg-surface-1 text-text-muted',
  },
}

// Maps a run / step / tool status string to a semantic tone.
export function statusTone(status: string | null | undefined): Tone {
  switch (status) {
    case 'completed':
    case 'success':
      return 'success'
    case 'failed':
    case 'error':
      return 'error'
    case 'guardrail_blocked':
    case 'paused':
    case 'unsuccessful':
    case 'timeout':
    case 'skipped':
      return 'warning'
    case 'running':
    case 'started':
      return 'info'
    case 'pending':
    case 'unknown':
    default:
      return 'muted'
  }
}

export function toneClasses(tone: Tone): ToneClasses {
  return TONE_CLASSES[tone]
}

const STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  paused: '已暂停',
  completed: '已完成',
  failed: '已失败',
  started: '进行中',
  pending: '待执行',
  skipped: '已跳过',
  timeout: '超时',
  unsuccessful: '未成功',
  guardrail_blocked: '护栏拦截',
  unknown: '未知',
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return STATUS_LABELS[status] ?? status
}

// Colour an event-type label the same way the realtime event stream does.
export function eventTypeColor(eventType: string): string {
  if (eventType.startsWith('Tool')) return 'text-accent-primary'
  if (eventType.startsWith('Run')) return 'text-accent-secondary'
  if (eventType.startsWith('Confirm')) return 'text-status-warning'
  if (eventType.startsWith('Guardrail')) return 'text-status-error'
  if (eventType.startsWith('Plan') || eventType.startsWith('Dag')) return 'text-accent-quaternary'
  if (eventType.startsWith('Think') || eventType.startsWith('Observe') || eventType.startsWith('Agent')) {
    return 'text-status-info'
  }
  return 'text-text-tertiary'
}

export function formatTime(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString('zh-CN', { hour12: false })
}

export function errorMessage(error: unknown): string {
  if (!error) return ''
  if (error instanceof Error) return error.message
  return String(error)
}

export function isNotFoundError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'status' in error && (error as { status: number }).status === 404
}
