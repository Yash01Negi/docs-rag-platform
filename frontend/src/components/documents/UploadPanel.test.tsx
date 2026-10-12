import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../../lib/api'
import { file, LIMITS, makeDocument } from '../../test/fixtures'
import UploadPanel from './UploadPanel'

function renderPanel(onUploaded = vi.fn()) {
  render(
    <MemoryRouter>
      <UploadPanel limits={LIMITS} limitsError={null} onUploaded={onUploaded} />
    </MemoryRouter>,
  )
  return onUploaded
}

describe('UploadPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows accepted formats and the size limit', () => {
    renderPanel()
    expect(screen.getByText(/\.pdf, \.md, \.markdown, \.txt/)).toBeInTheDocument()
    expect(screen.getByText(/up to 20 MB/)).toBeInTheDocument()
  })

  it('rejects an unsupported file without calling the API', async () => {
    const upload = vi.spyOn(api, 'uploadDocument')
    renderPanel()
    await userEvent.upload(screen.getByTestId('file-input'), file('photo.png', 100), {
      applyAccept: false,
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('unsupported file type')
    expect(upload).not.toHaveBeenCalled()
  })

  it('reports progress and success for a new document', async () => {
    let finish: (value: api.UploadResult) => void = () => {}
    vi.spyOn(api, 'uploadDocument').mockImplementation((_file, onProgress) => {
      onProgress?.(0.5)
      return new Promise((resolve) => {
        finish = resolve
      })
    })
    const onUploaded = renderPanel()

    await userEvent.upload(screen.getByTestId('file-input'), file('report.pdf', 1000))
    expect(await screen.findByText(/Uploading/)).toHaveTextContent('(50%)')
    expect(screen.getByRole('progressbar', { name: 'Upload progress' })).toBeInTheDocument()

    const result = { duplicate: false, document: makeDocument({ status: 'pending' }) }
    finish(result)
    expect(await screen.findByText(/uploaded and queued for indexing/)).toBeInTheDocument()
    expect(onUploaded).toHaveBeenCalledWith(result)
  })

  it('explains a duplicate upload and links to the existing document', async () => {
    const existing = makeDocument({ filename: 'original.pdf' })
    vi.spyOn(api, 'uploadDocument').mockResolvedValue({ duplicate: true, document: existing })
    renderPanel()

    await userEvent.upload(screen.getByTestId('file-input'), file('copy.pdf', 1000))
    expect(await screen.findByText(/already uploaded/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'original.pdf' })).toHaveAttribute(
      'href',
      `/documents/${existing.id}`,
    )
  })

  it('shows the server error message when the upload is rejected', async () => {
    vi.spyOn(api, 'uploadDocument').mockRejectedValue(
      new api.ApiError(422, 'encrypted_pdf', 'Encrypted or password-protected PDFs are not supported.'),
    )
    renderPanel()

    await userEvent.upload(screen.getByTestId('file-input'), file('locked.pdf', 1000))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'locked.pdf: Encrypted or password-protected PDFs are not supported.',
    )
  })

  it('accepts a dropped file', async () => {
    const upload = vi
      .spyOn(api, 'uploadDocument')
      .mockResolvedValue({ duplicate: false, document: makeDocument() })
    renderPanel()

    fireEvent.drop(screen.getByTestId('dropzone'), {
      dataTransfer: { files: [file('notes.md', 200)] },
    })
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1))
  })

  it('opens the file picker from the keyboard', async () => {
    renderPanel()
    const input = screen.getByTestId('file-input') as HTMLInputElement
    const click = vi.spyOn(input, 'click')
    screen.getByRole('button', { name: /Drop a file here/ }).focus()
    await userEvent.keyboard('{Enter}')
    expect(click).toHaveBeenCalled()
  })
})
