import { useMemo } from 'react'
import Particles from '@tsparticles/react'
import { ParticlesProvider } from '@tsparticles/react'
import { loadSlim } from '@tsparticles/slim'
import type { ISourceOptions } from '@tsparticles/engine'
import { cn } from '../../design-system/utils/cn'

export interface ParticleBackgroundProps {
  className?: string
}

const PARTICLE_OPTIONS: ISourceOptions = {
  background: { color: { value: 'transparent' } },
  fpsLimit: 60,
  detectRetina: true,
  fullScreen: { enable: false },
  particles: {
    number: { value: 70, density: { enable: true, width: 800, height: 800 } },
    color: { value: ['#6366F1', '#A855F7', '#EC4899', '#10B981'] },
    opacity: { value: { min: 0.1, max: 0.35 } },
    size: { value: { min: 0.5, max: 2 } },
    move: {
      enable: true,
      speed: 0.4,
      direction: 'none',
      random: true,
      straight: false,
      outModes: { default: 'out' as const },
    },
    links: {
      enable: true,
      distance: 140,
      color: '#6366F1',
      opacity: 0.12,
      width: 1,
    },
  },
  interactivity: {
    events: { onHover: { enable: true, mode: 'grab' } },
    modes: { grab: { distance: 160, links: { opacity: 0.25 } } },
  },
}

export function ParticleBackground({ className }: ParticleBackgroundProps) {
  const prefersReducedMotion = useMemo(
    () =>
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    [],
  )

  if (prefersReducedMotion) return null

  return (
    <ParticlesProvider init={loadSlim}>
      <Particles
        id="harness-particles"
        options={PARTICLE_OPTIONS}
        className={cn('h-full w-full', className)}
      />
    </ParticlesProvider>
  )
}