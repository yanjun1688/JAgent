import { useMemo } from 'react'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Stars } from '@react-three/drei'
import * as THREE from 'three'
import type { ToolStatItem } from '../../api/analysis-types'

export interface GalaxyCanvasProps {
  tools: ToolStatItem[]
  onToolClick?: (toolName: string) => void
}

interface GalaxyNode {
  name: string
  position: [number, number, number]
  radius: number
  color: string
  calls: number
}

const TOOL_COLORS = [
  '#6366F1',
  '#A855F7',
  '#EC4899',
  '#10B981',
  '#F59E0B',
  '#3B82F6',
  '#EF4444',
]

function distribute(tools: ToolStatItem[]): GalaxyNode[] {
  const n = tools.length
  return tools.map((t, i) => {
    const phi = Math.acos(-1 + (2 * i) / Math.max(n, 1))
    const theta = Math.sqrt(n * Math.PI) * phi
    const dist = 3 + Math.log10((t.call_count || 0) + 1) * 1.5
    return {
      name: t.tool_name,
      position: [
        dist * Math.cos(theta) * Math.sin(phi),
        dist * Math.sin(theta) * Math.sin(phi),
        dist * Math.cos(phi),
      ] as [number, number, number],
      radius: 0.2 + Math.log10((t.call_count || 0) + 1) * 0.25,
      color: TOOL_COLORS[i % TOOL_COLORS.length],
      calls: t.call_count || 0,
    }
  })
}

function ToolMesh({ node, onClick }: { node: GalaxyNode; onClick?: () => void }) {
  return (
    <mesh position={node.position} onClick={onClick}>
      <sphereGeometry args={[node.radius, 16, 16]} />
      <meshStandardMaterial
        color={node.color}
        emissive={new THREE.Color(node.color)}
        emissiveIntensity={0.6}
        roughness={0.3}
        metalness={0.7}
      />
    </mesh>
  )
}

export function GalaxyCanvas({ tools, onToolClick }: GalaxyCanvasProps) {
  const nodes = useMemo(() => distribute(tools), [tools])
  return (
    <Canvas camera={{ position: [0, 0, 12], fov: 50 }} dpr={[1, 2]}>
      <ambientLight intensity={0.4} />
      <pointLight position={[0, 0, 0]} intensity={1.4} color="#A855F7" />
      <Stars radius={50} depth={20} count={1500} factor={2} fade speed={1} />
      {nodes.map((n) => (
        <ToolMesh key={n.name} node={n} onClick={() => onToolClick?.(n.name)} />
      ))}
      <OrbitControls enablePan={false} autoRotate autoRotateSpeed={0.5} />
    </Canvas>
  )
}