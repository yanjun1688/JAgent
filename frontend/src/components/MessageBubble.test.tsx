import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import MessageBubble from './MessageBubble'

describe('MessageBubble', () => {
  it('renders user message with blue background', () => {
    render(<MessageBubble role="user" content="Hello, world" timestamp={1620000000} />)
    const content = screen.getByText('Hello, world')
    expect(content).toBeInTheDocument()
    expect(content.style.color).toBe('rgb(255, 255, 255)')
  })

  it('renders assistant message with white background and border', () => {
    render(<MessageBubble role="assistant" content="I am an agent" />)
    const content = screen.getByText('I am an agent')
    expect(content).toBeInTheDocument()
    expect(content.style.color).toBe('rgb(26, 26, 46)')
  })

  it('shows timestamp when provided', () => {
    render(<MessageBubble role="user" content="hi" timestamp={1620000000} />)
    const timeEl = screen.getByText(/\d{2}:\d{2}:\d{2}/)
    expect(timeEl).toBeInTheDocument()
  })

  it('does not show timestamp when not provided', () => {
    const { container } = render(<MessageBubble role="user" content="hi" />)
    const timeElements = container.querySelectorAll('[style*="font-size: 10px"]')
    expect(timeElements.length).toBe(0)
  })
})
