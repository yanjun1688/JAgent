import { useMemo } from 'react'
import { colors } from '../api/analysis-styles'

interface Props {
  content: string
}

function renderMarkdown(text: string): string {
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

  html = html.replace(/^### (.+)$/gm, '<h3 style="font-size:15px;margin:10px 0 4px;font-weight:700;">$1</h3>')
  html = html.replace(/^## (.+)$/gm, '<h2 style="font-size:17px;margin:12px 0 6px;font-weight:700;">$1</h2>')
  html = html.replace(/^# (.+)$/gm, '<h1 style="font-size:20px;margin:14px 0 8px;font-weight:700;">$1</h1>')

  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')

  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre style="background:#1e1e1e;color:#d4d4d4;padding:12px;border-radius:6px;overflow:auto;font-size:12px;margin:8px 0;"><code>${code.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</code></pre>`
  })

  html = html.replace(/`([^`]+)`/g, '<code style="background:#f0f0f0;padding:1px 4px;border-radius:3px;font-size:12px;">$1</code>')

  const lines = html.split('\n')
  const result: string[] = []
  let inList = false

  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim()
    const listMatch = trimmed.match(/^(\s*)[-*] (.+)$/)
    if (listMatch) {
      if (!inList) {
        result.push('<ul style="margin:4px 0;padding-left:20px;">')
        inList = true
      }
      result.push(`<li style="margin:2px 0;">${listMatch[2]}</li>`)
    } else {
      if (inList) {
        result.push('</ul>')
        inList = false
      }
      result.push(trimmed || '<br/>')
    }
  }
  if (inList) result.push('</ul>')

  return result.join('\n')
}

export default function FinalAnswer({ content }: Props) {
  const html = useMemo(() => renderMarkdown(content), [content])

  return (
    <div
      style={{
        padding: '10px 14px',
        borderRadius: 8,
        border: `1px solid ${colors.border}`,
        background: '#fff',
        color: colors.text,
        fontSize: 14,
        lineHeight: 1.6,
        wordBreak: 'break-word',
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
