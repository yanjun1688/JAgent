import { cn } from '../../design-system/utils/cn'

export interface AuroraGradientProps {
  className?: string
  /** 是否启用动画；遵循 prefers-reduced-motion */
  animated?: boolean
}

const prefersReducedMotion =
  typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export function AuroraGradient({ className, animated = true }: AuroraGradientProps) {
  const shouldAnimate = animated && !prefersReducedMotion
  return (
    <div
      aria-hidden
      className={cn(
        'pointer-events-none absolute inset-0 -z-10 overflow-hidden',
        className,
      )}
    >
      <div
        className={cn(
          'gradient-aurora absolute inset-0',
          shouldAnimate && 'animate-aurora-shift',
        )}
      />
    </div>
  )
}