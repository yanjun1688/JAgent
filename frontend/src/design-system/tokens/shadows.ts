export const shadows = {
  nebula:
    '0 0 0 1px rgba(99, 102, 241, 0.1), 0 4px 16px rgba(99, 102, 241, 0.15), 0 8px 32px rgba(0, 0, 0, 0.4)',
  glow: '0 0 20px rgba(99, 102, 241, 0.3), 0 0 40px rgba(99, 102, 241, 0.1)',
  float: '0 20px 60px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05)',
  sm: '0 1px 2px rgba(0, 0, 0, 0.3)',
  md: '0 4px 8px rgba(0, 0, 0, 0.3)',
  lg: '0 8px 16px rgba(0, 0, 0, 0.4)',
} as const

export type ShadowToken = typeof shadows