import { motion, type HTMLMotionProps } from 'motion/react'
import { cn } from '../../design-system/utils/cn'

type GlassVariant = 'base' | 'elevated' | 'frosted'

const variantClasses: Record<GlassVariant, string> = {
  base: 'glass-base',
  elevated: 'glass-elevated',
  frosted: 'glass-frosted',
}

export interface GlassCardProps extends HTMLMotionProps<'div'> {
  variant?: GlassVariant
  children: React.ReactNode
  interactive?: boolean
}

export function GlassCard({
  variant = 'base',
  children,
  className,
  interactive = false,
  ...props
}: GlassCardProps) {
  return (
    <motion.div
      className={cn('rounded-2xl', variantClasses[variant], className)}
      whileHover={interactive ? { scale: 1.01 } : undefined}
      transition={interactive ? { type: 'spring', stiffness: 300 } : undefined}
      {...props}
    >
      {children}
    </motion.div>
  )
}