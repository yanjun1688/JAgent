import { Suspense, lazy } from 'react'
import type { ToolStatItem } from '../../api/analysis-types'

export interface ToolGalaxyProps {
  tools: ToolStatItem[]
  onToolClick?: (toolName: string) => void
}

// Three.js bundle 独立 chunk，仅在需要时加载
const GalaxyCanvas = lazy(() =>
  import('./GalaxyCanvas').then((m) => ({ default: m.GalaxyCanvas })),
)

export function ToolGalaxy({ tools, onToolClick }: ToolGalaxyProps) {
  return (
    <div className="h-[420px] w-full overflow-hidden rounded-2xl glass-base">
      <Suspense fallback={<div className="h-full w-full animate-pulse bg-surface-1" />}>
        <GalaxyCanvas tools={tools} onToolClick={onToolClick} />
      </Suspense>
    </div>
  )
}