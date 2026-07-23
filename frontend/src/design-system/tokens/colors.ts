export const colors = {
  background: {
    primary: '#0A0A0F',
    secondary: '#12121A',
    tertiary: '#1A1A2E',
  },
  accent: {
    primary: '#6366F1',
    secondary: '#A855F7',
    tertiary: '#EC4899',
    quaternary: '#10B981',
  },
  status: {
    success: '#10B981',
    warning: '#F59E0B',
    error: '#EF4444',
    info: '#3B82F6',
  },
  text: {
    primary: '#F8FAFC',
    secondary: '#94A3B8',
    tertiary: '#64748B',
    muted: '#475569',
  },
  white: {
    DEFAULT: '#FFFFFF',
    '5': 'rgba(255, 255, 255, 0.05)',
    '10': 'rgba(255, 255, 255, 0.1)',
    '20': 'rgba(255, 255, 255, 0.2)',
  },
} as const

export type ColorToken = typeof colors