import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import * as api from '../lib/api'
import { LIMITS, makeDocument } from '../test/fixtures'
import DocumentsPage from './DocumentsPage'

function page(items: api.DocumentSummary[], total = items.length): api.Page<api.DocumentSummary> {
  return { items, total, limit: 20, offset: 0 }
}

function renderPage() {
  vi.spyOn(api, 'fetchUploadLimits').mockResolvedValue(LIMITS)
  return render(
    <MemoryRouter>
      <DocumentsPage />
    </MemoryRouter>,
  )
}

describe('DocumentsPage', () => {
  it('renders documents returned by the API', async () => {
    vi.spyOn(api, 'listDocuments').mockResolvedValue(
      page([
        makeDocument({ id: 'a', filename: '年次報告書.pdf', language: 'ja', chunk_count: 42 }),
        makeDocument({ id: 'b', filename: 'notes.md', file_type: 'markdown', status: 'processing', chunk_count: 0 }),
        makeDocument({
          id: 'c',
          filename: 'scan.pdf',
          status: 'failed',
          chunk_count: 0,
          error_code: 'no_extractable_text',
          error_message: 'No extractable text was found.',
        }),
      ]),
    )
    renderPage()

    const table = await screen.findByRole('table', { name: 'Uploaded documents' })
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(4) // header + 3
    expect(within(rows[1]).getByRole('link', { name: '年次報告書.pdf' })).toHaveAttribute(
      'href',
      '/documents/a',
    )
    expect(within(rows[1]).getByText('Japanese')).toBeInTheDocument()
    expect(within(rows[1]).getByText('42')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Indexed')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Processing')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Markdown')).toBeInTheDocument()
    expect(within(rows[3]).getByText('Failed')).toBeInTheDocument()
    expect(within(rows[3]).getByText('No extractable text was found.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Library/ })).toHaveTextContent('(3)')
  })

  it('shows an empty state when there are no documents', async () => {
    vi.spyOn(api, 'listDocuments').mockResolvedValue(page([]))
    renderPage()
    expect(await screen.findByText('No documents yet')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('shows an error state with a retry button', async () => {
    const list = vi
      .spyOn(api, 'listDocuments')
      .mockRejectedValueOnce(new api.ApiError(503, 'service_unavailable', 'Database unavailable'))
      .mockResolvedValueOnce(page([makeDocument()]))
    renderPage()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Documents could not be loaded.')
    expect(alert).toHaveTextContent('Database unavailable')

    await userEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    // Rendered twice: the desktop table and the mobile card list (CSS decides which shows).
    expect(await screen.findAllByRole('link', { name: 'report.pdf' })).toHaveLength(2)
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('filters by status', async () => {
    const list = vi.spyOn(api, 'listDocuments').mockResolvedValue(page([]))
    renderPage()
    await screen.findByText('No documents yet')

    await userEvent.selectOptions(screen.getByLabelText('Status'), 'failed')
    expect(await screen.findByText('No failed documents')).toBeInTheDocument()
    expect(list).toHaveBeenLastCalledWith({ limit: 20, offset: 0, status: 'failed' })
  })

  it('paginates', async () => {
    const list = vi
      .spyOn(api, 'listDocuments')
      .mockImplementation(async ({ offset }) => ({
        items: [makeDocument({ id: `doc-${offset}`, filename: `doc-${offset}.pdf` })],
        total: 45,
        limit: 20,
        offset,
      }))
    renderPage()

    const nav = await screen.findByRole('navigation', { name: 'Document pages' })
    expect(nav).toHaveTextContent('1–20 of 45')
    await userEvent.click(within(nav).getByRole('button', { name: 'Next' }))
    expect(await screen.findAllByRole('link', { name: 'doc-20.pdf' })).toHaveLength(2)
    expect(list).toHaveBeenLastCalledWith({ limit: 20, offset: 20, status: null })
  })
})
