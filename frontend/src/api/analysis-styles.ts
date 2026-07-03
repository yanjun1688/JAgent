export const colors = {
  primary: '#3ECF8E',
  primaryDark: '#2db37a',
  blue: '#1a73e8',
  blueLight: '#e8f0fe',
  red: '#ef5350',
  redLight: '#fde8e8',
  orange: '#ffb74d',
  orangeLight: '#fff3e0',
  purple: '#ce93d8',
  purpleLight: '#f3e5f5',
  teal: '#26a69a',
  tealLight: '#e0f2f1',
  gray: '#78909c',
  grayLight: '#eceff1',
  bg: '#f5f6f8',
  card: '#fff',
  text: '#1a1a2e',
  textSecondary: '#666',
  border: '#e8eaed',
  success: '#66bb6a',
  successLight: '#e8f5e9',
}

export const card: React.CSSProperties = {
  background: colors.card,
  borderRadius: 12,
  padding: 24,
  boxShadow: '0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04)',
  border: `1px solid ${colors.border}`,
}

export const valueText: React.CSSProperties = {
  fontSize: 32,
  fontWeight: 700,
  lineHeight: 1.2,
}

export const labelText: React.CSSProperties = {
  fontSize: 13,
  color: colors.textSecondary,
  marginTop: 4,
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
  fontWeight: 600,
}

export const badge = (bg: string, fg = '#fff'): React.CSSProperties => ({
  display: 'inline-block',
  padding: '2px 10px',
  borderRadius: 12,
  fontSize: 12,
  fontWeight: 700,
  color: fg,
  background: bg,
})

export const sectionTitle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  color: colors.text,
  marginBottom: 16,
}

export const table: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
}

export const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '10px 8px',
  borderBottom: `2px solid ${colors.border}`,
  fontSize: 12,
  fontWeight: 700,
  color: colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
}

export const td: React.CSSProperties = {
  padding: 12,
  borderBottom: `1px solid ${colors.border}`,
  fontSize: 14,
}

export const statusBadge = (status: string): React.CSSProperties => {
  const map: Record<string, string> = {
    running: colors.blue,
    paused: colors.orange,
    completed: colors.success,
    failed: colors.red,
  }
  return badge(map[status] || colors.gray)
}

export const eventTypeBadge = (type: string): React.CSSProperties => {
  const map: Record<string, string> = {
    RunStarted: colors.blue,
    AgentThought: colors.purple,
    ToolCalled: colors.orange,
    ToolCompleted: colors.success,
    ToolFailed: colors.red,
    ToolTimeout: colors.red,
    GuardrailTriggered: '#ff7043',
    ConfirmationRequested: colors.orange,
    ConfirmationReceived: colors.teal,
    RunPaused: colors.gray,
    RunResumed: colors.teal,
    RunCompleted: colors.success,
    RunFailed: colors.red,
  }
  return badge(map[type] || '#999')
}

export function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const min = Math.floor(ms / 60000)
  const sec = Math.round((ms % 60000) / 1000)
  return `${min}m ${sec}s`
}

export function fmt(n: number | null | undefined): string {
  if (n == null) return '-'
  return n.toLocaleString()
}

export function pct(n: number): string {
  return `${(n * 100).toFixed(0)}%`
}
