import type { Tone } from './kernel'

export interface ChatMessage {
  id: number
  role: 'you' | 'agent'
  text: string
  at: number
  /** A short label under the bubble, e.g. "已完成 · 2 步". */
  meta?: string
  tone?: Tone
}
