import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import AnswerMarkdown from './AnswerMarkdown'

describe('AnswerMarkdown', () => {
  it('renders Markdown and turns citation markers into buttons', async () => {
    const onCite = vi.fn()
    render(
      <AnswerMarkdown
        text={'**Three days** per week [S1][S2].\n\n- item [S3]'}
        onCite={onCite}
        knownSources={new Set([1, 2])}
      />,
    )
    expect(screen.getByText('Three days').tagName).toBe('STRONG')
    await userEvent.click(screen.getByRole('button', { name: 'Show source 2' }))
    expect(onCite).toHaveBeenCalledWith(2)
    expect(screen.getByRole('button', { name: 'Show source 3' })).toBeDisabled()
  })

  it('never renders raw HTML, unsafe links or remote images', () => {
    const { container } = render(
      <AnswerMarkdown
        text={
          '<script>alert(1)</script><img src=x onerror="alert(2)">\n\n' +
          '[click](javascript:alert(3)) [docs](https://example.com) ![tracker](https://evil.test/p.png)'
        }
        onCite={() => {}}
        knownSources={new Set()}
      />,
    )
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.innerHTML).not.toContain('onerror')
    expect(container.innerHTML).not.toContain('javascript:')
    const docs = screen.getByRole('link', { name: 'docs' })
    expect(docs).toHaveAttribute('rel', 'noopener noreferrer nofollow')
    expect(docs).toHaveAttribute('target', '_blank')
    expect(screen.getByText('[tracker]')).toBeInTheDocument()
  })

  it('leaves markers inside code alone', () => {
    const { container } = render(
      <AnswerMarkdown text={'Use `x[S1]` literally.'} onCite={() => {}} knownSources={new Set([1])} />,
    )
    expect(container.querySelector('code')?.textContent).toBe('x[S1]')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
