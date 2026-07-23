import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import FinalAnswer from './FinalAnswer'

describe('FinalAnswer', () => {
  it('renders plain text content', () => {
    render(<FinalAnswer content="Hello, here is the answer." />)
    expect(screen.getByText('Hello, here is the answer.')).toBeInTheDocument()
  })

  it('renders bold markdown', () => {
    render(<FinalAnswer content="This is **bold** text." />)
    const bold = document.querySelector('strong')
    expect(bold).toHaveTextContent('bold')
  })

  it('renders italic markdown', () => {
    render(<FinalAnswer content="This is *italic* text." />)
    const em = document.querySelector('em')
    expect(em).toHaveTextContent('italic')
  })

  it('renders inline code', () => {
    render(<FinalAnswer content="Use `npm install` to get started." />)
    const code = document.querySelector('code')
    expect(code).toHaveTextContent('npm install')
  })

  it('renders code blocks', () => {
    const text = "Here is code:\n```\nconsole.log('hi')\n```"
    render(<FinalAnswer content={text} />)
    const pre = document.querySelector('pre')
    expect(pre).toBeTruthy()
    expect(pre?.textContent).toContain("console.log('hi')")
  })

  it('renders headings', () => {
    const text = "# Heading 1\n## Heading 2\n### Heading 3"
    render(<FinalAnswer content={text} />)
    const h1 = document.querySelector('h1')
    const h2 = document.querySelector('h2')
    const h3 = document.querySelector('h3')
    expect(h1).toBeTruthy()
    expect(h2).toBeTruthy()
    expect(h3).toBeTruthy()
    expect(h1?.textContent).toContain('Heading 1')
    expect(h2?.textContent).toContain('Heading 2')
    expect(h3?.textContent).toContain('Heading 3')
  })

  it('renders lists', () => {
    const text = "- item 1\n- item 2\n- item 3"
    render(<FinalAnswer content={text} />)
    const lis = document.querySelectorAll('li')
    expect(lis).toHaveLength(3)
    expect(lis[0]?.textContent).toContain('item 1')
    expect(lis[1]?.textContent).toContain('item 2')
    expect(lis[2]?.textContent).toContain('item 3')
  })
})
