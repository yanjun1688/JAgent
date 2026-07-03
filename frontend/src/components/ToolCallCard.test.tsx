import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ToolCallCard from './ToolCallCard'

describe('ToolCallCard', () => {
  it('shows tool name and running status', () => {
    render(<ToolCallCard toolName="http_request" status="running" />)
    expect(screen.getByText('http_request')).toBeInTheDocument()
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('shows completed status with checkmark', () => {
    render(<ToolCallCard toolName="search" status="completed" durationMs={1500} />)
    expect(screen.getByText('search')).toBeInTheDocument()
    expect(screen.getByText('Completed')).toBeInTheDocument()
    expect(screen.getByText('1.5s')).toBeInTheDocument()
  })

  it('shows failed status with error', () => {
    render(<ToolCallCard toolName="browser" status="failed" error="Connection refused" durationMs={500} />)
    expect(screen.getByText('browser')).toBeInTheDocument()
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('0.5s')).toBeInTheDocument()
  })

  it('shows input details when expanded', () => {
    const input = { url: 'https://example.com', method: 'GET' }
    render(<ToolCallCard toolName="http_request" status="completed" input={input} />)
    fireEvent.click(screen.getByText('http_request'))
    expect(screen.getByText('Input:')).toBeInTheDocument()
    expect(screen.getByText(/https:\/\/example\.com/)).toBeInTheDocument()
  })

  it('shows output details when expanded', () => {
    const output = { status: 200, body: 'OK' }
    render(<ToolCallCard toolName="http_request" status="completed" output={output} />)
    fireEvent.click(screen.getByText('http_request'))
    expect(screen.getByText('Output:')).toBeInTheDocument()
  })
})
