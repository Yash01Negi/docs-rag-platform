import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import * as api from '../lib/api'
import { makeDetail } from '../test/fixtures'
import DocumentDetailPage from './DocumentDetailPage'

const ID = '7b0c9a7e-0000-4000-8000-000000000001'

function chunkPage(): api.Page<api.ChunkPreview> {
  return {
    items: [
      {
        id: 'c1',
        ordinal: 0,
        text: '東京は日本の首都です。',
        token_count: 9,
        language: 'ja',
        page_start: 2,
        page_end: 3,
        section: '第1章 概要',
      },
    ],
    total: 1,
    limit: 10,
    offset: 0,
  }
}

function renderDetail(detail: api.DocumentDetail) {
  vi.spyOn(api, 'getDocument').mockResolvedValue(detail)
  vi.spyOn(api, 'listChunks').mockResolvedValue(chunkPage())
  render(
    <MemoryRouter initialEntries={[`/documents/${ID}`]}>
      <Routes>
        <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
        <Route path="/documents" element={<p>Library page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('DocumentDetailPage', () => {
  it('shows metadata, processing details and chunk previews with page references', async () => {
    renderDetail(makeDetail({ filename: '報告書.pdf', language: 'ja' }))

    expect(await screen.findByRole('heading', { name: '報告書.pdf' })).toBeInTheDocument()
    expect(screen.getByText('Indexed')).toBeInTheDocument()
    expect(screen.getByText('BAAI/bge-m3 (1024 dimensions)')).toBeInTheDocument()
    expect(screen.getByText('1 of 3')).toBeInTheDocument()
    expect(await screen.findByText('東京は日本の首都です。')).toBeInTheDocument()
    expect(screen.getByText('pp. 2–3')).toBeInTheDocument()
    expect(screen.getByText('§ 第1章 概要')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      `/api/v1/documents/${ID}/file?download=true`,
    )
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })

  it('shows the error of a failed document and retries it', async () => {
    const failed = makeDetail({
      status: 'failed',
      chunk_count: 0,
      error_code: 'no_extractable_text',
      error_message: 'No extractable text was found. Scanned documents need OCR.',
      processed_at: null,
      embedding_model: null,
    })
    renderDetail(failed)
    const retry = vi
      .spyOn(api, 'retryDocument')
      .mockResolvedValue({ ...failed, status: 'pending', error_code: null, error_message: null })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Ingestion failed')
    expect(alert).toHaveTextContent('Scanned documents need OCR')
    expect(alert).toHaveTextContent('code: no_extractable_text')
    expect(screen.queryByRole('button', { name: 'Reindex' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /Retry ingestion/ }))
    expect(retry).toHaveBeenCalledWith(ID)
    expect(api.getDocument).toHaveBeenCalledTimes(2) // reloaded after the retry
  })

  it('shows a failed reindex as a warning while the old index stays active', async () => {
    renderDetail(
      makeDetail({
        error_code: 'embedding_failed',
        error_message: 'Reindexing failed: timeout The previous index is still in use.',
      }),
    )
    expect(await screen.findByText('The last reindex failed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reindex/ })).toBeEnabled()
  })

  it('asks for confirmation before deleting and can be cancelled', async () => {
    renderDetail(makeDetail())
    const remove = vi.spyOn(api, 'deleteDocument').mockResolvedValue()

    await userEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    const dialog = screen.getByRole('alertdialog', { name: 'Delete this document?' })
    expect(dialog).toHaveTextContent('cannot be undone')
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(remove).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /Delete/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete document' }))
    expect(remove).toHaveBeenCalledWith(ID)
    expect(await screen.findByText('Library page')).toBeInTheDocument()
  })

  it('shows a not-found state', async () => {
    vi.spyOn(api, 'getDocument').mockRejectedValue(
      new api.ApiError(404, 'document_not_found', 'Document not found.'),
    )
    render(
      <MemoryRouter initialEntries={[`/documents/${ID}`]}>
        <Routes>
          <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('This document does not exist.')
  })

  it('shows the S3 source of an imported document', async () => {
    renderDetail(
      makeDetail({
        source_type: 's3',
        source: {
          connector_id: 'conn-1',
          connector_name: 'Team handbook',
          provider: 's3',
          bucket: 'team-docs',
          object_key: 'handbook/policy.pdf',
          uri: 's3://team-docs/handbook/policy.pdf',
          etag: '9b2cf535f27731c974343645a3985328-3',
          version_id: null,
          size_bytes: 2048,
          last_modified: '2026-10-17T07:00:00Z',
          last_synced_at: '2026-10-17T08:00:00Z',
        },
      }),
    )
    const source = await screen.findByRole('region', { name: 'Source: Amazon S3' })
    expect(within(source).getByText('s3://team-docs/handbook/policy.pdf')).toBeInTheDocument()
    expect(within(source).getByText('9b2cf535f27731c974343645a3985328-3')).toBeInTheDocument()
    expect(within(source).getByText('— (bucket not versioned)')).toBeInTheDocument()
    expect(within(source).getByRole('link', { name: 'Connector: Team handbook' })).toHaveAttribute(
      'href',
      '/connectors/conn-1',
    )
    expect(screen.getByText('Imported')).toBeInTheDocument()
  })

  it('has no source section for uploads', async () => {
    renderDetail(makeDetail())
    expect(await screen.findByText('Uploaded')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Source: Amazon S3' })).not.toBeInTheDocument()
  })
})
