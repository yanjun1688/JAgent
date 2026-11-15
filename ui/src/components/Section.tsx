import type { ReactNode } from 'react'
import { cx } from '../lib/cx'

interface SectionProps {
  title: string
  code: string
  right?: ReactNode
  children: ReactNode
  className?: string
}

/** City Pop panel header: Orbitron label + mono channel code. */
export function Section({ title, code, right, children, className }: SectionProps) {
  return (
    <section className={cx('flex min-h-0 flex-col gap-3', className)}>
      <header className="flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span aria-hidden className="h-3 w-0.5 bg-neon-magenta shadow-[0_0_6px_rgb(var(--neon-magenta)/0.7)]" />
          <h2 className="font-display text-[13px] font-medium uppercase tracking-[0.18em] text-text-primary">
            {title}
          </h2>
          <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-text-tertiary">
            {code}
          </span>
        </div>
        {right}
      </header>
      {children}
    </section>
  )
}
