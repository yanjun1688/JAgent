import { motion } from 'motion/react'
import { Moon, Sun } from 'lucide-react'
import { useUIStore } from '../../stores/uiStore'
import { cn } from '../../design-system/utils/cn'

export interface ThemeToggleProps {
  className?: string
}

/**
 * 主题切换按钮 (白天 / 黑夜)
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~
 * 点击在 uiStore.theme (nebula | light) 之间切换，
 * 由 useTheme Hook 负责将变更同步到 <html>。
 */
export function ThemeToggle({ className }: ThemeToggleProps) {
  const theme = useUIStore((s) => s.theme)
  const toggleTheme = useUIStore((s) => s.toggleTheme)
  const isLight = theme === 'light'

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={isLight ? '切换到黑夜模式' : '切换到白天模式'}
      title={isLight ? '切换到黑夜模式' : '切换到白天模式'}
      className={cn(
        'inline-flex h-8 w-8 items-center justify-center rounded-lg border text-text-secondary transition-colors hover:text-text-primary',
        'border-border-soft bg-surface-1',
        className,
      )}
    >
      <motion.span
        key={theme}
        initial={{ rotate: -90, opacity: 0, scale: 0.5 }}
        animate={{ rotate: 0, opacity: 1, scale: 1 }}
        transition={{ duration: 0.2 }}
        className="inline-flex"
      >
        {isLight ? (
          <Moon size={15} className="text-accent-secondary" />
        ) : (
          <Sun size={15} className="text-status-warning" />
        )}
      </motion.span>
    </button>
  )
}