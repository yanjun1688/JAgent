import type { Tone } from '../lib/kernel'
import { cx } from '../lib/cx'

const SIZE = {
  xs: 'h-1.5 w-1.5',
  sm: 'h-2 w-2',
  md: 'h-2.5 w-2.5',
} as const

const LAMP: Record<Tone, string> = {
  off: 'lamp-off',
  ok: 'lamp-ok',
  warn: 'lamp-warn',
  bad: 'lamp-bad',
  flow: 'lamp-flow',
  human: 'lamp-human',
  live: 'lamp-live',
}

interface LedProps {
  tone: Tone
  size?: keyof typeof SIZE
  /** Change this value to replay the flash. A stable value never re-animates. */
  flash?: string | number
  /** Slow breathing, for a lamp that stays on while work continues. */
  breathe?: boolean
  className?: string
}

export function Led({ tone, size = 'sm', flash, breathe = false, className }: LedProps) {
  return (
    <span
      key={flash}
      role="presentation"
      className={cx(
        'inline-block shrink-0 rounded-full transition-colors duration-300 ease-base',
        SIZE[size],
        LAMP[tone],
        flash !== undefined && 'animate-led-pulse',
        breathe && 'animate-led-breathe',
        className,
      )}
    />
  )
}
