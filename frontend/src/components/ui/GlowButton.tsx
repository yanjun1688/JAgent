import { motion, type HTMLMotionProps } from 'motion/react'
import { cn } from '../../design-system/utils/cn'

type ButtonVariant = 'primary' | 'secondary' | 'danger'
type ButtonSize = 'sm' | 'md' | 'lg'

export interface GlowButtonProps extends HTMLMotionProps<'button'> {
  variant?: ButtonVariant
  size?: ButtonSize
  children: React.ReactNode
}

const variantClasses: Record<ButtonVariant, string> = {
  primary: 'bg-gradient-to-r from-accent-primary to-accent-secondary',
  secondary: 'glass-base hover:glass-elevated',
  danger: 'bg-status-error hover:bg-status-error/90',
}

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'px-4 py-2 text-sm',
  md: 'px-6 py-3 text-base',
  lg: 'px-8 py-4 text-lg',
}

export function GlowButton({
  variant = 'primary',
  size = 'md',
  children,
  className,
  ...props
}: GlowButtonProps) {
  return (
    <motion.button
      className={cn(
        'relative overflow-hidden rounded-xl font-medium text-white',
        variantClasses[variant],
        sizeClasses[size],
        className,
      )}
      whileHover={{
        scale: 1.05,
        boxShadow: '0 0 30px rgba(99, 102, 241, 0.5)',
      }}
      whileTap={{ scale: 0.95 }}
      {...props}
    >
      <span className="relative z-10">{children}</span>
      <motion.span
        className="pointer-events-none absolute inset-0 bg-white/20 blur-md"
        animate={{ scale: [1, 1.5, 1], opacity: [0.3, 0, 0.3] }}
        transition={{ duration: 2, repeat: Infinity }}
      />
    </motion.button>
  )
}