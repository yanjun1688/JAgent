import type { ReactNode } from 'react'
import { cx } from '../lib/cx'

interface SectionProps {
  title: string
  code: string
  right?: ReactNode
  children: ReactNode
  className?: string
}

export function Section({ title, code, right, children, className }: SectionProps) {
  return (
    <section className={cx('flex min-h-0 flex-col gap-3', className)}>
      <header className="flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <h2 className="text-sm text-text-primary">
            <span className="text-text-tertiary">[</span> {title} <span className="text-text-tertiary">]</span>
          </h2>
          <span className="font-mono text-xs uppercase tracking-[0.16em] text-text-tertiary">{code}</span>
        </div>
        {right}
      </header>
      {children}
    </section>
  )
}
