import type { Tone } from './kernel'

export interface ChatMessage {
  id: number
  role: 'you' | 'agent'
  text: string
  at: number
  /** A short label under the bubble, e.g. "已完成 · 2 步". */
  meta?: string
  tone?: Tone
  /** 输出分级徽标（"已核验" / "本次观测·已对账" / "未核验·模型观点"）。 */
  badge?: string
  badgeTone?: Tone
}
