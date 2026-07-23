import { Link, useLocation } from 'react-router-dom'
import { MessageSquare, LayoutGrid, History, type LucideIcon } from 'lucide-react'
import { cn } from '../design-system/utils/cn'
import { ThemeToggle } from './ui/ThemeToggle'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

const navItems: NavItem[] = [
  { to: '/', label: '对话', icon: MessageSquare },
  { to: '/overview', label: '概览', icon: LayoutGrid },
  { to: '/history', label: '历史', icon: History },
]

function isActive(pathname: string, to: string): boolean {
  if (to === '/') return pathname === '/'
  return pathname === to || pathname.startsWith(to + '/')
}

export function Header() {
  const location = useLocation()

  return (
    <header className="glass-frosted sticky top-0 z-40 flex h-14 items-center gap-4 border-b border-border-soft px-6">
      <Link
        to="/"
        className="font-display text-lg font-bold tracking-tight text-text-primary"
      >
        <span className="gradient-nebula bg-clip-text text-transparent">Harness</span>
      </Link>
      <span className="text-xs text-text-tertiary">Agent-First 执行引擎</span>

      <nav className="ml-auto flex items-center gap-1">
        {navItems.map((item) => {
          const active = isActive(location.pathname, item.to)
          const Icon = item.icon
          return (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
                active
                  ? 'bg-accent-primary/20 text-accent-primary'
                  : 'text-text-secondary hover:text-text-primary hover:bg-surface-1',
              )}
            >
              <Icon size={16} />
              {item.label}
            </Link>
          )
        })}
      </nav>
      <ThemeToggle />
    </header>
  )
}